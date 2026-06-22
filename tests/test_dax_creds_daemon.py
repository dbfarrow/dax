import json
import pytest
from dax_creds.daemon import handle_request, handle_open_url, FileTokenStore, KeyringTokenStore


class FakeKeyring:
    def __init__(self, stored=None):
        self._stored = stored or {}

    def get_password(self, service, name):
        return self._stored.get((service, name))


def test_keyring_store_returns_token():
    kr = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_abc123'})
    store = KeyringTokenStore(keyring=kr)
    assert store.get_refresh_token('github-dfarrow') == 'ghp_abc123'


def test_keyring_store_raises_key_error_when_missing():
    kr = FakeKeyring()
    store = KeyringTokenStore(keyring=kr)
    with pytest.raises(KeyError, match='github-dfarrow'):
        store.get_refresh_token('github-dfarrow')


def test_handle_request_with_keyring_store():
    kr = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_abc123'})
    store = KeyringTokenStore(keyring=kr)
    credentials = {'github-dfarrow': {'provider': 'github'}}
    exchanger = FakeTokenExchanger('ghp_abc123', '2099-01-01T00:00:00+00:00')

    response = handle_request({'credential': 'github-dfarrow'}, credentials, store, exchanger, {})

    assert response['token'] == 'ghp_abc123'


class FakeTokenStore:
    def __init__(self, tokens):
        self._tokens = tokens

    def get_refresh_token(self, name):
        return self._tokens[name]


class FakeTokenExchanger:
    def __init__(self, access_token, expires_at):
        self._access_token = access_token
        self._expires_at = expires_at
        self.call_count = 0

    def exchange(self, provider, refresh_token):
        self.call_count += 1
        return {'token': self._access_token, 'expires_at': self._expires_at}


def test_file_token_store_returns_token(tmp_path):
    tokens_file = tmp_path / 'tokens.json'
    tokens_file.write_text(json.dumps({'github-dfarrow': 'ghp_abc123'}))

    store = FileTokenStore(tokens_file)

    assert store.get_refresh_token('github-dfarrow') == 'ghp_abc123'


def test_file_token_store_raises_for_unknown_credential(tmp_path):
    tokens_file = tmp_path / 'tokens.json'
    tokens_file.write_text(json.dumps({'github-dfarrow': 'ghp_abc123'}))

    store = FileTokenStore(tokens_file)

    with pytest.raises(KeyError, match='claude-work'):
        store.get_refresh_token('claude-work')


def test_handle_request_returns_error_when_token_missing_from_keychain():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({})  # empty — KeyError on get

    response = handle_request(
        {'credential': 'github-dfarrow'},
        credentials,
        token_store,
        FakeTokenExchanger('', ''),
        {},
    )

    assert response['error'] == 'not_in_keychain'
    assert 'dax creds login' in response['message']


def test_handle_request_rejects_undeclared_credential():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({})

    response = handle_request(
        {'credential': 'claude-work'},
        credentials,
        token_store,
        FakeTokenExchanger('', ''),
        {},
    )

    assert response['error'] == 'not_configured'
    assert 'claude-work' in response['message']


def test_handle_request_returns_token_for_declared_credential():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({'github-dfarrow': 'refresh-abc'})
    exchanger = FakeTokenExchanger('access-xyz', '2026-06-19T15:00:00+00:00')

    response = handle_request(
        {'credential': 'github-dfarrow'},
        credentials,
        token_store,
        exchanger,
        {},
    )

    assert response['token'] == 'access-xyz'
    assert response['expires_at'] == '2026-06-19T15:00:00+00:00'


def test_handle_request_returns_cached_token_without_exchange():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({'github-dfarrow': 'refresh-abc'})
    exchanger = FakeTokenExchanger('new-token', '2026-06-19T16:00:00+00:00')
    cache = {'github-dfarrow': {'token': 'cached-token', 'expires_at': '2099-01-01T00:00:00+00:00'}}

    response = handle_request(
        {'credential': 'github-dfarrow'},
        credentials,
        token_store,
        exchanger,
        cache,
    )

    assert response['token'] == 'cached-token'
    assert exchanger.call_count == 0


def test_handle_request_refreshes_expired_cache_entry():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({'github-dfarrow': 'refresh-abc'})
    exchanger = FakeTokenExchanger('new-token', '2099-01-01T00:00:00+00:00')
    cache = {'github-dfarrow': {'token': 'stale-token', 'expires_at': '2000-01-01T00:00:00Z'}}

    response = handle_request(
        {'credential': 'github-dfarrow'},
        credentials,
        token_store,
        exchanger,
        cache,
    )

    assert response['token'] == 'new-token'
    assert exchanger.call_count == 1


def test_handle_open_url_calls_opener_with_browser_and_profile():
    credentials = {'github-dfarrow': {'provider': 'github', 'browser': 'chrome', 'chrome_profile': 'Profile 1'}}
    opened = []
    response = handle_open_url(
        {'action': 'open_url', 'credential': 'github-dfarrow',
         'url': 'https://github.com/login/device'},
        credentials,
        lambda url, browser, profile: opened.append((url, browser, profile)),
    )
    assert response == {'ok': True}
    assert opened == [('https://github.com/login/device', 'chrome', 'Profile 1')]


def test_handle_open_url_passes_default_browser_when_not_configured():
    credentials = {'github-dfarrow': {'provider': 'github', 'browser': 'firefox'}}
    opened = []
    handle_open_url(
        {'action': 'open_url', 'credential': 'github-dfarrow',
         'url': 'https://github.com/login/device'},
        credentials,
        lambda url, browser, profile: opened.append((url, browser, profile)),
    )
    assert opened == [('https://github.com/login/device', 'firefox', None)]


def test_handle_open_url_returns_error_when_url_missing():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    response = handle_open_url(
        {'action': 'open_url', 'credential': 'github-dfarrow'},
        credentials,
        lambda url, browser, profile: None,
    )
    assert response['error'] == 'missing_url'


def test_legacy_get_request_without_action_field_still_works():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({'github-dfarrow': 'refresh-abc'})
    exchanger = FakeTokenExchanger('access-xyz', '2099-01-01T00:00:00+00:00')
    response = handle_request(
        {'credential': 'github-dfarrow'},  # no 'action' field
        credentials, token_store, exchanger, {},
    )
    assert response['token'] == 'access-xyz'
