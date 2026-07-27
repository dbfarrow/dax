import json
from pathlib import Path

try:
    import keyring as _keyring
    _HAS_KEYRING = True
except ImportError:
    _keyring = None
    _HAS_KEYRING = False

_KEYCHAIN_SERVICE = 'dax-creds'


def _claude_dir():
    return Path.home() / '.claude'


def _default_credentials_json():
    # Claude Code reads the dot-prefixed name. The non-dot name is ignored.
    return _claude_dir() / '.credentials.json'


def _legacy_credentials_json():
    # dax wrote this name until 2026-07-27; Claude Code never read it.
    return _claude_dir() / 'credentials.json'


def _default_disk_locations():
    return [
        Path.home() / '.anthropic' / 'api_key',
        _default_credentials_json(),
        _legacy_credentials_json(),
    ]


def is_oauth_envelope(value):
    """True if value is a Claude Code credentials blob Claude Code can refresh.

    A bare access token is rejected: it authenticates until expiry and then
    fails with no way to recover, which is worse than failing immediately.
    """
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    oauth = data.get('claudeAiOauth')
    return isinstance(oauth, dict) and bool(oauth.get('refreshToken'))


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
        if credentials_file is not None:
            candidates = [Path(credentials_file)]
        else:
            candidates = [_default_credentials_json(), _legacy_credentials_json()]
        for path in candidates:
            if not path.exists():
                continue
            try:
                blob = path.read_text()
            except OSError:
                continue
            if is_oauth_envelope(blob):
                return json.dumps(json.loads(blob))
        return None

    def has_disk_copy(self, credential_def, disk_locations=None):
        locations = disk_locations if disk_locations is not None else _default_disk_locations()
        return any(p.exists() and p.stat().st_size > 0 for p in locations)

    def store(self, credential_name, token):
        self._require_keyring()
        if not is_oauth_envelope(token):
            raise ValueError(
                'refusing to store a Claude credential that is not a full '
                'credentials blob with a refreshToken — Claude Code could not '
                'refresh it. Expected {"claudeAiOauth": {..., "refreshToken": ...}}'
            )
        self._keyring.set_password(_KEYCHAIN_SERVICE, credential_name, token)

    def get_token(self, credential_name):
        self._require_keyring()
        return self._keyring.get_password(_KEYCHAIN_SERVICE, credential_name)
