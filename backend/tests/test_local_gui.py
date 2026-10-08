import pytest
from fastapi.testclient import TestClient
from backend.app import main, auth


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setenv('YOUDUB_LOCAL_GUI', 'true')
    monkeypatch.delenv('YOUDUB_AUTH_PASSWORD_HASH', raising=False)
    return TestClient(main.app, base_url='http://127.0.0.1:8000', client=('127.0.0.1', 12345))


def test_local_session_without_password_or_cookie(local):
    response = local.get('/api/auth/session')
    assert response.status_code == 200
    assert response.json()['authenticated']
    assert response.json()['csrf_token']
    assert 'set-cookie' not in response.headers
    assert local.post('/api/auth/login', json={'password': 'unused'}).status_code == 404
    assert local.post('/api/auth/logout').status_code == 404


def test_local_mutations_require_csrf(local):
    assert local.post('/api/nonexistent').status_code == 403
    token = local.get('/api/auth/session').json()['csrf_token']
    assert local.post('/api/nonexistent', headers={'X-CSRF-Token':token}).status_code == 404


@pytest.mark.parametrize('headers', [
    {'Origin':'https://evil.example'}, {'Host':'evil.example'},
    {'Sec-Fetch-Site':'cross-site'}, {'Authorization':'Bearer worker-token'},
])
def test_other_sources_cannot_read_local_session(local, headers):
    assert local.get('/api/auth/session', headers=headers).status_code == 403


def test_non_loopback_peer_rejected(local):
    remote = TestClient(main.app, base_url='http://127.0.0.1:8000', client=('192.0.2.5', 12345))
    assert remote.get('/api/auth/session').status_code == 403


def test_worker_still_requires_token(local):
    assert local.post('/api/colab-worker/claim').status_code == 401


def test_local_gui_origin_accepted(local):
    assert local.get('/api/auth/session', headers={'Origin':'http://localhost:3000'}).status_code == 200
