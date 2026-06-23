from pathlib import Path

try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _keyring = None
    _HAS_KEYRING = False

_KEYCHAIN_SERVICE = 'dax-creds'


def _default_disk_locations():
    home = Path.home()
    return [
        home / '.anthropic' / 'api_key',
        home / '.claude' / 'credentials.json',
    ]


def _default_credentials_json():
    return Path.home() / '.claude' / 'credentials.json'


class ClaudeProvider:
    def __init__(self, keyring=None):
        self._keyring = keyring or _keyring

    def _require_keyring(self):
        if self._keyring is None:
            raise ImportError('keyring package required: pip install keyring')

    def check(self, credential_def, credential_name):
        self._require_keyring()
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name) is not None

    def import_from_disk(self, credential_def, credentials_file=None):
        path = Path(credentials_file) if credentials_file else _default_credentials_json()
        if not path.exists():
            return None
        try:
            import json
            with open(path) as f:
                data = json.load(f)
            if 'claudeAiOauth' not in data:
                return None
            return json.dumps(data)
        except Exception:
            return None

    def has_disk_copy(self, credential_def, disk_locations=None):
        locations = disk_locations if disk_locations is not None else _default_disk_locations()
        return any(p.exists() and p.stat().st_size > 0 for p in locations)

    def store(self, credential_name, token):
        self._require_keyring()
        self._keyring.set_password(_KEYCHAIN_SERVICE, credential_name, token)

    def get_token(self, credential_name):
        self._require_keyring()
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name)
