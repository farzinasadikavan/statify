from fastapi.testclient import TestClient
from backend.main import app

def test_import_feature_removed():
    with TestClient(app) as client:
        assert client.get('/api/health').json() == {'status':'ok'}
        assert client.post('/api/import',headers={'X-Podscope':'local'}).status_code in (404,405)
        assert client.get('/api/dashboard').status_code == 404
