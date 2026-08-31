"""Tests for `_sync_and_clear_claude_credential` — the fix for a real
architectural gap found live, 2026-08-31: the Keychain-based credential
design was supposed to mean secrets don't sit on disk when nothing's
running, but `claude_tenant_state`'s persistent state tree quietly broke
that. Nothing ever wrote a session's refreshed `.credentials.json` back to
Keychain, and nothing deleted it on container stop — so once a tree had a
credential, it sat there in plaintext indefinitely, and a host-side
`dax creds login` could never reach it (the wrapper only injects when the
file is *absent*).

This closes both halves at once, host-side, in `cmd_run`'s existing
teardown `finally:` (same place the credential daemon and SSH bridge
already get stopped) — no container/wrapper changes needed, since the
state tree is a plain host bind mount. Once the file is deleted here, the
wrapper's existing inject-if-absent check does the rest on next launch:
Keychain always holds the freshest copy, so "inject" and "refresh" become
the same, unconditional action.
"""
import argparse
import json

import pytest
import yaml

import dax
from dax import _sync_and_clear_claude_credential, cmd_run
from dax_creds.config import state_tree_path


VALID_BLOB = json.dumps({'claudeAiOauth': {
    'refreshToken': 'fake-refresh-token', 'accessToken': 'fake-access',
}})


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return home


class FakeKeyring:
    def __init__(self):
        self.stored = {}

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value


@pytest.fixture
def fake_keyring(monkeypatch):
    fk = FakeKeyring()
    import dax_creds.providers.claude as claude_mod
    monkeypatch.setattr(claude_mod, '_keyring', fk)
    return fk


def _write_cred_file(home, tenant, name, content=VALID_BLOB):
    state = state_tree_path(tenant, name)
    state.mkdir(parents=True, exist_ok=True)
    cred_file = state / '.credentials.json'
    cred_file.write_text(content)
    return cred_file


def test_writes_to_keychain_and_deletes_the_file(home, fake_keyring):
    cred_file = _write_cred_file(home, 'acme', 'myproc')

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.get_password('dax-creds', 'claude-acme-myproc') == VALID_BLOB
    assert not cred_file.exists()


def test_noop_when_claude_tenant_state_not_in_features(home, fake_keyring):
    cred_file = _write_cred_file(home, 'acme', 'myproc')

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        [],  # claude_tenant_state not active
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.stored == {}
    assert cred_file.exists()


def test_noop_when_no_claude_credential_in_project_creds(home, fake_keyring):
    cred_file = _write_cred_file(home, 'acme', 'myproc')

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {})  # no creds at all for this run

    assert fake_keyring.stored == {}
    assert cred_file.exists()


def test_noop_when_no_credential_file_exists(home, fake_keyring):
    # state tree exists but never got a credential written into it
    state_tree_path('acme', 'myproc').mkdir(parents=True)

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.stored == {}


def test_noop_on_a_zero_byte_credential_file(home, fake_keyring):
    """A zero-byte file (an interrupted write) is treated as absent, matching
    the wrapper's own `-s` (not `-e`) check for the same file."""
    cred_file = _write_cred_file(home, 'acme', 'myproc', content='')

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.stored == {}
    assert cred_file.exists()  # left alone, not deleted


