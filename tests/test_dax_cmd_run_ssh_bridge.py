"""`dax run`'s SSH agent forwarding must use a TCP bridge, not a bind-mounted
Unix socket — the same class of Docker Desktop VM-boundary fragility
`_start_creds_daemon` already fixed for credentials (see
test_dax_cmd_run_creds_daemon.py). `feature_ssh` alone can't prove this
anymore: resolving the host socket, starting the bridge, and deciding
whether `--add-host` is needed all happen in `cmd_run`, before the feature
loop runs — `feature_ssh` just reads what `cmd_run` already decided.
"""
import argparse

import yaml

import dax
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


def _setup(tmp_path, monkeypatch, project_features, creds=None, credentials=None):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'myenv'
    repo.mkdir(exist_ok=True)

    project = {'dir': str(repo), 'creds': creds or [], 'features': project_features}
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
    return home, repo


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


def test_ssh_bridge_starts_and_sets_container_env(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_features=['ssh'])

    sock = tmp_path / 'agent.sock'
    sock.touch()
    monkeypatch.setattr(dax, '_resolve_ssh_agent_sock', lambda config: str(sock))
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    started = {}
    monkeypatch.setattr(dax, '_start_ssh_agent_bridge', _fake_bridge_starter(started))

    cmd_run(_args())

    out = capsys.readouterr().out
    assert started['sock'] == str(sock)
    assert started['port'] == 54321
    assert 'DAX_SSH_AGENT_TCP_PORT=54321' in out
    assert out.count('--add-host=host.docker.internal:host-gateway') == 1
    # The old bind-mounted-socket approach is gone — no --volume to a host
    # ssh-agent socket and no SSH_AUTH_SOCK set directly on the docker cmd.
    assert 'ssh-agent' not in out
    assert 'SSH_AUTH_SOCK' not in out


def test_no_bridge_and_no_add_host_when_ssh_not_a_feature(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_features=[])

    def _fail(config):
        raise AssertionError('_resolve_ssh_agent_sock should not be called')
    monkeypatch.setattr(dax, '_resolve_ssh_agent_sock', _fail)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert '--add-host' not in out
    assert 'DAX_SSH_AGENT_TCP_PORT' not in out


def test_no_bridge_when_no_socket_resolves(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_features=['ssh'])
    monkeypatch.setattr(dax, '_resolve_ssh_agent_sock', lambda config: None)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'No SSH agent socket found' in out
    assert '--add-host' not in out
    assert 'DAX_SSH_AGENT_TCP_PORT' not in out


def test_add_host_not_duplicated_when_ssh_and_creds_both_active(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_features=['ssh'],
           creds=['test-github'], credentials={'test-github': {'provider': 'github'}})

    sock = tmp_path / 'agent.sock'
    sock.touch()
    monkeypatch.setattr(dax, '_resolve_ssh_agent_sock', lambda config: str(sock))
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    monkeypatch.setattr(dax, '_start_ssh_agent_bridge', _fake_bridge_starter({}))
    monkeypatch.setattr(dax, '_start_creds_daemon', lambda creds, port: _FakeProc())

    cmd_run(_args())

    out = capsys.readouterr().out
    assert out.count('--add-host=host.docker.internal:host-gateway') == 1
    assert 'DAX_SSH_AGENT_TCP_PORT=54321' in out
    assert 'DAX_CREDS_SOCK=tcp:host.docker.internal:54321' in out
