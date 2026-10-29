"""Statify Spotify application."""
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .spotify import router

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title='Statify', version='0.3.0')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

@app.middleware('http')
async def local_mutations(request: Request, call_next):
    if request.method in ('POST','PUT','DELETE') and not any(request.headers.get(name) == 'local' for name in ('x-statify', 'x-podscope')):
        return JSONResponse({'detail': 'Request verification failed. Refresh Statify and try again.'}, status_code=403)
    return await call_next(request)

@app.get('/api/health')
def health():
    return {'status':'ok'}

app.include_router(router)
DIST = ROOT / 'frontend' / 'dist'
if DIST.exists():
    app.mount('/',StaticFiles(directory=DIST,html=True),name='frontend')
