"""A Claude `dax creds login` writes into a fresh temp folder, never the
host's real ~/.claude (env-lifecycle consolidation, decision 32).

The login used to mount the host's ~/.claude into the login container, so
`claude auth login` wrote the token there, and nothing removed it: the host
always held a plaintext copy of the newest env grant. Now a fresh 0700
folder under ~/.local/state/dax/login/ is mounted at the container's
~/.claude, the token is read from it into Keychain, and the folder is
removed whether the login succeeded, was abandoned, or blew up.

Also covers the implied credential (decision 35) reaching `dax creds login`
for an env whose `creds:` doesn't mention Claude at all.
"""
import json
import os
import time
from pathlib import Path

import pytest

import dax
import dax_creds.providers.claude as claude_provider_module


BLOB = json.dumps({'claudeAiOauth': {'refreshToken': 'fresh-grant', 'accessToken': 'a'}})


class FakeKeyring:
    def __init__(self):
        self.stored = {}

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.fixture
def harness(home, monkeypatch):
    """Stubs everything around the login container. `docker run` is replaced
    by `fake_run`, which records the command and, if told to, writes a token
    into whatever host folder is mounted at the container's ~/.claude."""
    kr = FakeKeyring()
    monkeypatch.setattr(claude_provider_module, '_keyring', kr)
    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)

    class _FakeDaemon:
        _dax_log_path = '/dev/null'

        def terminate(self):
            pass

    monkeypatch.setattr(dax, '_start_login_daemon', lambda *a, **k: _FakeDaemon())

    state = {'cmd': None, 'write': True, 'raise': None, 'mounted': None}

    def fake_run(cmd, *a, **k):
        state['cmd'] = cmd
        container_claude = '/home/{}/.claude'.format(dax._get_username())
        for i, arg in enumerate(cmd):
            if arg == '-v' and cmd[i + 1].endswith(':' + container_claude):
                state['mounted'] = Path(cmd[i + 1].rsplit(':', 1)[0])
        if state['raise']:
            raise state['raise']
        if state['write'] and state['mounted'] is not None:
            (state['mounted'] / '.credentials.json').write_text(BLOB)

    monkeypatch.setattr(dax.subprocess, 'run', fake_run)
    config = {'credentials': {}, 'projects': {
        'fabric': {'dir': str(home / 'fabric'), 'tenant': 'personal', 'creds': []}}}
    return state, kr, config


def test_the_login_mounts_a_fresh_folder_never_the_host_claude(home, harness):
    state, kr, config = harness
    host_claude = home / '.claude'
    host_claude.mkdir()
    (host_claude / 'CLAUDE.md').write_text('global')

    dax.cmd_creds_login('claude-personal-fabric', config)

    mounted = state['mounted']
    assert mounted is not None
    assert mounted.parent == home / '.local' / 'state' / 'dax' / 'login'
    assert str(host_claude) + ':' not in ' '.join(state['cmd'])


def test_a_successful_login_stores_the_token_and_leaves_nothing_on_disk(home, harness):
    state, kr, config = harness

    dax.cmd_creds_login('claude-personal-fabric', config)

    assert kr.get_password('dax-creds', 'claude-personal-fabric') == BLOB
    assert not state['mounted'].exists()
    assert not (home / '.claude' / '.credentials.json').exists()


def test_the_folder_is_private(home, harness, monkeypatch):
    state, kr, config = harness
    modes = []
    real_run = dax.subprocess.run

    def run_and_check(cmd, *a, **k):
        real_run(cmd, *a, **k)
        modes.append(oct(state['mounted'].stat().st_mode)[-3:])
    monkeypatch.setattr(dax.subprocess, 'run', run_and_check)

    dax.cmd_creds_login('claude-personal-fabric', config)

    assert modes == ['700']


def test_an_abandoned_login_imports_nothing_and_cleans_up(home, harness, capsys):
    state, kr, config = harness
    state['write'] = False

    dax.cmd_creds_login('claude-personal-fabric', config)

    assert kr.stored == {}
    assert 'no token found' in capsys.readouterr().out
    assert not state['mounted'].exists()


def test_the_folder_is_removed_even_when_the_login_blows_up(home, harness):
    state, kr, config = harness
    state['raise'] = KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        dax.cmd_creds_login('claude-personal-fabric', config)

    assert not state['mounted'].exists()


def test_leftover_folders_over_an_hour_old_are_swept_newer_ones_kept(home, harness):
    state, kr, config = harness
    base = home / '.local' / 'state' / 'dax' / 'login'
    old = base / 'claude-old'
    recent = base / 'claude-recent'
    old.mkdir(parents=True)
    recent.mkdir()
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))

    dax.cmd_creds_login('claude-personal-fabric', config)

    assert not old.exists()
    assert recent.exists()  # could be another login still in progress


def test_github_still_uses_its_real_config_folder(home, harness, monkeypatch):
    """Only Claude changed: GitHub's ~/.config/gh is the host gh's own
    long-lived config, which is meant to stay."""
    state, kr, config = harness
    monkeypatch.setattr(dax, '_post_login_import', lambda *a, **k: None)
    config['credentials']['gh-me'] = {'provider': 'github'}

    dax.cmd_creds_login('gh-me', config)

    assert '{}:'.format(home / '.config' / 'gh') in ' '.join(state['cmd'])
    assert not (home / '.local' / 'state' / 'dax' / 'login').exists() or \
        list((home / '.local' / 'state' / 'dax' / 'login').iterdir()) == []


def test_the_implied_credential_is_loginable_with_nothing_in_creds(home, harness):
    """Decision 35: `fabric`'s creds list is empty, yet its implied
    claude-personal-fabric is a credential `dax creds login` knows."""
    state, kr, config = harness
    assert config['projects']['fabric']['creds'] == []

    dax.cmd_creds_login('claude-personal-fabric', config)

    assert kr.get_password('dax-creds', 'claude-personal-fabric') == BLOB