def test_noop_when_no_tenant(home, fake_keyring):
    _sync_and_clear_claude_credential(
        {'tenant': None, 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.stored == {}


def test_malformed_credential_file_is_deleted_not_preserved(home, fake_keyring, capsys):
    """A file that exists but isn't a real oauth envelope (corrupted, or a
    partial write) gets deleted, not preserved. Regression test for a real
    bug found live, 2026-08-31: a `dax creds login` + restart cycle still
    left the env logged out, because Claude Code's own dead-credential
    shape — {"claudeAiOauth": {"accessToken": "", "refreshToken": "", ...
    other fields intact}} — is real, non-empty, valid JSON, so it survived
    an earlier version of this function that only deleted a *successfully
    synced* blob and otherwise left anything it didn't recognize in place
    "to be safe." That caution was actively harmful here: the file could
    never become useful, and its mere presence permanently blocked
    re-injection from Keychain (both here and in the wrapper's own `-s`
    check) on every subsequent launch."""
    cred_file = _write_cred_file(home, 'acme', 'myproc', content='not valid json at all')

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.stored == {}
    assert not cred_file.exists()
    assert 'was not a usable Claude credentials blob' in capsys.readouterr().out


def test_blanked_token_credential_file_is_deleted_not_preserved(home, fake_keyring):
    """The exact real-world shape found live: a genuinely dead credential —
    Claude Code blanks accessToken/refreshToken to "" on a failed refresh
    while keeping every other field (scopes, subscriptionType,
    rateLimitTier) populated. Non-empty, valid JSON, `claudeAiOauth`
    present — everything the old check accepted as "a credential is here,"
    while being completely unusable."""
    blob = json.dumps({'claudeAiOauth': {
        'accessToken': '', 'refreshToken': '', 'expiresAt': 0,
        'refreshTokenExpiresAt': 1788120165026,
        'scopes': ['user:inference'], 'subscriptionType': 'team',
        'rateLimitTier': 'default_raven',
    }})
    cred_file = _write_cred_file(home, 'acme', 'myproc', content=blob)

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert fake_keyring.stored == {}
    assert not cred_file.exists()


def test_keyring_failure_is_reported_not_raised(home, monkeypatch, capsys):
    """A Keychain error (locked keychain, permission issue, etc.) must be
    reported and swallowed, never propagate out of cmd_run's teardown —
    this runs inside a `finally:` block that also has to stop the
    credential daemon and SSH bridge regardless of what happens here."""
    _write_cred_file(home, 'acme', 'myproc')

    import dax_creds.providers.claude as claude_mod

    class BoomKeyring:
        def get_password(self, service, name):
            return None

        def set_password(self, service, name, value):
            raise RuntimeError('keychain is locked')

    monkeypatch.setattr(claude_mod, '_keyring', BoomKeyring())

    _sync_and_clear_claude_credential(  # must not raise
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {'claude-acme-myproc': {'provider': 'claude'}})

    assert 'could not sync' in capsys.readouterr().out


def test_picks_the_claude_credential_out_of_multiple_providers(home, fake_keyring):
    """project_creds can hold non-claude credentials too (github, ssh, ...)
    — must find the claude one specifically, not just grab the first key."""
    _write_cred_file(home, 'acme', 'myproc')

    _sync_and_clear_claude_credential(
        {'tenant': 'acme', 'workdir_name': 'myproc'},
        ['claude_tenant_state'],
        {
            'github-dbfarrow': {'provider': 'github'},
            'claude-acme-myproc': {'provider': 'claude'},
            'ssh-id-rsa': {'provider': 'ssh'},
        })

    assert fake_keyring.get_password('dax-creds', 'claude-acme-myproc') == VALID_BLOB


# ---------------------------------------------------------------------------
# Wiring: cmd_run must actually call _sync_and_clear_claude_credential from
# its teardown finally: — the unit tests above only prove the function
# itself is correct, not that anything in cmd_run invokes it.
# ---------------------------------------------------------------------------

def _setup_claude_tenant_state_project(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'myproc'
    repo.mkdir(exist_ok=True)

    project = {
        'dir': str(repo), 'tenant': 'acme', 'creds': ['claude'],
        'features': ['claude_tenant_state'],
    }
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'image': 'test/dax:latest', 'features': [],
            'projects': {'myproc': project},
        }, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr('dax_creds.init.ensure_project_credentials', lambda *a, **k: [])
    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)

    class _FakeDaemon:
        def terminate(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(dax, '_start_creds_daemon', lambda credentials, port: _FakeDaemon())
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)
    # test_only=False exercises the real (non-dry-run) teardown path, so the
    # actual `docker run` call must be faked rather than really launched.
    monkeypatch.setattr(dax.subprocess, 'run', lambda cmd: None)
    return home, repo


def test_cmd_run_calls_the_sync_on_teardown(tmp_path, monkeypatch):
    home, repo = _setup_claude_tenant_state_project(tmp_path, monkeypatch)

    calls = []
    monkeypatch.setattr(dax, '_sync_and_clear_claude_credential',
                        lambda config, features, project_creds: calls.append(
                            (features, project_creds)))

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=False))

    assert len(calls) == 1
    features, project_creds = calls[0]
    assert 'claude_tenant_state' in features
    assert any(d.get('provider') == 'claude' for d in project_creds.values())


def test_cmd_run_skips_the_sync_in_test_only_mode(tmp_path, monkeypatch):
    """-t is a pure preview — must never delete the real credential file as
    a side effect of just printing the docker command."""
    home, repo = _setup_claude_tenant_state_project(tmp_path, monkeypatch)

    calls = []
    monkeypatch.setattr(dax, '_sync_and_clear_claude_credential',
                        lambda *a, **k: calls.append(True))

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=True))

    assert calls == []
