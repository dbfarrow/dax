import json
import os
import socket


class CredentialNotAvailable(Exception):
    pass


def _send(sock_path, payload):
    if sock_path.startswith('tcp:'):
        _, host, port_str = sock_path.split(':', 2)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.connect((host, int(port_str)))
            sock.sendall(json.dumps(payload).encode())
            return json.loads(sock.recv(4096))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(sock_path)
        sock.sendall(json.dumps(payload).encode())
        return json.loads(sock.recv(4096))


def get_token(credential_name):
    sock_path = os.environ.get('DAX_CREDS_SOCK')
    if not sock_path:
        raise CredentialNotAvailable('DAX_CREDS_SOCK is not set')
    response = _send(sock_path, {'credential': credential_name})
    if 'error' in response:
        raise CredentialNotAvailable(response.get('message', response['error']))
    return response['token']


def open_url(credential_name, url):
    sock_path = os.environ.get('DAX_CREDS_SOCK')
    if not sock_path:
        raise CredentialNotAvailable('DAX_CREDS_SOCK is not set')
    response = _send(sock_path, {'action': 'open_url', 'credential': credential_name, 'url': url})
    if 'error' in response:
        raise CredentialNotAvailable(response.get('message', response['error']))
    return response.get('ok', False)
