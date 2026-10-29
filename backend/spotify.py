"""Single-user, loopback-only Spotify PKCE integration.

Account credentials and snapshots survive restarts in an owner-only SQLite database.
"""
import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from urllib.parse import urlencode, urlparse
from pathlib import Path
from .storage import SessionStore

import httpx
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

REDIRECT_URI = 'http://127.0.0.1:8765/api/spotify/callback'
SCOPE = 'user-library-read user-library-modify user-read-playback-position user-read-private'
router = APIRouter(prefix='/api/spotify', tags=['Spotify'])


class SpotifyError(Exception):
    pass


class SpotifySession:
    def __init__(self, transport=None, store=None):
        self.transport = transport
        self.tokens = {}
        self.client_id = os.environ.get('SPOTIFY_CLIENT_ID', '')
        self.pending = {}
        self.catalog = []
        self.last_sync = None
        self.retry_at = 0
        self.error = ''
        self.progress = ''
        self.task = None
        self.generation = 0
        self.profile = None
        self.details = {}
        self.followed_ids = set()
        self.library_changes = {}
        self.token_lock = asyncio.Lock()
        self.store = store
        if self.store:
            saved = self.store.load()
            for key in ('tokens', 'client_id', 'catalog', 'profile', 'details', 'last_sync', 'retry_at'):
                if key in saved:
                    setattr(self, key, saved[key])
            self.followed_ids = set(saved.get('followed_ids', []))

    def persist(self):
        if self.store:
            self.store.save({key: getattr(self, key) for key in
                ('tokens', 'client_id', 'catalog', 'profile', 'details', 'last_sync', 'retry_at')} |
                {'followed_ids': sorted(self.followed_ids)})

    def http(self):
        return httpx.AsyncClient(timeout=30, transport=self.transport, follow_redirects=False)

    @property
    def syncing(self):
        return self.task is not None and not self.task.done()

    def status(self):
        return {'connected': bool(self.tokens), 'client_id': self.client_id,
                'redirect_uri': REDIRECT_URI, 'syncing': self.syncing,
                'last_sync': self.last_sync, 'retry_at': self.retry_at,
                'error': self.error, 'progress': self.progress, 'shows': self.catalog,
                'profile': self.profile, 'followed_ids': sorted(self.followed_ids)}

    async def exchange(self, body):
        try:
            async with self.http() as client:
                response = await client.post('https://accounts.spotify.com/api/token', data=body)
            if response.status_code != 200:
                if body['grant_type'] == 'refresh_token' and response.status_code in (400, 401):
                    self.tokens = {}
                    self.persist()
                raise SpotifyError('Spotify could not authorize this session. Check the Client ID and redirect URL, then connect again.')
            data = response.json()
            if not isinstance(data.get('access_token'), str) or not isinstance(data.get('expires_in'), (int, float)):
                raise ValueError('Invalid token response')
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise SpotifyError('Could not reach Spotify securely or read its response. Check your connection and try again.') from exc

    async def access_token(self, force=False):
        async with self.token_lock:
            return await self._access_token(force)

    async def _access_token(self, force=False):
        if not self.tokens:
            raise SpotifyError('Your Spotify session has ended. Connect again.')
        if force or self.tokens['expires_at'] <= time.time() + 60:
            if not self.tokens.get('refresh_token'):
                self.tokens = {}
                self.persist()
                raise SpotifyError('Your Spotify session has ended. Connect again.')
            generation = self.generation
            data = await self.exchange({'grant_type': 'refresh_token', 'refresh_token': self.tokens['refresh_token'], 'client_id': self.client_id})
            if generation != self.generation:
                raise SpotifyError('Your session changed. Sign in again.')
            self.tokens.update(data)
            self.tokens['expires_at'] = time.time() + data['expires_in']
            self.persist()
        return self.tokens['access_token']

    async def get(self, client, url, method='GET'):
        if self.retry_at > time.time():
            raise SpotifyError('Spotify is rate-limiting requests. Please try again after the displayed retry time.')
        token = await self.access_token()
        response = await client.request(method, url, headers={'Authorization': 'Bearer ' + token})
        if response.status_code == 401:
            token = await self.access_token(force=True)
            response = await client.request(method, url, headers={'Authorization': 'Bearer ' + token})
        if response.status_code == 429:
            try:
                delay = max(1, int(response.headers.get('Retry-After', '60')))
            except ValueError:
                delay = 60
            self.retry_at = time.time() + delay
            self.persist()
            raise SpotifyError(f'Spotify rate limit reached. Try again in {delay} seconds; your previous snapshot is preserved.')
        if response.status_code == 403:
            raise SpotifyError('Spotify denied access. Check your app’s Development Mode access, Premium requirement, allowed users, and library permission.')
        if response.status_code == 401:
            self.tokens = {}
            self.persist()
            raise SpotifyError('Spotify authorization expired. Connect again.')
        if response.status_code != 200:
            raise SpotifyError(f'Spotify returned HTTP {response.status_code}. Try syncing again later.')
        return response.json() if response.content else {}

    async def pages(self, client, path):
        url = 'https://api.spotify.com/v1/' + path + '?limit=50'
        expected_path = urlparse(url).path
        seen = set()
        while url:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or parsed.netloc != 'api.spotify.com' or parsed.path != expected_path or url in seen or len(seen) >= 10000:
                raise SpotifyError('Spotify returned unexpected pagination. Sync stopped without replacing your previous data.')
            seen.add(url)
            data = await self.get(client, url)
            if not isinstance(data.get('items'), list):
                raise SpotifyError('Spotify returned an unexpected catalog response.')
            yield data
            url = data.get('next')

    async def show_details(self, client, show):
        episodes = {}; expected = 0; missing = 0
        async for page in self.pages(client, f"shows/{show['id']}/episodes"):
            expected = max(expected, page.get('total', 0))
            for episode in page['items']:
                if not isinstance(episode, dict) or not episode.get('id'):
                    missing += 1; continue
                ms = episode.get('duration_ms')
                if not isinstance(ms, int) or isinstance(ms, bool) or ms < 0:
                    missing += 1; continue
                resume = episode.get('resume_point')
                listened = None
                if isinstance(resume, dict):
                    if resume.get('fully_played') is True:
                        listened = ms
                    elif resume.get('fully_played') is False and isinstance(resume.get('resume_position_ms'), int):
                        listened = max(0, min(ms, resume['resume_position_ms']))
                episodes[episode['id']] = {'id':episode['id'], 'name':episode.get('name','Untitled episode'),
                    'duration_ms':ms, 'listened_ms':listened,
                    'remaining_ms':ms-listened if listened is not None else None,
                    'url':'https://open.spotify.com/episode/'+episode['id'],
                    'date':episode.get('release_date','')}
        reported = show.get('total_episodes'); count = len(episodes)
        total = sum(e['duration_ms'] for e in episodes.values())
        unknown = sum(e['listened_ms'] is None for e in episodes.values())
        listened = sum(e['listened_ms'] for e in episodes.values()) if not unknown else None
        result = dict(show_summary(show), retrieved_episodes=count, duration_ms=total,
            partial=bool(missing or count != expected or (reported is not None and count != reported)),
            listened_ms=listened, remaining_ms=total-listened if listened is not None else None,
            progress_unknown=unknown, episodes=list(episodes.values()), fetched_at=time.time())
        return result

    async def sync(self):
        generation = self.generation
        self.error = ''; self.progress = 'Fetching your podcasts…'
        self.library_changes = {}
        try:
            shows = {}
            async with self.http() as client:
                profile = await self.get(client, 'https://api.spotify.com/v1/me')
                self.profile = {'name':profile.get('display_name') or profile.get('id') or 'Spotify listener',
                    'image':safe_image(profile.get('images')), 'url':safe_link(profile.get('external_urls',{}).get('spotify')),
                    'product':profile.get('product')}
                async for page in self.pages(client, 'me/shows'):
                    for item in page['items']:
                        show = item.get('show') if isinstance(item, dict) else None
                        if not show or not re.fullmatch(r'[A-Za-z0-9]+', str(show.get('id', ''))):
                            raise SpotifyError('Spotify returned an unavailable saved show. Try syncing again later.')
                        shows[show['id']] = show
                self.followed_ids = set(shows)
                for key, followed in self.library_changes.items():
                    if followed:
                        self.followed_ids.add(key)
                    else:
                        self.followed_ids.discard(key)
                catalog = []
                for i, show in enumerate(shows.values(), 1):
                    self.progress = f'Reading podcast {i} of {len(shows)}…'
                    detail = await self.show_details(client, show)
                    if self.generation != generation:
                        return
                    self.details[show['id']] = detail
                    catalog.append({k:v for k,v in detail.items() if k != 'episodes'})
            if self.generation == generation:
                # A show followed while this sync was in flight must not disappear.
                existing = {s['id'] for s in catalog}
                for key in self.followed_ids - existing:
                    if key in self.details:
                        catalog.append({k:v for k,v in self.details[key].items() if k != 'episodes'})
                self.catalog = sorted((s for s in catalog if s['id'] in self.followed_ids), key=lambda s: s['name'].casefold())
                self.last_sync = time.time(); self.retry_at = 0
                self.persist()
        except asyncio.CancelledError:
            raise
        except (SpotifyError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
            if self.generation == generation:
                self.error = str(exc) if isinstance(exc, SpotifyError) else 'Spotify could not complete the sync. Try again later.'
        finally:
            if self.generation == generation:
                self.progress = ''

    def start_sync(self):
        if not self.tokens:
            raise HTTPException(401, 'Connect to Spotify first.')
        if self.retry_at > time.time():
            raise HTTPException(429, 'Spotify is rate-limiting requests. Wait until the displayed retry time.')
        if not self.syncing:
            self.task = asyncio.create_task(self.sync())

    async def disconnect(self):
        self.generation += 1
        if self.syncing:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.tokens = {}; self.pending = {}; self.catalog = []
        self.profile = None; self.details = {}; self.followed_ids = set()
        self.last_sync = None; self.retry_at = 0; self.error = ''; self.progress = ''; self.task = None
        self.persist()


session = SpotifySession(store=SessionStore(Path(os.environ.get('STATIFY_DB', Path(__file__).resolve().parents[1] / 'data' / 'statify.sqlite3'))))


class Login(BaseModel):
    client_id: str = Field(default='', max_length=100)


@router.get('/status')
def status():
    return JSONResponse(session.status(), headers={'Cache-Control': 'no-store'})


@router.post('/login')
def login(body: Login):
    client_id = body.client_id.strip() or session.client_id
    if not re.fullmatch(r'[a-fA-F0-9]{32}', client_id):
        raise HTTPException(400, 'Enter the 32-character Client ID from your Spotify app settings. Do not enter a client secret.')
    if session.syncing:
        raise HTTPException(409, 'Wait for the current sync to finish before connecting another account.')
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    session.pending = {k: v for k, v in session.pending.items() if v['expires'] > time.time()}
    if len(session.pending) >= 10:
        raise HTTPException(429, 'Too many sign-in attempts. Wait ten minutes and try again.')
    session.pending[state] = {'client_id': client_id, 'verifier': verifier, 'expires': time.time() + 600, 'generation': session.generation}
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    url = 'https://accounts.spotify.com/authorize?' + urlencode({'client_id': client_id,
        'response_type': 'code', 'redirect_uri': REDIRECT_URI, 'scope': SCOPE,
        'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge})
    response = JSONResponse({'url': url}, headers={'Cache-Control': 'no-store'})
    response.set_cookie('podscope_oauth', state, httponly=True, samesite='lax', max_age=600, path='/api/spotify')
    return response


@router.get('/callback')
async def callback(request: Request, state: str = '', code: str = '', error: str = ''):
    pending = session.pending.get(state)
    cookie = request.cookies.get('podscope_oauth', '')
    if not pending or not cookie or not hmac.compare_digest(state, cookie) or pending['expires'] <= time.time() or pending['generation'] != session.generation:
        return RedirectResponse('/?spotify=invalid_state', status_code=303, headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'})
    session.pending.pop(state, None)
    result = 'denied' if error else 'failed'
    if code and not error:
        try:
            data = await session.exchange({'grant_type': 'authorization_code', 'code': code,
                'redirect_uri': REDIRECT_URI, 'client_id': pending['client_id'], 'code_verifier': pending['verifier']})
            if pending['generation'] != session.generation:
                raise SpotifyError('Sign-in was canceled. Connect again.')
            if not set(SCOPE.split()).issubset(data.get('scope', '').split()):
                raise SpotifyError('Spotify did not grant all required permissions. Connect again to enable library, profile, and playback progress access.')
            await session.disconnect()
            session.client_id = pending['client_id']
            session.tokens = dict(data, expires_at=time.time() + data['expires_in'])
            session.persist()
            session.start_sync()
            result = 'connected'
        except SpotifyError as exc:
            session.error = str(exc)
    response = RedirectResponse('/?spotify=' + result, status_code=303, headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'})
    response.delete_cookie('podscope_oauth', path='/api/spotify')
    return response


@router.post('/sync')
async def sync_catalog():
    session.start_sync()
    return {'started': True}


@router.post('/disconnect')
async def disconnect():
    await session.disconnect()
    return {'disconnected': True}


def safe_image(images):
    for image in images or []:
        url = image.get('url') if isinstance(image, dict) else None
        if isinstance(url, str) and url.startswith('https://'):
            return url
    return None


def safe_link(url):
    return url if isinstance(url, str) and url.startswith('https://open.spotify.com/') else None


def show_summary(show):
    return {'id':show['id'], 'name':show.get('name','Untitled show'),
        'url':'https://open.spotify.com/show/'+show['id'],
        'description':show.get('description',''), 'publisher':show.get('publisher'),
        'image':safe_image(show.get('images')), 'languages':show.get('languages',[]),
        'explicit':show.get('explicit',False), 'reported_episodes':show.get('total_episodes')}


def require_connection():
    if not session.tokens:
        raise HTTPException(401, 'Sign in to Spotify first.')


def validate_id(show_id):
    if not re.fullmatch(r'[A-Za-z0-9]{1,64}', show_id):
        raise HTTPException(400, 'Invalid Spotify show ID.')


@router.get('/search')
async def search(q: str = Query(min_length=1,max_length=200), offset: int = Query(0,ge=0,le=1000)):
    require_connection()
    try:
        async with session.http() as client:
            data = await session.get(client, 'https://api.spotify.com/v1/search?'+urlencode({'q':q,'type':'show','limit':10,'offset':offset}))
        result = data.get('shows',{})
        return {'items':[dict(show_summary(show), followed=show['id'] in session.followed_ids) for show in result.get('items',[]) if show],
            'more':bool(result.get('next')) and offset < 1000, 'offset':offset}
    except (SpotifyError,httpx.HTTPError,ValueError) as exc:
        raise HTTPException(502, str(exc) if isinstance(exc,SpotifyError) else 'Could not search Spotify. Try again.')


@router.get('/shows/{show_id}')
async def detail(show_id: str):
    require_connection(); validate_id(show_id)
    cached = session.details.get(show_id)
    if cached and time.time()-cached['fetched_at'] < 900:
        return dict(cached, followed=show_id in session.followed_ids)
    generation = session.generation
    try:
        async with session.http() as client:
            show = await session.get(client, 'https://api.spotify.com/v1/shows/'+show_id)
            result = await session.show_details(client,show)
        if generation != session.generation:
            raise HTTPException(409, 'Your Spotify account changed. Open the podcast again.')
        session.details[show_id] = result
        session.persist()
        return dict(result,followed=show_id in session.followed_ids)
    except (SpotifyError,httpx.HTTPError,ValueError) as exc:
        raise HTTPException(502,str(exc) if isinstance(exc,SpotifyError) else 'Could not load this podcast. Try again.')


@router.post('/shows/{show_id}/follow')
async def follow(show_id: str):
    require_connection(); validate_id(show_id)
    generation = session.generation
    try:
        async with session.http() as client:
            await session.get(client, 'https://api.spotify.com/v1/me/library?'+urlencode({'uris':'spotify:show:'+show_id}),method='PUT')
        if generation != session.generation:
            raise HTTPException(409, 'The Spotify account changed during the request. Refresh to check its library.')
        session.followed_ids.add(show_id)
        session.library_changes[show_id] = True
        cached = session.details.get(show_id)
        if cached and not any(s['id']==show_id for s in session.catalog):
            session.catalog.append({k:v for k,v in cached.items() if k!='episodes'})
            session.catalog.sort(key=lambda s:s['name'].casefold())
        elif not cached:
            session.start_sync()
        session.persist()
        return {'followed':True}
    except (SpotifyError,httpx.HTTPError,ValueError) as exc:
        raise HTTPException(502,str(exc) if isinstance(exc,SpotifyError) else 'Could not follow this podcast. Try again.')


@router.post('/shows/{show_id}/unfollow')
async def unfollow(show_id: str):
    require_connection(); validate_id(show_id)
    generation = session.generation
    try:
        async with session.http() as client:
            await session.get(client, 'https://api.spotify.com/v1/me/library?'+urlencode({'uris':'spotify:show:'+show_id}), method='DELETE')
        if generation != session.generation:
            raise HTTPException(409, 'The Spotify account changed during the request. Refresh to check its library.')
        session.followed_ids.discard(show_id)
        session.library_changes[show_id] = False
        session.catalog = [show for show in session.catalog if show['id'] != show_id]
        session.persist()
        return {'followed':False}
    except (SpotifyError,httpx.HTTPError,ValueError) as exc:
        raise HTTPException(502,str(exc) if isinstance(exc,SpotifyError) else 'Could not unfollow this podcast. Try again.')
