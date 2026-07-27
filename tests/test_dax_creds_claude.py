import json
import pytest
from dax_creds.providers.claude import (
    ClaudeProvider,
    is_oauth_envelope,
    _default_credentials_json,
    _legacy_credentials_json,
)

ENVELOPE = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc',
                              'refreshToken': 'sk-ant-ort01-xyz'}}
ENVELOPE_JSON = json.dumps(ENVELOPE)


class FakeKeyring:
    def __init__(self, stored=None):
        self._stored = stored or {}

    def get_password(self, service, name):
        return self._stored.get((service, name))

    def set_password(self, service, name, value):
        self._stored[(service, name)] = value


def test_check_returns_true_when_key_in_keychain():
    kr = FakeKeyring({('dax-creds', 'claude-work'): 'sk-ant-api03-abc'})
    provider = ClaudeProvider(keyring=kr)
    assert provider.check({}, 'claude-work') is True


def test_check_returns_false_when_key_not_in_keychain():
    kr = FakeKeyring()
    provider = ClaudeProvider(keyring=kr)
    assert provider.check({}, 'claude-work') is False


def test_store_saves_full_envelope_to_keychain():
    kr = FakeKeyring()
    provider = ClaudeProvider(keyring=kr)
    provider.store('claude-work', ENVELOPE_JSON)
    assert kr.get_password('dax-creds', 'claude-work') == ENVELOPE_JSON


def test_store_rejects_bare_access_token():
    kr = FakeKeyring()
    provider = ClaudeProvider(keyring=kr)
    with pytest.raises(ValueError):
        provider.store('claude-work', 'sk-ant-oat01-' + 'a' * 90)
    assert kr.get_password('dax-creds', 'claude-work') is None


def test_store_rejects_envelope_without_refresh_token():
    kr = FakeKeyring()
    provider = ClaudeProvider(keyring=kr)
    with pytest.raises(ValueError):
        provider.store('claude-work', json.dumps({'claudeAiOauth': {'accessToken': 'x'}}))
    assert kr.get_password('dax-creds', 'claude-work') is None


def test_get_token_returns_from_keychain():
    kr = FakeKeyring({('dax-creds', 'claude-work'): 'sk-ant-api03-abc'})
    provider = ClaudeProvider(keyring=kr)
    assert provider.get_token('claude-work') == 'sk-ant-api03-abc'


def test_import_from_disk_returns_full_credentials_json(tmp_path):
    creds = tmp_path / 'credentials.json'
    creds.write_text(ENVELOPE_JSON)
    provider = ClaudeProvider(keyring=FakeKeyring())
    result = provider.import_from_disk({}, credentials_file=creds)
    assert json.loads(result) == ENVELOPE


def test_import_from_disk_returns_none_for_bare_token(tmp_path):
    creds = tmp_path / 'credentials.json'
    creds.write_text('sk-ant-oat01-' + 'a' * 90)
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.import_from_disk({}, credentials_file=creds) is None


def test_import_from_disk_returns_none_without_refresh_token(tmp_path):
    creds = tmp_path / 'credentials.json'
    creds.write_text(json.dumps({'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc'}}))
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.import_from_disk({}, credentials_file=creds) is None


def test_import_from_disk_returns_none_when_file_missing(tmp_path):
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.import_from_disk({}, credentials_file=tmp_path / 'credentials.json') is None


def test_import_from_disk_returns_none_when_no_oauth_key(tmp_path):
    creds = tmp_path / 'credentials.json'
    creds.write_text('{"other": "data"}')
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.import_from_disk({}, credentials_file=creds) is None


def test_has_disk_copy_returns_true_for_anthropic_api_key_file(tmp_path):
    key_file = tmp_path / 'api_key'
    key_file.write_text('sk-ant-api03-abc')
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.has_disk_copy({}, disk_locations=[key_file]) is True


