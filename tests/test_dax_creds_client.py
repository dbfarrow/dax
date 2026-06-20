import json
import socket
import threading
import pytest
from dax_creds.client import get_token, CredentialNotAvailable


def _serve_once(sock_path, response):
    """Spin up a one-shot Unix socket server that returns response to one request."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(json.dumps(response).encode())
        conn.close()
        server.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t


def test_get_token_raises_when_sock_not_set(monkeypatch):
    monkeypatch.delenv('DAX_CREDS_SOCK', raising=False)

    with pytest.raises(CredentialNotAvailable):
        get_token('github-dfarrow')


def test_get_token_returns_token_from_socket(tmp_path, monkeypatch):
    sock_path = str(tmp_path / 'creds.sock')
    monkeypatch.setenv('DAX_CREDS_SOCK', sock_path)
    _serve_once(sock_path, {'token': 'access-xyz', 'expires_at': '2099-01-01T00:00:00Z'})

    token = get_token('github-dfarrow')

    assert token == 'access-xyz'


def test_get_token_raises_on_error_response(tmp_path, monkeypatch):
    sock_path = str(tmp_path / 'creds.sock')
    monkeypatch.setenv('DAX_CREDS_SOCK', sock_path)
    _serve_once(sock_path, {'error': 'not_configured', 'message': 'No credential named github-dfarrow'})

    with pytest.raises(CredentialNotAvailable, match='No credential named github-dfarrow'):
        get_token('github-dfarrow')
