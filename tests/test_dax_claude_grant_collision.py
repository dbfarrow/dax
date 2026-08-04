"""One OAuth grant must never end up stored under two credential names.

Per-env Claude credentials only isolate anything because each is its own grant:
independent grants have independent refresh-token chains, so a rotation in one
project cannot invalidate another's. Two names backed by one grant authenticate
perfectly and look correct — until a rotation kills both at once.

Decision C3 guarded the login path by comparing the provider's on-disk token
before and after the flow, which answers "did the file change" rather than "did
the grant change". Two holes remained, both closed here (decision C6):

  * a Claude Code token refresh landing inside the login window changes the
    file, so an abandoned login looks successful and imports another env's grant;
  * `dax creds add`'s claude path never had the before/after guard at all.
"""
import json

import pytest

import dax_creds.providers.claude as claude_provider_module
from dax import _post_login_import
from dax_creds.config import credential_names_for_provider
from dax_creds.init import _setup_claude_credential
from dax_creds.providers.claude import (
    ClaudeProvider, grant_collision_message, grant_id,
)


class FakeKeyring:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value


def envelope(refresh, access='sk-ant-oat01-x'):
    return json.dumps({'claudeAiOauth': {'accessToken': access,
                                         'refreshToken': refresh}})


CONFIG = {
    'credentials': {'github-dbfarrow': {'provider': 'github'}},
    'projects': {
        'dax': {'tenant': 'personal', 'creds': ['claude']},
        'fabric': {'tenant': 'personal', 'creds': ['claude']},
    },
}


def write_disk_token(home, blob):
    claude_dir = home / '.claude'
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / '.credentials.json').write_text(blob)


# --- grant identity ---------------------------------------------------------

def test_grant_id_is_stable_and_ignores_the_access_token():
    """Access tokens rotate constantly; the grant is the refresh-token chain."""
    a = envelope('sk-ant-ort01-same', access='first')
    b = envelope('sk-ant-ort01-same', access='second-refreshed')

    assert grant_id(a) == grant_id(b)


def test_grant_id_differs_between_grants():
    assert grant_id(envelope('one')) != grant_id(envelope('two'))


def test_grant_id_never_returns_the_secret():
    assert 'sk-ant-ort01-secret' not in grant_id(envelope('sk-ant-ort01-secret'))


@pytest.mark.parametrize('value', [
    None, '', 'not json', '{}', '{"claudeAiOauth": {}}',
    '{"claudeAiOauth": {"accessToken": "bare-access-token-only"}}',
])
def test_grant_id_is_none_for_anything_without_a_refresh_token(value):
    assert grant_id(value) is None


# --- collision detection ----------------------------------------------------

def test_collision_detected_across_names():
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('shared')})
    provider = ClaudeProvider(keyring=kr)

    clash = provider.grant_collision(
        'claude-personal-fabric', envelope('shared'),
        {'claude-personal-dax', 'claude-personal-fabric'})

    assert clash == 'claude-personal-dax'


def test_no_collision_between_independent_grants():
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('grant-a')})
    provider = ClaudeProvider(keyring=kr)

    clash = provider.grant_collision(
        'claude-personal-fabric', envelope('grant-b'),
        {'claude-personal-dax', 'claude-personal-fabric'})

    assert clash is None


def test_a_credentials_own_grant_is_not_a_collision():
    """Re-importing a name's own token is a no-op, not sharing — and refusing it
    would block re-importing after a `dax creds remove`."""
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('mine')})
    provider = ClaudeProvider(keyring=kr)

    clash = provider.grant_collision('claude-personal-dax', envelope('mine'),
                                     {'claude-personal-dax'})

    assert clash is None


def test_collision_check_is_inert_without_a_keychain():
    """In a container there is no Keychain to compare against. Nothing to check
    is not the same as a collision, so the import must not be blocked."""
    provider = ClaudeProvider(keyring=None)

    assert provider.grant_collision('claude-personal-dax', envelope('x'),
                                    {'claude-personal-fabric'}) is None


