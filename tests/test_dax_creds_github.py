import json
import pytest
from dax_creds.providers.github import GitHubProvider


class FakeKeyring:
    def __init__(self, stored=None):
        self._stored = stored or {}

    def get_password(self, service, name):
        return self._stored.get((service, name))

    def set_password(self, service, name, value):
        self._stored[(service, name)] = value


def test_check_returns_true_when_token_in_keychain():
    keyring = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_abc'})
    provider = GitHubProvider(keyring=keyring)
    assert provider.check({'provider': 'github'}, 'github-dfarrow') is True


def test_check_returns_false_when_token_not_in_keychain():
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    assert provider.check({'provider': 'github'}, 'github-dfarrow') is False


def test_import_from_disk_reads_gh_hosts_yml(tmp_path):
    hosts_yml = tmp_path / 'hosts.yml'
    hosts_yml.write_text(
        'github.com:\n'
        '    oauth_token: ghp_existing123\n'
        '    user: dfarrow\n'
    )
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    token = provider.import_from_disk({'provider': 'github'}, hosts_file=hosts_yml)
    assert token == 'ghp_existing123'


def test_import_from_disk_returns_none_when_file_missing(tmp_path):
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    token = provider.import_from_disk({'provider': 'github'}, hosts_file=tmp_path / 'none.yml')
    assert token is None


def test_import_from_disk_nested_format_matches_by_credential_name(tmp_path):
    hosts_yml = tmp_path / 'hosts.yml'
    hosts_yml.write_text(
        'github.com:\n'
        '  user: dbfarrow\n'
        '  users:\n'
        '    dbfarrow:\n'
        '      oauth_token: ghp_dbfarrow\n'
        '    auggie-davefarrow:\n'
        '      oauth_token: ghp_auggie\n'
    )
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    assert provider.import_from_disk({}, credential_name='github-dbfarrow', hosts_file=hosts_yml) == 'ghp_dbfarrow'
    assert provider.import_from_disk({}, credential_name='github-auggie-davefarrow', hosts_file=hosts_yml) == 'ghp_auggie'


def test_import_from_disk_nested_format_falls_back_to_default_user(tmp_path):
    hosts_yml = tmp_path / 'hosts.yml'
    hosts_yml.write_text(
        'github.com:\n'
        '  user: dbfarrow\n'
        '  users:\n'
        '    dbfarrow:\n'
        '      oauth_token: ghp_dbfarrow\n'
        '    other:\n'
        '      oauth_token: ghp_other\n'
    )
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    # No credential_name match — falls back to default 'user' field
    token = provider.import_from_disk({}, credential_name='github-unknown', hosts_file=hosts_yml)
    assert token == 'ghp_dbfarrow'


def test_import_from_disk_nested_format_returns_none_when_no_tokens(tmp_path):
    hosts_yml = tmp_path / 'hosts.yml'
    hosts_yml.write_text(
        'github.com:\n'
        '  user: dbfarrow\n'
        '  users:\n'
        '    dbfarrow: {}\n'
    )
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    assert provider.import_from_disk({}, hosts_file=hosts_yml) is None


def test_store_saves_to_keychain():
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring)
    provider.store('github-dfarrow', 'ghp_newtoken')
    assert keyring.get_password('dax-creds', 'github-dfarrow') == 'ghp_newtoken'


def test_get_token_returns_from_keychain():
    keyring = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_abc'})
    provider = GitHubProvider(keyring=keyring)
    assert provider.get_token('github-dfarrow') == 'ghp_abc'


def test_acquire_device_flow_polls_until_authorized():
    responses = iter([
        # First call: device code request
        {'device_code': 'dc123', 'user_code': 'ABCD-1234',
         'verification_uri': 'https://github.com/login/device',
         'interval': 0, 'expires_in': 30},
        # Second call: still pending
        {'error': 'authorization_pending'},
        # Third call: authorized
        {'access_token': 'ghp_newtoken'},
    ])

    def fake_http(method, url, data):
        return next(responses)

    keyring = FakeKeyring()
    prompts = []
    provider = GitHubProvider(keyring=keyring, http=fake_http)
    token = provider.acquire(
        {'provider': 'github', 'client_id': 'test-client-id'},
        'github-dfarrow',
        prompter=prompts.append,
    )

    assert token == 'ghp_newtoken'
    assert keyring.get_password('dax-creds', 'github-dfarrow') == 'ghp_newtoken'
    assert any('ABCD-1234' in p for p in prompts)


def test_acquire_calls_opener_with_verification_uri():
    responses = iter([
        {'device_code': 'dc123', 'user_code': 'ABCD-1234',
         'verification_uri': 'https://github.com/login/device',
         'interval': 0, 'expires_in': 30},
        {'access_token': 'ghp_newtoken'},
    ])

    def fake_http(method, url, data):
        return next(responses)

    opened_urls = []
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring, http=fake_http)
    provider.acquire(
        {'provider': 'github', 'client_id': 'test-client-id'},
        'github-dfarrow',
        prompter=lambda _: None,
        opener=opened_urls.append,
    )

    assert opened_urls == ['https://github.com/login/device']


def test_acquire_without_opener_includes_url_in_prompts():
    responses = iter([
        {'device_code': 'dc123', 'user_code': 'ABCD-1234',
         'verification_uri': 'https://github.com/login/device',
         'interval': 0, 'expires_in': 30},
        {'access_token': 'ghp_newtoken'},
    ])

    def fake_http(method, url, data):
        return next(responses)

    prompts = []
    keyring = FakeKeyring()
    provider = GitHubProvider(keyring=keyring, http=fake_http)
    provider.acquire(
        {'provider': 'github', 'client_id': 'test-client-id'},
        'github-dfarrow',
        prompter=prompts.append,
    )

    assert any('https://github.com/login/device' in p for p in prompts)
