import json
import os
import socket


class CredentialNotAvailable(Exception):
    pass


def get_token(credential_name):
    sock_path = os.environ.get('DAX_CREDS_SOCK')
    if not sock_path:
        raise CredentialNotAvailable('DAX_CREDS_SOCK is not set')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(sock_path)
        sock.sendall(json.dumps({'credential': credential_name}).encode())
        response = json.loads(sock.recv(4096))
    if 'error' in response:
        raise CredentialNotAvailable(response.get('message', response['error']))
    return response['token']