def test_has_disk_copy_returns_true_for_claude_credentials_json(tmp_path):
    creds_file = tmp_path / 'credentials.json'
    creds_file.write_text('{"claudeAiOauth": {"accessToken": "sk-ant-oat01-abc"}}')
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.has_disk_copy({}, disk_locations=[creds_file]) is True


def test_has_disk_copy_returns_false_when_no_files_exist(tmp_path):
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.has_disk_copy({}, disk_locations=[tmp_path / 'api_key', tmp_path / 'credentials.json']) is False


def test_has_disk_copy_returns_false_when_all_files_empty(tmp_path):
    key_file = tmp_path / 'api_key'
    key_file.write_text('')
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.has_disk_copy({}, disk_locations=[key_file]) is False


def test_has_disk_copy_returns_true_when_any_location_has_content(tmp_path):
    empty = tmp_path / 'api_key'
    empty.write_text('')
    present = tmp_path / 'credentials.json'
    present.write_text('{"claudeAiOauth": {}}')
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.has_disk_copy({}, disk_locations=[empty, present]) is True


# --- default paths -----------------------------------------------------------
# These exercise the defaults rather than injected paths. The original bug —
# dax writing and reading 'credentials.json' where Claude Code reads
# '.credentials.json' — survived because every test passed an explicit path.

def test_default_credentials_path_is_dot_prefixed():
    assert _default_credentials_json().name == '.credentials.json'


def test_default_disk_locations_include_dot_prefixed_name(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    from dax_creds.providers.claude import _default_disk_locations
    names = [p.name for p in _default_disk_locations()]
    assert '.credentials.json' in names


def test_import_from_disk_reads_dot_prefixed_file_by_default(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    claude_dir = tmp_path / '.claude'
    claude_dir.mkdir()
    (claude_dir / '.credentials.json').write_text(ENVELOPE_JSON)
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert json.loads(provider.import_from_disk({})) == ENVELOPE


def test_import_from_disk_falls_back_to_legacy_name(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    claude_dir = tmp_path / '.claude'
    claude_dir.mkdir()
    (claude_dir / 'credentials.json').write_text(ENVELOPE_JSON)
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert json.loads(provider.import_from_disk({})) == ENVELOPE


def test_import_from_disk_prefers_dot_prefixed_over_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    claude_dir = tmp_path / '.claude'
    claude_dir.mkdir()
    (claude_dir / '.credentials.json').write_text(ENVELOPE_JSON)
    (claude_dir / 'credentials.json').write_text(
        json.dumps({'claudeAiOauth': {'accessToken': 'stale', 'refreshToken': 'stale'}}))
    provider = ClaudeProvider(keyring=FakeKeyring())
    result = json.loads(provider.import_from_disk({}))
    assert result['claudeAiOauth']['refreshToken'] == 'sk-ant-ort01-xyz'


def test_import_from_disk_skips_unparseable_legacy_file(monkeypatch, tmp_path):
    monkeypatch.setenv('HOME', str(tmp_path))
    claude_dir = tmp_path / '.claude'
    claude_dir.mkdir()
    (claude_dir / 'credentials.json').write_text('sk-ant-oat01-' + 'a' * 90)
    provider = ClaudeProvider(keyring=FakeKeyring())
    assert provider.import_from_disk({}) is None


# --- envelope validation -----------------------------------------------------

@pytest.mark.parametrize('value', [
    'sk-ant-oat01-abc',
    '',
    'null',
    '[]',
    '{}',
    '{"other": "data"}',
    '{"claudeAiOauth": "not-a-dict"}',
    '{"claudeAiOauth": {"accessToken": "x"}}',
    '{"claudeAiOauth": {"accessToken": "x", "refreshToken": ""}}',
])
def test_is_oauth_envelope_rejects(value):
    assert is_oauth_envelope(value) is False


def test_is_oauth_envelope_accepts_full_blob():
    assert is_oauth_envelope(ENVELOPE_JSON) is True


def test_is_oauth_envelope_rejects_none():
    assert is_oauth_envelope(None) is False
