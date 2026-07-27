import json
import socket
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _keyring = None
    _HAS_KEYRING = False

_KEYCHAIN_SERVICE = 'dax-creds'
_AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
_TOKEN_URL = 'https://oauth2.googleapis.com/token'

_SCOPE_ALIASES = {
    'gmail':             'https://www.googleapis.com/auth/gmail.modify',
    'gmail.readonly':    'https://www.googleapis.com/auth/gmail.readonly',
    'gmail.send':        'https://www.googleapis.com/auth/gmail.send',
    'drive':             'https://www.googleapis.com/auth/drive',
    'drive.readonly':    'https://www.googleapis.com/auth/drive.readonly',
    'drive.file':        'https://www.googleapis.com/auth/drive.file',
    'calendar':          'https://www.googleapis.com/auth/calendar',
    'calendar.readonly': 'https://www.googleapis.com/auth/calendar.readonly',
}

_DEFAULT_SCOPES = {
    'gmail': ['gmail'],
    'drive': ['drive'],
}


def _resolve_scopes(scope_list):
    return [_SCOPE_ALIASES.get(s, s) for s in (scope_list or [])]


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _make_redirect_handler(code_holder):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            code = params.get('code', [None])[0]
            code_holder[0] = code
            if code:
                body = b'<html><body><h2>Authorization complete. You can close this tab.</h2></body></html>'
                self.send_response(200)
            else:
                error = params.get('error', ['unknown'])[0]
                body = f'<html><body><h2>Authorization failed: {error}</h2></body></html>'.encode()
                self.send_response(400)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, *args):
            pass

    return Handler


def _default_http(method, url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, method=method,
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


class GoogleProvider:
    def __init__(self, provider, keyring=None, http=None):
        self._provider = provider
        self._keyring = keyring or _keyring
        if self._keyring is None:
            raise ImportError('keyring package required: pip install keyring')
        self._http = http or _default_http

    def check(self, cred_def, credential_name):
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name) is not None

    def store(self, credential_name, refresh_token):
        self._keyring.set_password(_KEYCHAIN_SERVICE, credential_name, refresh_token)

    def get_refresh_token(self, credential_name):
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name)

    def acquire(self, cred_def, credential_name, prompter=print, opener=None):
        client_id = cred_def.get('client_id')
        client_secret = cred_def.get('client_secret')
        if not client_id or not client_secret:
            raise ValueError(f'{self._provider} provider requires client_id and client_secret')

        raw_scopes = cred_def.get('scopes') or _DEFAULT_SCOPES[self._provider]
        scopes = _resolve_scopes(raw_scopes)

        port = _find_free_port()
        redirect_uri = f'http://localhost:{port}'

        params = {
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': ' '.join(scopes),
            'access_type': 'offline',
            'prompt': 'consent',
        }
        auth_url = _AUTH_URL + '?' + urllib.parse.urlencode(params)

        code_holder = [None]
        handler_class = _make_redirect_handler(code_holder)
        server = HTTPServer(('localhost', port), handler_class)

        prompter(f'\nGoogle authorization required for {credential_name}.')
        if opener:
            opener(auth_url)
        else:
            prompter(f'  Open this URL in your browser:\n  {auth_url}')
        prompter('  Waiting for authorization...')

        server.serve_forever()  # returns after handler calls server.shutdown()

        code = code_holder[0]
        if not code:
            raise RuntimeError('Authorization cancelled or failed')

        token_resp = self._http('POST', _TOKEN_URL, {
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'redirect_uri': redirect_uri,
            'grant_type': 'authorization_code',
        })

        if 'error' in token_resp:
            raise RuntimeError(
                f'Token exchange failed: {token_resp.get("error_description", token_resp["error"])}'
            )

        refresh_token = token_resp.get('refresh_token')
        if not refresh_token:
            raise RuntimeError(
                'No refresh_token returned — ensure access_type=offline and prompt=consent'
            )

        self.store(credential_name, refresh_token)
        prompter('  Authorized. Refresh token stored in Keychain.')
        return refresh_token


class GmailProvider(GoogleProvider):
    def __init__(self, **kwargs):
        super().__init__('gmail', **kwargs)


class DriveProvider(GoogleProvider):
    def __init__(self, **kwargs):
        super().__init__('drive', **kwargs)
