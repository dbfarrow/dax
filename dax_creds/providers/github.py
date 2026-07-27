import time
from pathlib import Path

try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _keyring = None
    _HAS_KEYRING = False

_KEYCHAIN_SERVICE = 'dax-creds'
_GH_HOSTS_FILE = Path.home() / '.config' / 'gh' / 'hosts.yml'
_DEVICE_CODE_URL = 'https://github.com/login/device/code'
_ACCESS_TOKEN_URL = 'https://github.com/login/oauth/access_token'


class GitHubProvider:
    def __init__(self, keyring=None, http=None):
        self._keyring = keyring or _keyring
        if self._keyring is None:
            raise ImportError('keyring package required: pip install keyring')
        self._http = http or _default_http

    def check(self, credential_def, credential_name):
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name) is not None

    def import_from_disk(self, credential_def, credential_name=None, hosts_file=None):
        path = Path(hosts_file) if hosts_file else _GH_HOSTS_FILE
        if not path.exists():
            return None
        if not _HAS_YAML:
            return None
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        gh = (data or {}).get('github.com') or {}

        # Flat format (older gh CLI)
        if 'oauth_token' in gh:
            return gh['oauth_token']

        # Nested format: github.com.users.<username>.oauth_token
        users = gh.get('users') or {}
        if not users:
            return None

        # Try matching by credential name (strip provider prefix)
        if credential_name:
            for prefix in ('github-', 'gh-'):
                if credential_name.startswith(prefix):
                    username = credential_name[len(prefix):]
                    if username in users:
                        token = users[username].get('oauth_token')
                        if token:
                            return token

        # Fall back to default user
        default_user = gh.get('user')
        if default_user and default_user in users:
            token = users[default_user].get('oauth_token')
            if token:
                return token

        # Return any available token
        for user_data in users.values():
            token = user_data.get('oauth_token')
            if token:
                return token

        return None

    def has_disk_copy(self, credential_def, credential_name=None):
        return self.import_from_disk(credential_def, credential_name=credential_name) is not None

    def store(self, credential_name, token):
        self._keyring.set_password(_KEYCHAIN_SERVICE, credential_name, token)

    def get_token(self, credential_name):
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name)

    def acquire(self, credential_def, credential_name, prompter=print, opener=None):
        client_id = credential_def.get('client_id')
        if not client_id:
            raise ValueError('github provider requires client_id in credential definition')

        resp = self._http('POST', _DEVICE_CODE_URL, {
            'client_id': client_id,
            'scope': 'repo read:org',
        })
        device_code = resp['device_code']
        user_code = resp['user_code']
        verification_uri = resp['verification_uri']
        interval = resp.get('interval', 5)

        prompter(f'\nGitHub authorization required.')
        if opener:
            opener(verification_uri)
        else:
            prompter(f'  1. Open: {verification_uri}')
        prompter(f'  2. Enter code: {user_code}')
        prompter(f'  Waiting for authorization...')

        expires_in = resp.get('expires_in', 900)
        deadline = time.monotonic() + expires_in
        while time.monotonic() < deadline:
            time.sleep(interval)
            poll = self._http('POST', _ACCESS_TOKEN_URL, {
                'client_id': client_id,
                'device_code': device_code,
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
            })
            if 'access_token' in poll:
                token = poll['access_token']
                self.store(credential_name, token)
                prompter(f'  Authorized. Token stored in Keychain.')
                return token
            if poll.get('error') == 'access_denied':
                raise RuntimeError('GitHub authorization denied')

        raise RuntimeError('GitHub authorization timed out')


def _default_http(method, url, data):
    import urllib.request
    import urllib.parse
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                  headers={'Accept': 'application/json'})
    with urllib.request.urlopen(req) as resp:
        import json
        return json.loads(resp.read())
