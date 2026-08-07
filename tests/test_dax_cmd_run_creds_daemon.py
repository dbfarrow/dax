"""`dax run`'s per-project credential daemon must use TCP, not a bind-mounted
Unix socket.

Found 2026-08: with two or more projects' containers running concurrently,
only the most-recently-started one's `dax-creds list` could reach its daemon;
every earlier one got ECONNREFUSED despite its daemon process being alive.
Docker Desktop's Mac-VM file sharing does not reliably forward a live
macOS-native Unix socket's connect/accept semantics into a container — the
same class of problem `_start_login_daemon` already solved for `dax creds
login` by switching to `tcp:host.docker.internal:<port>` (see its own comment,
"avoids Docker Desktop Unix socket permission issues"). `cmd_run`'s daemon
never got the same fix.

The real daemon subprocess needs `keyring` importable just to construct
`KeyringTokenStore()` at startup, before it ever serves a request — not
installed in this sandbox, so `_start_creds_daemon`/`_wait_for_tcp` are faked
here rather than actually spawned. That's fine: the bug and the fix are both
entirely about which transport `cmd_run` tells the container to use, i.e. the
assembled `docker run` command — not the daemon's own request handling, which
`test_dax_creds_daemon.py` already covers directly.
"""
import argparse

import yaml

import dax
from dax import _start_creds_daemon, cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'myenv'
    repo.mkdir(exist_ok=True)

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': ['test-github']}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'image': 'test/dax:latest', 'features': [],
            'credentials': {'test-github': {'provider': 'github'}},
            'projects': {'myenv': project},
        }, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    # Sidesteps the real Keychain (unreachable in this sandbox anyway) so
    # `cmd_run` doesn't hit the interactive "run dax creds login now? [Y/n]"
    # prompt, which would hang the test waiting on stdin.
    monkeypatch.setattr('dax_creds.init.ensure_project_credentials', lambda *a, **k: [])
    # Fakes daemon startup/liveness — see module docstring for why the real
    # subprocess can't run here — while still recording exactly what
    # cmd_run asked for, and its own terminate()/wait() for the finally block.
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    # This sandbox has no `keyring` either — same gap the module docstring
    # above already documents for the daemon subprocess. cmd_run's own
    # keyring check runs in-process now, so it needs faking too.
    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)

    class _FakeDaemon:
        def terminate(self):
            pass

        def wait(self):
            pass

    started = {}

    def _fake_start(credentials, port):
        started['credentials'] = credentials
        started['port'] = port
        return _FakeDaemon()

    monkeypatch.setattr(dax, '_start_creds_daemon', _fake_start)
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)
    return home, repo, started


def test_creds_daemon_uses_tcp_not_a_bind_mounted_socket(tmp_path, monkeypatch, capsys):
    home, repo, started = _setup(tmp_path, monkeypatch)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert started['port'] == 54321
    assert 'DAX_CREDS_SOCK=tcp:host.docker.internal:54321' in out
    assert '--add-host=host.docker.internal:host-gateway' in out
    assert 'DAX_CREDS_GITHUB=test-github' in out
    assert 'dax-creds.sock' not in out
    assert 'dax-state' not in out


def test_credential_daemon_skipped_cleanly_when_keyring_not_importable(tmp_path, monkeypatch, capsys):
    """Regression test for the 2026-08-06 incident: the daemon subprocess
    crashes on an uncaught ImportError when `keyring` isn't importable in
    whatever Python spawns it, and that crash was invisible to the caller —
    stdout/stderr go to a log file, so all `dax run`/`dax creds login` ever
    showed was a silent 5-second timeout and a generic "did not start". This
    must be caught before spawning anything, not discovered by a timeout.
    """
    home, repo, started = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(dax, '_keyring_importable', lambda: False)

    def _fail_if_called(*a, **k):
        raise AssertionError('_start_creds_daemon should not be called')
    monkeypatch.setattr(dax, '_start_creds_daemon', _fail_if_called)

    dax.cmd_run(_args())

    out = capsys.readouterr().out
    assert 'keyring not importable' in out
    assert 'DAX_CREDS_SOCK' not in out


def test_start_creds_daemon_passes_tcp_port_not_a_socket_path(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    captured = {}

    class _FakeProc:
        pass

    def _fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        return _FakeProc()

    monkeypatch.setattr('dax.subprocess.Popen', _fake_popen)

    _start_creds_daemon({'test-github': {'provider': 'github'}}, 54321)

    cmd = captured['cmd']
    assert '--tcp-port' in cmd
    assert cmd[cmd.index('--tcp-port') + 1] == '54321'
    assert '--socket' not in cmd
