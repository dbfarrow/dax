import json
import socket
import threading
import pytest
from dax_creds.client import get_token, open_url, CredentialNotAvailable


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


def _serve_once_tcp(response):
    """Spin up a one-shot TCP server; returns (thread, port)."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    port = server.getsockname()[1]
    server.listen(1)
    def serve():
        conn, _ = server.accept()
        conn.recv(4096)
        conn.sendall(json.dumps(response).encode())
        conn.close()
        server.close()
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    return t, port


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


def test_open_url_sends_correct_request(tmp_path, monkeypatch):
    sock_path = str(tmp_path / 'creds.sock')
    monkeypatch.setenv('DAX_CREDS_SOCK', sock_path)
    requests_received = []

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        data = conn.recv(4096)
        requests_received.append(json.loads(data))
        conn.sendall(json.dumps({'ok': True}).encode())
        conn.close()
        server.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    open_url('github-dfarrow', 'https://github.com/login/device')
    t.join(timeout=2)

    assert requests_received[0] == {
        'action': 'open_url',
        'credential': 'github-dfarrow',
        'url': 'https://github.com/login/device',
    }


def test_open_url_raises_when_sock_not_set(monkeypatch):
    monkeypatch.delenv('DAX_CREDS_SOCK', raising=False)
    with pytest.raises(CredentialNotAvailable):
        open_url('github-dfarrow', 'https://github.com/login/device')


def test_get_token_returns_token_over_tcp(monkeypatch):
    t, port = _serve_once_tcp({'token': 'tcp-token', 'expires_at': '2099-01-01T00:00:00Z'})
    monkeypatch.setenv('DAX_CREDS_SOCK', f'tcp:127.0.0.1:{port}')

    token = get_token('github-dfarrow')

    assert token == 'tcp-token'
    t.join(timeout=2)


def test_open_url_sends_correct_request_over_tcp(monkeypatch):
    requests_received = []
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('127.0.0.1', 0))
    port = server.getsockname()[1]
    server.listen(1)

    def serve():
        conn, _ = server.accept()
        data = conn.recv(4096)
        requests_received.append(json.loads(data))
        conn.sendall(json.dumps({'ok': True}).encode())
        conn.close()
        server.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    monkeypatch.setenv('DAX_CREDS_SOCK', f'tcp:127.0.0.1:{port}')

    open_url('github-dfarrow', 'https://github.com/login/device')
    t.join(timeout=2)

    assert requests_received[0] == {
        'action': 'open_url',
        'credential': 'github-dfarrow',
        'url': 'https://github.com/login/device',
    }
