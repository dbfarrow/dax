from pathlib import Path

try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _keyring = None
    _HAS_KEYRING = False

_KEYCHAIN_SERVICE = 'dax-creds'


def _default_session_json():
    return Path.home() / '.augment' / 'session.json'


class AuggieProvider:
    def __init__(self, keyring=None):
        self._keyring = keyring or _keyring

    def _require_keyring(self):
        if self._keyring is None:
            raise ImportError('keyring package required: pip install keyring')

    def check(self, credential_def, credential_name):
        self._require_keyring()
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name) is not None

    def import_from_disk(self, credential_def, session_file=None):
        path = Path(session_file) if session_file else _default_session_json()
        if not path.exists():
            return None
        try:
            import json
            with open(path) as f:
                data = json.load(f)
            if 'accessToken' not in data:
                return None
            return json.dumps(data)
        except Exception:
            return None

    def has_disk_copy(self, credential_def, session_file=None):
        path = Path(session_file) if session_file else _default_session_json()
        if not path.exists() or path.stat().st_size == 0:
            return False
        try:
            import json
            with open(path) as f:
                data = json.load(f)
            return 'accessToken' in data
        except Exception:
            return False

    def store(self, credential_name, token):
        self._require_keyring()
        self._keyring.set_password(_KEYCHAIN_SERVICE, credential_name, token)

    def get_token(self, credential_name):
        self._require_keyring()
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name)
