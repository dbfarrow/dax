import json
import pytest
from dax_creds.providers.auggie import AuggieProvider


class FakeKeyring:
    def __init__(self, stored=None):
        self._stored = stored or {}

    def get_password(self, service, name):
        return self._stored.get((service, name))

    def set_password(self, service, name, value):
        self._stored[(service, name)] = value


def test_check_returns_true_when_token_in_keychain():
    kr = FakeKeyring({('dax-creds', 'auggie-work'): '{"accessToken":"tok"}'})
    assert AuggieProvider(keyring=kr).check({}, 'auggie-work') is True


def test_check_returns_false_when_token_not_in_keychain():
    assert AuggieProvider(keyring=FakeKeyring()).check({}, 'auggie-work') is False


def test_store_saves_to_keychain():
    kr = FakeKeyring()
    AuggieProvider(keyring=kr).store('auggie-work', '{"accessToken":"tok"}')
    assert kr.get_password('dax-creds', 'auggie-work') == '{"accessToken":"tok"}'


def test_get_token_returns_from_keychain():
    kr = FakeKeyring({('dax-creds', 'auggie-work'): '{"accessToken":"tok"}'})
    assert AuggieProvider(keyring=kr).get_token('auggie-work') == '{"accessToken":"tok"}'


def test_import_from_disk_returns_full_session_json(tmp_path):
    session = tmp_path / 'session.json'
    payload = {'accessToken': 'tok', 'scopes': ['email'], 'tenantURL': 'https://acme.augmentcode.com'}
    session.write_text(json.dumps(payload))
    result = AuggieProvider(keyring=FakeKeyring()).import_from_disk({}, session_file=session)
    assert json.loads(result) == payload


def test_import_from_disk_returns_none_when_file_missing(tmp_path):
    result = AuggieProvider(keyring=FakeKeyring()).import_from_disk({}, session_file=tmp_path / 'session.json')
    assert result is None


def test_import_from_disk_returns_none_when_no_access_token(tmp_path):
    session = tmp_path / 'session.json'
    session.write_text('{"tenantURL": "https://acme.augmentcode.com"}')
    result = AuggieProvider(keyring=FakeKeyring()).import_from_disk({}, session_file=session)
    assert result is None


def test_has_disk_copy_returns_true_when_file_exists(tmp_path):
    session = tmp_path / 'session.json'
    session.write_text('{"accessToken":"tok"}')
    assert AuggieProvider(keyring=FakeKeyring()).has_disk_copy({}, session_file=session) is True


def test_has_disk_copy_returns_false_when_file_missing(tmp_path):
    assert AuggieProvider(keyring=FakeKeyring()).has_disk_copy({}, session_file=tmp_path / 'session.json') is False


def test_has_disk_copy_returns_false_when_file_empty(tmp_path):
    session = tmp_path / 'session.json'
    session.write_text('')
    assert AuggieProvider(keyring=FakeKeyring()).has_disk_copy({}, session_file=session) is False


def test_has_disk_copy_returns_false_when_no_access_token(tmp_path):
    session = tmp_path / 'session.json'
    session.write_text('{"tenantURL":"https://acme.augmentcode.com","scopes":["email"]}')
    assert AuggieProvider(keyring=FakeKeyring()).has_disk_copy({}, session_file=session) is False
