"""`dax run` proactively reruns the Claude login flow for a
`claude_tenant_state` env before every launch, rather than trusting
whatever is already sitting in Keychain.

Real incident, 2026-10: a `claude_tenant_state` env's session died before
any teardown ever synced a fresher token back to Keychain, leaving a
well-formed but dead credential there. `dax run` launched clean (nothing
looked wrong locally — `is_oauth_envelope` only checks shape, not whether
the refresh token is still accepted), and the container came up already
logged out, discovered only once Claude Code itself tried to use it. There
is no reliable local freshness signal to detect this in advance, so rather
than build one, `dax run` just reruns the login flow unconditionally —
gated by `relogin_due`'s cooldown (tests/test_dax_creds_login_state.py) so
restarting the same env several times in a day (e.g. after a `dax build`)
doesn't pop a browser window on every single restart.
"""
import argparse
from datetime import datetime, timedelta, timezone

import yaml

import dax
from dax import cmd_run
from dax_creds.config import record_login


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch, features=('claude_tenant_state',)):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'myenv'
    repo.mkdir(exist_ok=True)

    project = {
        'dir': str(repo), 'tenant': 'personal', 'creds': ['test-claude'],
        'features': list(features),
    }
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'image': 'test/dax:latest', 'features': [],
            'claudedir': {'mount': '~/.claude'},
            'credentials': {'test-claude': {'provider': 'claude'}},
            'projects': {'myenv': project},
        }, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    # The credential is already in Keychain — this isn't the "missing
    # credential" path, which is already covered elsewhere.
    monkeypatch.setattr('dax_creds.init.ensure_project_credentials', lambda *a, **k: [])
    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)

    class _FakeDaemon:
        def terminate(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(dax, '_start_creds_daemon', lambda *a, **k: _FakeDaemon())
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(dax, '_sync_and_clear_claude_credential', lambda *a, **k: None)

    logins = []
    monkeypatch.setattr(dax, 'cmd_creds_login',
                        lambda name, config: logins.append(name))
    return home, logins


def test_relogin_runs_when_never_logged_in_before(tmp_path, monkeypatch):
    home, logins = _setup(tmp_path, monkeypatch)

    cmd_run(_args())

    assert logins == ['test-claude']


def test_relogin_skipped_within_cooldown(tmp_path, monkeypatch):
    home, logins = _setup(tmp_path, monkeypatch)
    record_login('test-claude', when=datetime.now(timezone.utc) - timedelta(hours=1))

    cmd_run(_args())

    assert logins == []


def test_relogin_runs_again_once_cooldown_elapses(tmp_path, monkeypatch):
    home, logins = _setup(tmp_path, monkeypatch)
    record_login('test-claude', when=datetime.now(timezone.utc) - timedelta(hours=25))

    cmd_run(_args())

    assert logins == ['test-claude']


def test_relogin_not_triggered_without_claude_tenant_state_feature(tmp_path, monkeypatch):
    """The plain shared `claude` mount doesn't have the dead-rotation-link
    problem a persistent per-env state tree does — only claude_tenant_state
    envs get the proactive relogin."""
    home, logins = _setup(tmp_path, monkeypatch, features=('claude',))

    cmd_run(_args())

    assert logins == []


def test_relogin_respects_configured_cooldown(tmp_path, monkeypatch):
    home, logins = _setup(tmp_path, monkeypatch)
    with open(home / '.dax.yaml') as f:
        config = yaml.safe_load(f)
    config['defaults'] = {'relogin_cooldown_hours': 1}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump(config, f)
    record_login('test-claude', when=datetime.now(timezone.utc) - timedelta(hours=2))

    cmd_run(_args())

    assert logins == ['test-claude']


def test_relogin_skipped_for_credential_the_missing_path_already_handled(tmp_path, monkeypatch):
    """A credential absent from Keychain is handled by the existing
    "not in Keychain, log in now?" prompt — the proactive-relogin step must
    not also run the flow a second time for it in the same `dax run`."""
    home, logins = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr('dax_creds.init.ensure_project_credentials',
                        lambda *a, **k: {'test-claude'})
    monkeypatch.setattr('builtins.input', lambda prompt: 'y')

    cmd_run(_args())

    assert logins == ['test-claude']


def test_relogin_records_login_time_via_record_login(tmp_path, monkeypatch):
    """cmd_creds_login itself (the real one, not this module's fake) is
    responsible for calling record_login — exercised directly here rather
    than through cmd_run, since cmd_run fakes cmd_creds_login out above."""
    from dax_creds.config import last_login_at

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr(dax, '_post_login_import', lambda *a, **k: None)
    monkeypatch.setattr(dax, '_disk_token_for', lambda *a, **k: None)
    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(dax.subprocess, 'run', lambda cmd: None)

    class _FakeDaemon:
        _dax_log_path = '/dev/null'

        def terminate(self):
            pass

    monkeypatch.setattr(dax, '_start_login_daemon', lambda creds, port: _FakeDaemon())
    dax._LOGIN_PROVIDERS = getattr(dax, '_LOGIN_PROVIDERS', {})
    monkeypatch.setitem(dax._LOGIN_PROVIDERS, 'claude',
                        {'mounts': [], 'auth_command': 'true'})

    assert last_login_at('test-claude') is None

    dax.cmd_creds_login('test-claude', {'credentials': {'test-claude': {'provider': 'claude'}}})

    assert last_login_at('test-claude') is not None