def test_credential_names_for_provider_includes_derived_names():
    """The colliding name is usually derived, so enumerating only the registry
    would make the check pass by seeing nothing."""
    names = credential_names_for_provider(CONFIG, 'claude')

    assert names == {'claude-personal-dax', 'claude-personal-fabric'}
    assert 'github-dbfarrow' not in names


# --- login path (decision C3's residual hole) -------------------------------

def test_login_refuses_a_grant_already_stored_under_another_name(
        tmp_path, monkeypatch, capsys):
    """The C6 scenario: `before != after`, so C3's file comparison is satisfied,
    but the token on disk is still another env's grant — as happens when Claude
    Code refreshes it during the login window."""
    monkeypatch.setenv('HOME', str(tmp_path))
    write_disk_token(tmp_path, envelope('dax-grant', access='refreshed'))
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('dax-grant')})
    monkeypatch.setattr(claude_provider_module, '_keyring', kr)

    _post_login_import('claude-personal-fabric', {'provider': 'claude'}, CONFIG,
                       'claude', before=envelope('dax-grant', access='stale'))

    assert ('dax-creds', 'claude-personal-fabric') not in kr.stored
    out = capsys.readouterr().out
    assert "same OAuth grant as 'claude-personal-dax'" in out
    assert 'dax creds login claude-personal-fabric' in out


def test_login_stores_an_independent_grant(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    write_disk_token(tmp_path, envelope('fresh-fabric-grant'))
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('dax-grant')})
    monkeypatch.setattr(claude_provider_module, '_keyring', kr)

    _post_login_import('claude-personal-fabric', {'provider': 'claude'}, CONFIG,
                       'claude', before=envelope('dax-grant'))

    stored = kr.stored[('dax-creds', 'claude-personal-fabric')]
    assert grant_id(stored) == grant_id(envelope('fresh-fabric-grant'))


# --- add path (never had a guard at all) ------------------------------------

def test_add_refuses_to_copy_another_envs_grant(tmp_path, monkeypatch, capsys):
    """`dax creds add`'s claude path imports straight from
    ~/.claude/.credentials.json, which under a shared mount is whichever env
    logged in last. This was the one remaining door to a shared grant."""
    monkeypatch.setenv('HOME', str(tmp_path))
    write_disk_token(tmp_path, envelope('dax-grant'))
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('dax-grant')})
    monkeypatch.setattr(claude_provider_module, '_keyring', kr)

    _setup_claude_credential('claude-personal-fabric', {'provider': 'claude'},
                             config=CONFIG)

    assert ('dax-creds', 'claude-personal-fabric') not in kr.stored
    assert "same OAuth grant as 'claude-personal-dax'" in capsys.readouterr().out


def test_add_still_imports_an_unshared_grant(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    write_disk_token(tmp_path, envelope('fabric-only-grant'))
    kr = FakeKeyring({('dax-creds', 'claude-personal-dax'): envelope('dax-grant')})
    monkeypatch.setattr(claude_provider_module, '_keyring', kr)

    _setup_claude_credential('claude-personal-fabric', {'provider': 'claude'},
                             config=CONFIG)

    assert ('dax-creds', 'claude-personal-fabric') in kr.stored


def test_add_without_config_skips_the_check_rather_than_crashing(
        tmp_path, monkeypatch):
    """`config` is optional so callers that have none still work — `dax init`'s
    path passes it, but the signature must not become load-bearing."""
    monkeypatch.setenv('HOME', str(tmp_path))
    write_disk_token(tmp_path, envelope('some-grant'))
    kr = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', kr)

    _setup_claude_credential('claude-work', {'provider': 'claude'})

    assert ('dax-creds', 'claude-work') in kr.stored


# --- message ----------------------------------------------------------------

def test_both_paths_share_one_explanation():
    lines = grant_collision_message('claude-personal-fabric', 'claude-personal-dax')

    assert "'claude-personal-dax'" in lines[0]
    assert 'rotation' in ' '.join(lines)
    assert 'dax creds login claude-personal-fabric' in ' '.join(lines)
