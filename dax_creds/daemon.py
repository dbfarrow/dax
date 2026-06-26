import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import keyring as _keyring_module
    _HAS_KEYRING = True
except ImportError:
    _keyring_module = None
    _HAS_KEYRING = False

_KEYCHAIN_SERVICE = 'dax-creds'


class KeyringTokenStore:
    def __init__(self, keyring=None):
        self._keyring = keyring or _keyring_module
        if self._keyring is None:
            raise ImportError('keyring package required: pip install keyring')

    def get_refresh_token(self, name):
        token = self._keyring.get_password(_KEYCHAIN_SERVICE, name)
        if token is None:
            raise KeyError(f'No token for {name!r} in Keychain')
        return token


class FileTokenStore:
    def __init__(self, path):
        self._path = Path(path)

    def get_refresh_token(self, name):
        with open(self._path) as f:
            tokens = json.load(f)
        if name not in tokens:
            raise KeyError(f'No token for {name} in {self._path}')
        return tokens[name]


class PassthroughExchanger:
    def exchange(self, provider, refresh_token, cred_def=None):
        return {'token': refresh_token, 'expires_at': '2099-01-01T00:00:00+00:00'}


class GoogleTokenExchanger:
    _TOKEN_URL = 'https://oauth2.googleapis.com/token'

    def exchange(self, provider, refresh_token, cred_def=None):
        import urllib.error
        import urllib.parse
        import urllib.request
        cred_def = cred_def or {}
        client_id = cred_def.get('client_id')
        client_secret = cred_def.get('client_secret')
        if not client_id or not client_secret:
            return {
                'error': 'missing_client_credentials',
                'message': f'client_id and client_secret required for {provider}',
            }
        body = urllib.parse.urlencode({
            'client_id': client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
            'grant_type': 'refresh_token',
        }).encode()
        req = urllib.request.Request(
            self._TOKEN_URL, data=body, method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = json.loads(e.read())
            return {
                'error': 'token_refresh_failed',
                'message': error_body.get('error_description', str(e)),
            }
        access_token = result.get('access_token')
        expires_in = result.get('expires_in', 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        return {'token': access_token, 'expires_at': expires_at.isoformat()}


class DispatchingExchanger:
    _GOOGLE_PROVIDERS = frozenset({'gmail', 'drive'})

    def __init__(self):
        self._google = GoogleTokenExchanger()
        self._passthrough = PassthroughExchanger()

    def exchange(self, provider, refresh_token, cred_def=None):
        if provider in self._GOOGLE_PROVIDERS:
            return self._google.exchange(provider, refresh_token, cred_def)
        return self._passthrough.exchange(provider, refresh_token)


def handle_open_url(request, credentials, url_opener):
    url = request.get('url')
    if not url:
        return {'error': 'missing_url', 'message': 'url is required'}
    cred_name = request.get('credential', '')
    cred_def = credentials.get(cred_name, {})
    browser = cred_def.get('browser', 'default')
    chrome_profile = cred_def.get('chrome_profile')
    url_opener(url, browser, chrome_profile)
    return {'ok': True}


def handle_request(request, credentials, token_store, token_exchanger, cache):
    name = request.get('credential')
    if name not in credentials:
        return {'error': 'not_configured', 'message': f'No credential named {name}'}
    if name in cache:
        entry = cache[name]
        expires_at = datetime.fromisoformat(entry['expires_at'].replace('Z', '+00:00'))
        if expires_at > datetime.now(timezone.utc):
            return entry
    cred_def = credentials[name]
    provider = cred_def['provider']
    try:
        refresh_token = token_store.get_refresh_token(name)
    except KeyError:
        return {'error': 'not_in_keychain', 'message': f'{name!r} not in Keychain — run: dax creds login {name}'}
    result = token_exchanger.exchange(provider, refresh_token, cred_def)
    cache[name] = result
    return result


def _default_url_opener(url, browser, chrome_profile):
    import subprocess
    if browser == 'chrome':
        from dax_creds.chrome import open_url_in_profile
        open_url_in_profile(url, chrome_profile)
    elif browser and browser != 'default':
        _app = {
            'firefox': 'Firefox',
            'safari':  'Safari',
            'arc':     'Arc',
            'brave':   'Brave Browser',
        }.get(browser, browser.title())
        subprocess.Popen(['open', '-a', _app, url])
    else:
        subprocess.Popen(['open', url])


def _make_handle(credentials, token_store, token_exchanger, cache, url_opener):
    async def handle(reader, writer):
        data = await reader.read(4096)
        try:
            request = json.loads(data)
        except json.JSONDecodeError:
            response = {'error': 'invalid_request', 'message': 'Invalid JSON'}
        else:
            action = request.get('action', 'get')
            if action == 'open_url':
                response = handle_open_url(request, credentials, url_opener)
            else:
                response = handle_request(request, credentials, token_store, token_exchanger, cache)
        writer.write(json.dumps(response).encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    return handle


async def run_daemon(socket_path, credentials, token_store, token_exchanger, url_opener=None):
    cache = {}
    handle = _make_handle(credentials, token_store, token_exchanger, cache,
                          url_opener or _default_url_opener)

    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    server = await asyncio.start_unix_server(handle, path=str(path))
    path.chmod(0o666)  # allow container user to connect across Docker uid mapping
    async with server:
        await server.serve_forever()


async def run_tcp_daemon(host, port, credentials, token_store, token_exchanger, url_opener=None):
    cache = {}
    handle = _make_handle(credentials, token_store, token_exchanger, cache,
                          url_opener or _default_url_opener)
    server = await asyncio.start_server(handle, host, port)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='dax-creds daemon')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--socket', help='Unix socket path')
    group.add_argument('--tcp-port', type=int, help='TCP port (binds 0.0.0.0)')
    parser.add_argument('--credentials', required=True, help='JSON of credential definitions')
    args = parser.parse_args()

    credentials = json.loads(args.credentials)
    token_store = KeyringTokenStore()
    exchanger = DispatchingExchanger()

    if args.tcp_port:
        asyncio.run(run_tcp_daemon('0.0.0.0', args.tcp_port, credentials, token_store, exchanger))
    else:
        asyncio.run(run_daemon(args.socket, credentials, token_store, exchanger))
