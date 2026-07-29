"""Tests for dax.py's _post_login_import, the step that runs right after a
`dax creds login` browser auth flow completes.

Covers the claude branch specifically, since it was just brought in line
with the existing github/auggie behavior (read the token the auth flow just
wrote to disk, store it in Keychain) rather than only printing a manual
`dax creds add` hint.
"""
import json

import dax_creds.providers.claude as claude_provider_module
from dax import _post_login_import


class FakeKeyring:
    def __init__(self):
        self.stored = {}

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value


ENVELOPE = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc',
                              'refreshToken': 'sk-ant-ort01-xyz'}}


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
