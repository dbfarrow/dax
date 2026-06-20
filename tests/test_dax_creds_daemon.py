import json
import pytest
from dax_creds.daemon import handle_request, FileTokenStore


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
    exchanger = FakeTokenExchanger('access-xyz', '2026-06-19T15:00:00Z')

    response = handle_request(
        {'credential': 'github-dfarrow'},
        credentials,
        token_store,
        exchanger,
        {},
    )

    assert response['token'] == 'access-xyz'
    assert response['expires_at'] == '2026-06-19T15:00:00Z'


def test_handle_request_returns_cached_token_without_exchange():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({'github-dfarrow': 'refresh-abc'})
    exchanger = FakeTokenExchanger('new-token', '2026-06-19T16:00:00Z')
    cache = {'github-dfarrow': {'token': 'cached-token', 'expires_at': '2099-01-01T00:00:00Z'}}

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
    exchanger = FakeTokenExchanger('new-token', '2099-01-01T00:00:00Z')
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
