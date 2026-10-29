import asyncio
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlparse
import httpx
import pytest
from fastapi.testclient import TestClient
from backend import main, spotify

CLIENT_ID = 'a' * 32

@pytest.fixture
def client(monkeypatch):
    instance = spotify.SpotifySession()
    monkeypatch.setattr(spotify, 'session', instance)
    with TestClient(main.app) as client:
        yield client, instance

def login(client):
    response = client.post('/api/spotify/login', json={'client_id':CLIENT_ID}, headers={'X-Podscope':'local'})
    assert response.status_code == 200
    return parse_qs(urlparse(response.json()['url']).query)

def test_pkce_and_state_cookie(client, monkeypatch):
    client, session = client
    query = login(client)
    assert query['scope'] == [spotify.SCOPE]
    assert query['redirect_uri'] == [spotify.REDIRECT_URI]
    state = query['state'][0]
    verifier = session.pending[state]['verifier']
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    assert query['code_challenge'] == [challenge]
    assert query['code_challenge_method'] == ['S256']
    assert 'client_secret' not in query
    assert client.get('/api/spotify/callback?state=wrong&code=example', follow_redirects=False).headers['location'].endswith('invalid_state')
    assert state in session.pending
    async def exchange(body):
        assert body['code_verifier'] == verifier
        return {'access_token':'test-access', 'refresh_token':'test-refresh','expires_in':3600,'scope':spotify.SCOPE}
    monkeypatch.setattr(session, 'exchange', exchange)
    started = []
    monkeypatch.setattr(session, 'start_sync', lambda: started.append(True))
    result = client.get('/api/spotify/callback', params={'state':state,'code':'example'}, follow_redirects=False)
    assert result.headers['location'] == '/?spotify=connected'
    assert started and session.tokens['access_token'] == 'test-access'
    public = client.get('/api/spotify/status')
    assert public.json()['connected'] and 'test-access' not in public.text and 'test-refresh' not in public.text
    replay = client.get('/api/spotify/callback',params={'state':state,'code':'example'},follow_redirects=False)
    assert replay.headers['location'].endswith('invalid_state')

def test_denial_expiry_disconnect_and_input(client):
    client, session = client
    assert client.post('/api/spotify/login',json={'client_id':CLIENT_ID}).status_code == 403
    assert client.post('/api/spotify/login',json={'client_id':'bad'},headers={'X-Podscope':'local'}).status_code == 400
    state = login(client)['state'][0]
    assert client.get('/api/spotify/callback',params={'state':state,'error':'access_denied'},follow_redirects=False).headers['location'].endswith('denied')
    state = login(client)['state'][0]; session.pending[state]['expires'] = time.time()-1
    assert client.get('/api/spotify/callback',params={'state':state,'code':'x'},follow_redirects=False).headers['location'].endswith('invalid_state')
    session.tokens={'access_token':'x'};session.catalog=[{'name':'Cached'}]
    assert client.post('/api/spotify/disconnect',json={},headers={'X-Podscope':'local'}).status_code == 200
    assert not session.tokens and not session.catalog and not session.pending
    assert client.post('/api/spotify/sync',json={},headers={'X-Podscope':'local'}).status_code == 401

def test_sync_pagination_refresh_dedup_and_partial():
    calls=[]
    def handler(request):
        calls.append(request)
        if request.url.host=='accounts.spotify.com':
            assert b'grant_type=refresh_token' in request.content
            return httpx.Response(200,json={'access_token':'fresh','expires_in':3600})
        assert request.headers['authorization']=='Bearer fresh'
        if request.url.path=='/v1/me':
            return httpx.Response(200,json={'display_name':'Test listener','images':[]})
        if request.url.path=='/v1/me/shows':
            return httpx.Response(200,json={'items':[{'show':{'id':'abc','name':'Example','total_episodes':4}}], 'next':None})
        if request.url.params.get('offset')=='50':
            return httpx.Response(200,json={'items':[{'id':'two','duration_ms':120000},{'id':'one','duration_ms':60000}], 'total':3,'next':None})
        return httpx.Response(200,json={'items':[{'id':'one','duration_ms':60000}], 'total':3,'next':'https://api.spotify.com/v1/shows/abc/episodes?offset=50&limit=50'})
    session=spotify.SpotifySession(httpx.MockTransport(handler))
    session.client_id=CLIENT_ID
    session.tokens={'access_token':'old','refresh_token':'refresh','expires_at':0}
    asyncio.run(session.sync())
    assert not session.error
    assert session.catalog[0]['retrieved_episodes']==2
    assert session.catalog[0]['duration_ms']==180000
    assert session.catalog[0]['partial'] is True
    assert session.tokens['refresh_token']=='refresh'
    assert len(calls)==5

