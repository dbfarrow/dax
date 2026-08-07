"""`dax run`'s SSH agent forwarding turns on purely because a project's
`creds:` resolves an `ssh`-provider credential — there is no separate `ssh`
feature to also remember. `feature_ssh`/`_resolve_ssh_agent_sock` and the
ambient SSH_AUTH_SOCK/Docker-Desktop-bridge-socket fallback chain were
removed 2026-08: the only source is ever this run's own ephemeral agent,
folded directly into the credential-resolution flow that already starts it
(see docs/design/2026-08-04-codebase-review.md). A project with no `ssh`
credential gets no forwarding at all — no ambient fallback left to catch it.

Bridging over TCP rather than a bind-mounted socket (the Docker Desktop
VM-boundary fragility this replaced) is covered directly below and in
test_dax_creds_ssh_bridge.py.
"""
import argparse

import pytest
import yaml

import dax
import dax_creds.providers.ssh as ssh_provider_module
from dax import cmd_run, _start_ssh_agent_bridge


def test_start_ssh_agent_bridge_runs_python_module_not_socat(tmp_path, monkeypatch):
    # Regression test for the real bug this was shipped with: socat is
    # guaranteed inside the container image, never on the host, and stock
    # macOS doesn't have it. The host-side bridge must not shell out to it.
    monkeypatch.setenv('HOME', str(tmp_path))
    captured = {}

    class _FakeProc:
        pass

    def _fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        return _FakeProc()

    monkeypatch.setattr('dax.subprocess.Popen', _fake_popen)

    _start_ssh_agent_bridge('/tmp/agent.sock', 54782)

    cmd = captured['cmd']
    assert 'socat' not in cmd
    assert cmd[0] == dax.sys.executable
    assert '-m' in cmd
    assert cmd[cmd.index('-m') + 1] == 'dax_creds.ssh_bridge'
    assert cmd[cmd.index('--agent-sock') + 1] == '/tmp/agent.sock'
    assert cmd[cmd.index('--port') + 1] == '54782'


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


class _FakeProc:
    def terminate(self):
        pass

    def wait(self):
        pass


def _fake_bridge_starter(started):
    def _start(agent_sock, port):
        started['sock'] = agent_sock
        started['port'] = port
        return _FakeProc()
    return _start


def _setup(tmp_path, monkeypatch, creds=None, credentials=None, features=None):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'myenv'
    repo.mkdir(exist_ok=True)

    project = {'dir': str(repo), 'creds': creds or []}
    if features is not None:
        project['features'] = features
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'image': 'test/dax:latest', 'features': [],
            'credentials': credentials or {},
            'projects': {'myenv': project},
        }, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr('dax_creds.init.ensure_project_credentials', lambda *a, **k: [])
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)
    # Real ssh-agent/ssh-add are host-only side effects with no place in a
    # unit test — faked the same way _start_creds_daemon already is.
    monkeypatch.setattr(ssh_provider_module, 'start_ephemeral_agent',
                        lambda: ('/tmp/fake-agent.sock', 99999))
    monkeypatch.setattr(ssh_provider_module, 'stop_ephemeral_agent', lambda pid: None)
    monkeypatch.setattr(ssh_provider_module.SshProvider, 'setup',
                        lambda self, cred_def, agent_sock=None: None)
    return home, repo


def test_ssh_bridge_starts_when_an_ssh_credential_is_configured(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, creds=['ssh-key'],
           credentials={'ssh-key': {'provider': 'ssh', 'key': '~/.ssh/id_rsa'}})

    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    started = {}
    monkeypatch.setattr(dax, '_start_ssh_agent_bridge', _fake_bridge_starter(started))

    cmd_run(_args())

    out = capsys.readouterr().out
    assert started['sock'] == '/tmp/fake-agent.sock'
    assert started['port'] == 54321
    assert 'DAX_SSH_AGENT_TCP_PORT=54321' in out
    assert out.count('--add-host=host.docker.internal:host-gateway') == 1


def test_no_bridge_when_no_ssh_credential_configured(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, creds=[])

    def _fail(*a, **k):
        raise AssertionError('start_ephemeral_agent should not be called')
    monkeypatch.setattr(ssh_provider_module, 'start_ephemeral_agent', _fail)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert '--add-host' not in out
    assert 'DAX_SSH_AGENT_TCP_PORT' not in out


def test_ssh_in_features_is_now_an_unknown_feature(tmp_path, monkeypatch, capsys):
    """The old opt-in switch is gone, not just inert: `_add_feature` treats
    any unrecognized name as fatal, so a project whose `features:` still
    lists `ssh` from before this change now hard-refuses to launch at all,
    rather than silently doing nothing. Migrating off the old two-switch
    design means actually removing `ssh` from `features:`, not just leaving
    a now-meaningless entry there."""
    _setup(tmp_path, monkeypatch, creds=[], features=['ssh'])

    with pytest.raises(SystemExit):
        cmd_run(_args())

    assert 'unknown feature: ssh' in capsys.readouterr().out


def test_add_host_not_duplicated_when_ssh_and_other_creds_both_active(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch,
           creds=['ssh-key', 'test-github'],
           credentials={'ssh-key': {'provider': 'ssh', 'key': '~/.ssh/id_rsa'},
                        'test-github': {'provider': 'github'}})

    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    monkeypatch.setattr(dax, '_start_ssh_agent_bridge', _fake_bridge_starter({}))
    monkeypatch.setattr(dax, '_start_creds_daemon', lambda creds, port: _FakeProc())

    cmd_run(_args())

    out = capsys.readouterr().out
    assert out.count('--add-host=host.docker.internal:host-gateway') == 1
    assert 'DAX_SSH_AGENT_TCP_PORT=54321' in out
    assert 'DAX_CREDS_SOCK=tcp:host.docker.internal:54321' in out
