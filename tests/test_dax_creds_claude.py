import pytest
from dax_creds.providers.claude import ClaudeProvider


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


def test_store_saves_to_keychain():
    kr = FakeKeyring()
    provider = ClaudeProvider(keyring=kr)
    provider.store('claude-work', 'sk-ant-api03-abc')
    assert kr.get_password('dax-creds', 'claude-work') == 'sk-ant-api03-abc'


def test_get_token_returns_from_keychain():
    kr = FakeKeyring({('dax-creds', 'claude-work'): 'sk-ant-api03-abc'})
    provider = ClaudeProvider(keyring=kr)
    assert provider.get_token('claude-work') == 'sk-ant-api03-abc'


def test_import_from_disk_returns_full_credentials_json(tmp_path):
    import json
    creds = tmp_path / 'credentials.json'
    payload = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc', 'refreshToken': 'sk-ant-ort01-xyz'}}
    creds.write_text(json.dumps(payload))
    provider = ClaudeProvider(keyring=FakeKeyring())
    result = provider.import_from_disk({}, credentials_file=creds)
    assert json.loads(result) == payload


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