def test_rate_limit_preserves_snapshot_and_blocks_retry():
    session=spotify.SpotifySession(httpx.MockTransport(lambda r:httpx.Response(429,headers={'Retry-After':'120'})))
    session.tokens={'access_token':'x','expires_at':time.time()+3600}
    session.catalog=[{'name':'previous snapshot'}]
    asyncio.run(session.sync())
    assert 'rate limit' in session.error
    assert session.retry_at > time.time()+100
    assert session.catalog == [{'name':'previous snapshot'}]
    with pytest.raises(spotify.HTTPException) as exc:
        session.start_sync()
    assert exc.value.status_code==429

def test_pagination_cannot_leak_token_to_other_host():
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'items':[],'next':'https://example.org/steal'})
    session=spotify.SpotifySession(httpx.MockTransport(handler))
    session.tokens={'access_token':'x','expires_at':time.time()+3600}
    asyncio.run(session.sync())
    assert len(requests)==2 and all(r.url.host=='api.spotify.com' for r in requests) and 'pagination' in session.error

def test_disconnect_cancels_sync():
    async def scenario():
        session=spotify.SpotifySession()
        session.tokens={'access_token':'x','expires_at':time.time()+3600}
        started=asyncio.Event()
        async def slow_sync():
            started.set(); await asyncio.sleep(100)
            session.catalog=[{'name':'must not return'}]
        session.sync=slow_sync
        session.start_sync()
        await started.wait()
        await session.disconnect()
        assert not session.syncing and not session.catalog and not session.tokens
    asyncio.run(scenario())

def test_progress_estimate_and_missing_data():
    episodes=[{'id':'complete','duration_ms':60000,'resume_point':{'fully_played':True,'resume_position_ms':0}},
              {'id':'partial','duration_ms':100000,'resume_point':{'fully_played':False,'resume_position_ms':30000}},
              {'id':'new','duration_ms':40000,'resume_point':{'fully_played':False,'resume_position_ms':0}}]
    session=spotify.SpotifySession(httpx.MockTransport(lambda r:httpx.Response(200,json={'items':episodes,'total':len(episodes),'next':None})))
    session.tokens={'access_token':'x','expires_at':time.time()+3600}
    async def scenario():
        async with session.http() as client:
            result=await session.show_details(client,{'id':'abc','total_episodes':3})
            assert result['duration_ms']==200000
            assert result['listened_ms']==90000 and result['remaining_ms']==110000
            assert result['progress_unknown']==0 and not result['partial']
            episodes.append({'id':'unknown','duration_ms':10000})
            result=await session.show_details(client,{'id':'abc','total_episodes':4})
            assert result['listened_ms'] is None and result['remaining_ms'] is None
            assert result['progress_unknown']==1
            episodes[-1]['resume_point']={'fully_played':False,'resume_position_ms':999999}
            result=await session.show_details(client,{'id':'abc','total_episodes':4})
            assert result['listened_ms']==100000
    asyncio.run(scenario())


