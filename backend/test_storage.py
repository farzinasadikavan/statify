import asyncio
import json
import os
import time
import httpx
from backend.spotify import SpotifySession
from backend.storage import SessionStore


def test_restart_restores_account_and_snapshot(tmp_path):
    path=tmp_path/'state'/'statify.sqlite3'
    first=SpotifySession(store=SessionStore(path))
    first.client_id='a'*32
    first.tokens={'access_token':'test-access','refresh_token':'test-refresh','expires_at':time.time()+3600}
    first.profile={'name':'Listener'}
    first.catalog=[{'id':'show1','name':'Example','duration_ms':60000}]
    first.details={'show1':{'episodes':[{'id':'ep1','listened_ms':30000}]}}
    first.followed_ids={'show1'};first.last_sync=123;first.retry_at=456
    first.persist()
    second=SpotifySession(store=SessionStore(path))
    assert second.tokens==first.tokens and second.catalog==first.catalog
    assert second.details==first.details and second.profile==first.profile
    assert second.followed_ids=={'show1'} and second.last_sync==123
    assert second.client_id=='a'*32 and second.retry_at==456
    assert 'test-refresh' not in json.dumps(second.status())
    assert os.stat(path).st_mode&0o777==0o600
    assert os.stat(path.parent).st_mode&0o777==0o700


def test_rotated_refresh_token_survives_restart(tmp_path):
    path=tmp_path/'state'/'statify.sqlite3'
    def handler(r):
        assert b'refresh_token=old-refresh' in r.content
        return httpx.Response(200,json={'access_token':'new-access','refresh_token':'new-refresh','expires_in':3600})
    first=SpotifySession(transport=httpx.MockTransport(handler),store=SessionStore(path))
    first.tokens={'access_token':'expired','refresh_token':'old-refresh','expires_at':0}
    first.persist()
    assert asyncio.run(first.access_token())=='new-access'
    second=SpotifySession(store=SessionStore(path))
    assert second.tokens['refresh_token']=='new-refresh'
    assert second.tokens['expires_at']>time.time()+3500


def test_signout_is_persistent(tmp_path):
    path=tmp_path/'state'/'statify.sqlite3'
    first=SpotifySession(store=SessionStore(path))
    first.client_id='a'*32;first.tokens={'access_token':'x'};first.catalog=[{'id':'show'}]
    first.profile={'name':'Listener'};first.followed_ids={'show'};first.persist()
    asyncio.run(first.disconnect())
    second=SpotifySession(store=SessionStore(path))
    assert not second.tokens and not second.catalog and not second.followed_ids
    assert second.profile is None and second.client_id=='a'*32


def test_revoked_auth_is_not_restored(tmp_path):
    path=tmp_path/'state'/'statify.sqlite3'
    first=SpotifySession(transport=httpx.MockTransport(lambda r:httpx.Response(400,json={'error':'invalid_grant'})),store=SessionStore(path))
    first.tokens={'access_token':'expired','refresh_token':'revoked','expires_at':0};first.persist()
    try:
        asyncio.run(first.access_token())
    except Exception:
        pass
    assert not SpotifySession(store=SessionStore(path)).tokens
