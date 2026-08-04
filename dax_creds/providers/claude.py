import hashlib
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


def grant_id(value):
    """A stable identifier for the OAuth grant a credentials blob belongs to.

    The blob carries no grant ID, so the refresh token stands in for one: two
    blobs holding the same refresh token are the same grant. Hashed rather than
    compared directly so callers can log and report it without handling the
    secret. None when the value isn't a refreshable envelope.
    """
    if not is_oauth_envelope(value):
        return None
    token = json.loads(value)['claudeAiOauth']['refreshToken']
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def grant_collision_message(credential_name, other_name):
    """Why an import was refused, as lines a caller prefixes in its own style.

    Shared so `dax creds login` and `dax creds add` explain it identically — the
    two paths reached this failure by different routes and used to disagree about
    everything else.
    """
    return [
        f"refusing to import: this token is the same OAuth grant as "
        f"'{other_name}'.",
        f"Two names on one grant share a refresh-token chain, so a rotation on "
        f"either invalidates both — and nothing looks wrong until it does.",
        f"Run `dax creds login {credential_name}` and complete the browser flow "
        f"to mint an independent grant.",
    ]


class ClaudeProvider:
    def __init__(self, keyring=None):
        self._keyring = keyring or _keyring

    def grant_collision(self, credential_name, token, candidate_names):
        """The already-stored credential `token` would share a grant with, if any.

        Per-env credentials only isolate anything when each one is its own OAuth
        grant: independent grants have independent refresh-token chains, so a
        rotation in one project cannot invalidate another's. Two names backed by
        one grant authenticate perfectly and look correct — and a rotation on
        either kills both. That failure has happened for real (2026-07-30), which
        is why this is enforced at the moment of storing rather than left to the
        manual check.

        `credential_name` is skipped, so re-importing a credential's own token
        under its own name is not a collision — that is a no-op, not sharing.
        Returns None when the grant cannot be determined, or when there is no
        Keychain to compare against.
        """
        gid = grant_id(token)
        if gid is None or self._keyring is None:
            return None
        for name in sorted(candidate_names):
            if name == credential_name:
                continue
            stored = self._keyring.get_password(_KEYCHAIN_SERVICE, name)
            if stored and grant_id(stored) == gid:
                return name
        return None

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