def test_search_details_and_follow(client):
    client,session=client
    requests=[]
    def handler(r):
        requests.append(r)
        if r.url.path=='/v1/search':
            assert r.url.params['limit']=='10' and r.url.params['type']=='show'
            return httpx.Response(200,json={'shows':{'items':[{'id':'abc','name':'A podcast','total_episodes':1,'description':'A description'}],'next':None}})
        if r.url.path=='/v1/shows/abc':
            return httpx.Response(200,json={'id':'abc','name':'A podcast','total_episodes':1,'description':'A description'})
        if r.url.path.endswith('/episodes'):
            return httpx.Response(200,json={'items':[{'id':'ep','name':'Episode','duration_ms':60000,'resume_point':{'fully_played':False,'resume_position_ms':10000}}],'next':None,'total':1})
        if r.url.path=='/v1/me/library':
            assert r.method=='PUT' and r.url.params['uris']=='spotify:show:abc'
            return httpx.Response(200)
        raise AssertionError(str(r.url))
    session.transport=httpx.MockTransport(handler)
    session.tokens={'access_token':'x','expires_at':time.time()+3600}
    assert client.get('/api/spotify/search?q=code').json()['items'][0]['name']=='A podcast'
    detail=client.get('/api/spotify/shows/abc').json()
    assert detail['remaining_ms']==50000 and detail['description']=='A description'
    response=client.post('/api/spotify/shows/abc/follow',json={},headers={'X-Podscope':'local'})
    assert response.status_code==200 and response.json()['followed']
    assert 'abc' in session.followed_ids and len(session.catalog)==1
    assert client.get('/api/spotify/shows/abc').json()['followed']
    assert client.get('/api/spotify/search?q=a&offset=-1').status_code==422
    assert client.post('/api/spotify/shows/abc/follow',json={}).status_code==403


def test_signin_accepts_frontend_header_and_errors_are_json(client):
    from pathlib import Path
    import re
    client, _ = client
    # Exercise the header actually sent by the frontend, not a separately hardcoded copy.
    source=(Path(__file__).resolve().parents[1]/'frontend/src/main.tsx').read_text()
    name,value=re.search(r"'(X-[^']+)':'([^']+)'",source).groups()
    response=client.post('/api/spotify/login',json={'client_id':CLIENT_ID},headers={name:value})
    assert response.status_code==200
    assert response.json()['url'].startswith('https://accounts.spotify.com/authorize?')
    for headers in ({}, {name:'wrong'}):
        response=client.post('/api/spotify/login',json={'client_id':CLIENT_ID},headers=headers)
        assert response.status_code==403
        assert 'Refresh Statify' in response.json()['detail']


def test_unfollow_persists_and_failure_preserves_library(client, tmp_path):
    client, session = client
    session.store = spotify.SessionStore(tmp_path/'session.sqlite3')
    session.tokens = {'access_token':'x','expires_at':time.time()+3600}
    session.followed_ids = {'abc'}
    session.catalog = [{'id':'abc','name':'Example'}]
    def handler(request):
        assert request.method == 'DELETE'
        assert request.url.path == '/v1/me/library'
        assert request.url.params['uris'] == 'spotify:show:abc'
        return httpx.Response(200)
    session.transport = httpx.MockTransport(lambda r: httpx.Response(403))
    assert client.post('/api/spotify/shows/abc/unfollow',json={},headers={'X-Statify':'local'}).status_code == 502
    assert session.followed_ids == {'abc'} and len(session.catalog) == 1
    session.transport = httpx.MockTransport(handler)
    assert client.post('/api/spotify/shows/abc/unfollow',json={}).status_code == 403
    response = client.post('/api/spotify/shows/abc/unfollow',json={},headers={'X-Statify':'local'})
    assert response.json() == {'followed':False}
    restored = spotify.SpotifySession(store=session.store)
    assert not restored.catalog and not restored.followed_ids


def test_unfollow_during_sync_does_not_restore_show():
    async def scenario():
        def handler(request):
            if request.url.path == '/v1/me':
                return httpx.Response(200,json={'images':[]})
            return httpx.Response(200,json={'items':[{'show':{'id':'abc','name':'Example'}}],'next':None})
        session = spotify.SpotifySession(httpx.MockTransport(handler))
        session.tokens = {'access_token':'x','expires_at':time.time()+3600}
        async def details(client, show):
            session.followed_ids.discard('abc')
            session.library_changes['abc'] = False
            return {'id':'abc','name':'Example','episodes':[]}
        session.show_details = details
        await session.sync()
        assert not session.error and not session.catalog and not session.followed_ids
    asyncio.run(scenario())


def test_profile_image_skips_missing_entries():
    assert spotify.safe_image([]) is None
    assert spotify.safe_image([{}, {'url':'https://example.com/avatar.jpg'}]) == 'https://example.com/avatar.jpg'
    assert spotify.safe_image([{'url':'http://example.com/avatar.jpg'}]) is None
