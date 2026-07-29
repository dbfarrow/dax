import asyncio
import json
import pytest
from dax_creds.daemon import handle_request, handle_open_url, handle_list, FileTokenStore, KeyringTokenStore, _make_handle


class FakeReader:
    def __init__(self, data):
        self._data = data

    async def read(self, n):
        return self._data


class FakeWriter:
    def __init__(self):
        self.written = b''
        self.closed = False

    def write(self, data):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


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

    def exchange(self, provider, refresh_token, cred_def=None):
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


def test_handle_open_url_rewrites_claude_localhost_redirect_to_device_flow():
    credentials = {'claude-fre': {'provider': 'claude', 'browser': 'chrome', 'chrome_profile': 'Profile 2'}}
    opened = []
    localhost_url = (
        'https://claude.com/cai/oauth/authorize?code=true&client_id=abc'
        '&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A43775%2Fcallback'
        '&scope=user%3Aprofile&code_challenge=xyz&code_challenge_method=S256&state=stateval'
    )

    response = handle_open_url(
        {'action': 'open_url', 'credential': 'claude-fre', 'url': localhost_url},
        credentials,
        lambda url, browser, profile: opened.append((url, browser, profile)),
    )

    assert response == {'ok': True}
    opened_url, browser, profile = opened[0]
    assert browser == 'chrome'
    assert profile == 'Profile 2'
    assert 'redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback' in opened_url
    # Everything else about the authorization request is preserved untouched.
    assert 'client_id=abc' in opened_url
    assert 'code_challenge=xyz' in opened_url
    assert 'code_challenge_method=S256' in opened_url
    assert 'state=stateval' in opened_url


def test_handle_open_url_leaves_claude_non_localhost_redirect_untouched():
    credentials = {'claude-fre': {'provider': 'claude'}}
    opened = []
    already_fine_url = (
        'https://claude.com/cai/oauth/authorize?redirect_uri='
        'https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&state=stateval'
    )

    handle_open_url(
        {'action': 'open_url', 'credential': 'claude-fre', 'url': already_fine_url},
        credentials,
        lambda url, browser, profile: opened.append(url),
    )

    assert opened == [already_fine_url]


def test_handle_open_url_does_not_rewrite_non_claude_localhost_redirect():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    opened = []
    localhost_url = 'https://github.com/login/device?redirect_uri=http%3A%2F%2Flocalhost%3A9999%2Fcallback'

    handle_open_url(
        {'action': 'open_url', 'credential': 'github-dfarrow', 'url': localhost_url},
        credentials,
        lambda url, browser, profile: opened.append(url),
    )

    assert opened == [localhost_url]


def test_handle_closes_connection_silently_on_empty_read(caplog):
    # A bare connect-then-disconnect (e.g. dax.py's _wait_for_tcp readiness
    # probe) must not be logged as a malformed request - there's no client
    # data to have been invalid in the first place.
    handle = _make_handle(
        credentials={}, token_store=None, token_exchanger=None, cache={},
        url_opener=lambda url, browser, profile: None,
    )
    reader = FakeReader(b'')
    writer = FakeWriter()

    asyncio.run(handle(reader, writer))

    assert writer.written == b''
    assert writer.closed is True
    assert 'invalid JSON' not in caplog.text


def test_handle_still_logs_and_responds_to_genuinely_invalid_json():
    handle = _make_handle(
        credentials={}, token_store=None, token_exchanger=None, cache={},
        url_opener=lambda url, browser, profile: None,
    )
    reader = FakeReader(b'not json')
    writer = FakeWriter()

    asyncio.run(handle(reader, writer))

    response = json.loads(writer.written)
    assert response == {'error': 'invalid_request', 'message': 'Invalid JSON'}
    assert writer.closed is True


def test_handle_list_returns_all_credentials_with_providers():
    credentials = {
        'gmail-work': {'provider': 'gmail', 'client_id': 'x'},
        'gmail-client-a': {'provider': 'gmail'},
        'drive-work': {'provider': 'drive'},
        'gh': {'provider': 'github'},
    }
    response = handle_list(credentials)
    assert response['credentials'] == [
        {'name': 'gmail-work', 'provider': 'gmail'},
        {'name': 'gmail-client-a', 'provider': 'gmail'},
        {'name': 'drive-work', 'provider': 'drive'},
        {'name': 'gh', 'provider': 'github'},
    ]


def test_handle_list_returns_empty_for_no_credentials():
    assert handle_list({}) == {'credentials': []}


def test_handle_list_does_not_leak_secrets():
    credentials = {'gmail-work': {'provider': 'gmail', 'client_secret': 'GOCSPX-secret'}}
    response = handle_list(credentials)
    assert 'GOCSPX-secret' not in json.dumps(response)


def test_legacy_get_request_without_action_field_still_works():
    credentials = {'github-dfarrow': {'provider': 'github'}}
    token_store = FakeTokenStore({'github-dfarrow': 'refresh-abc'})
    exchanger = FakeTokenExchanger('access-xyz', '2099-01-01T00:00:00+00:00')
    response = handle_request(
        {'credential': 'github-dfarrow'},  # no 'action' field
        credentials, token_store, exchanger, {},
    )
    assert response['token'] == 'access-xyz'
