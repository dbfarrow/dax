"""Tests for dax.py's `dax creds login` support: which credential definition the
command resolves, and `_post_login_import`, the step that runs right after the
browser auth flow completes.

Covers the claude branch specifically, since it was just brought in line
with the existing github/auggie behavior (read the token the auth flow just
wrote to disk, store it in Keychain) rather than only printing a manual
`dax creds add` hint.
"""
import json

import pytest

import dax_creds.providers.claude as claude_provider_module
from dax import _login_credential_def, _post_login_import


class FakeKeyring:
    def __init__(self):
        self.stored = {}

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value


ENVELOPE = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc',
                              'refreshToken': 'sk-ant-ort01-xyz'}}


DERIVED_CONFIG = {
    'credential_defaults': {'claude': {'browser': 'chrome',
                                       'chrome_profile': 'Profile 2'}},
    'credentials': {'github-dbfarrow': {'provider': 'github'}},
    'projects': {'fabric': {'tenant': 'personal',
                            'creds': ['claude', 'github-dbfarrow']}},
}


def test_login_resolves_a_derived_credential_name():
    """`dax creds login claude-personal-fabric` refused a name `dax creds list`
    displayed, because only the registry was consulted. With `add`'s claude path
    being the unguarded copy-from-disk route, that left no way to mint a per-env
    credential at all. Found by manual step 4 on 2026-07-30 (decision C5)."""
    cred_def = _login_credential_def(DERIVED_CONFIG, 'claude-personal-fabric')

    assert cred_def['provider'] == 'claude'


def test_login_derived_credential_carries_credential_defaults():
    """Without this the per-env login opens the default browser instead of the
    Chrome profile the account lives in — decision C4."""
    cred_def = _login_credential_def(DERIVED_CONFIG, 'claude-personal-fabric')

    assert cred_def['browser'] == 'chrome'
    assert cred_def['chrome_profile'] == 'Profile 2'


def test_login_prefers_an_explicit_registration_over_the_derived_name():
    """An explicit entry wins everywhere else resolution happens; login matches.
    It is also the escape hatch for deliberately sharing one credential."""
    config = dict(DERIVED_CONFIG,
                  credentials=dict(DERIVED_CONFIG['credentials'],
                                   **{'claude-personal-fabric': {
                                       'provider': 'claude',
                                       'chrome_profile': 'Profile 9'}}))

    cred_def = _login_credential_def(config, 'claude-personal-fabric')

    assert cred_def['chrome_profile'] == 'Profile 9'


def test_login_still_rejects_a_name_that_is_neither_registered_nor_derived():
    with pytest.raises(KeyError):
        _login_credential_def(DERIVED_CONFIG, 'claude-personal-typo')


def test_login_rejects_a_derived_name_whose_env_has_no_tenant():
    """A bare `claude` with no tenant is unresolvable, so no name is derived from
    it — the refusal must stay a refusal rather than crashing out of login."""
    config = dict(DERIVED_CONFIG,
                  projects={'fabric': {'creds': ['claude']}})

    with pytest.raises(KeyError):
        _login_credential_def(config, 'claude-personal-fabric')


def test_post_login_import_claude_stores_token_from_disk(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / '.credentials.json').write_text(json.dumps(ENVELOPE))
    monkeypatch.setenv('HOME', str(home))
    fake_keyring = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', fake_keyring)

    _post_login_import('claude-personal-fabric', {'provider': 'claude'}, {}, 'claude')

    stored = fake_keyring.stored[('dax-creds', 'claude-personal-fabric')]
    assert json.loads(stored) == ENVELOPE
    assert 'imported to Keychain' in capsys.readouterr().out


def test_post_login_import_claude_reports_when_nothing_on_disk(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    fake_keyring = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', fake_keyring)

    _post_login_import('claude-personal-fabric', {'provider': 'claude'}, {}, 'claude')

    assert fake_keyring.stored == {}
    assert 'no token found' in capsys.readouterr().out


def test_post_login_import_refuses_a_token_the_flow_did_not_write(tmp_path, monkeypatch, capsys):
    """An abandoned login leaves the pre-existing credential on disk. Importing
    it would store an existing grant under a new name — two Keychain entries,
    one grant, no isolation — and it presents as success. Observed 2026-07-30
    when a login was cancelled at the wrong browser."""
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / '.credentials.json').write_text(json.dumps(ENVELOPE))
    monkeypatch.setenv('HOME', str(home))
    fake_keyring = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', fake_keyring)

    # `before` is what the same file already held — unchanged by the flow.
    _post_login_import('claude-personal-discernment', {'provider': 'claude'}, {},
                       'claude', before=json.dumps(ENVELOPE))

    assert fake_keyring.stored == {}
    out = capsys.readouterr().out
    assert 'did not write a new credential' in out
    assert 'share an existing grant' in out


def test_post_login_import_accepts_a_token_the_flow_did_write(tmp_path, monkeypatch):
    """A real login replaces the file, so before != after and the import runs."""
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / '.credentials.json').write_text(json.dumps(ENVELOPE))
    monkeypatch.setenv('HOME', str(home))
    fake_keyring = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', fake_keyring)

    stale = json.dumps({'claudeAiOauth': {'accessToken': 'old', 'refreshToken': 'old'}})
    _post_login_import('claude-personal-discernment', {'provider': 'claude'}, {},
                       'claude', before=stale)

    stored = fake_keyring.stored[('dax-creds', 'claude-personal-discernment')]
    assert json.loads(stored) == ENVELOPE


def test_post_login_import_first_ever_login_has_nothing_before(tmp_path, monkeypatch):
    """No credential on disk beforehand — `before` is None, import proceeds."""
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / '.credentials.json').write_text(json.dumps(ENVELOPE))
    monkeypatch.setenv('HOME', str(home))
    fake_keyring = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', fake_keyring)

    _post_login_import('claude-new', {'provider': 'claude'}, {}, 'claude', before=None)

    assert ('dax-creds', 'claude-new') in fake_keyring.stored


def test_post_login_import_claude_overwrites_existing_keychain_entry(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / '.claude').mkdir(parents=True)
    (home / '.claude' / '.credentials.json').write_text(json.dumps(ENVELOPE))
    monkeypatch.setenv('HOME', str(home))
    fake_keyring = FakeKeyring()
    fake_keyring.set_password('dax-creds', 'claude-personal-fabric', 'stale-old-value')
    monkeypatch.setattr(claude_provider_module, '_keyring', fake_keyring)

    _post_login_import('claude-personal-fabric', {'provider': 'claude'}, {}, 'claude')

    stored = fake_keyring.stored[('dax-creds', 'claude-personal-fabric')]
    assert json.loads(stored) == ENVELOPE
