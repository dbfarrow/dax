import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import (
    feature_workdir,
    feature_optdir,
    feature_aws,
    feature_ssh,
    feature_dotfiles,
    feature_ports,
    feature_msf,
    feature_ovpn,
    feature_X11,
    feature_webpreview,
    _find_preview_port,
)


def test_feature_workdir_mounts_cwd():
    config = {
        'cwd': '/Users/davefarrow/work/project',
        'workdir': {'container': 'work'},
        '_container_home': '/home/davefarrow',
    }
    opts = feature_workdir(config)
    assert '--volume=/Users/davefarrow/work/project:/home/davefarrow/work' in opts


def test_feature_optdir_mounts_volume():
    config = {
        'optdir': {
            'host': '/Users/dfarrow/opt',
            'container': '/home/dfarrow/opt',
        }
    }
    opts = feature_optdir(config)
    assert '--volume=/Users/dfarrow/opt:/home/dfarrow/opt' in opts


def test_feature_aws_mounts_volume():
    config = {
        'awsdir': {
            'host': '/Users/dfarrow/.aws',
            'container': '/home/dfarrow/.aws',
        }
    }
    opts = feature_aws(config)
    assert '--volume=/Users/dfarrow/.aws:/home/dfarrow/.aws' in opts


def test_feature_dotfiles_ro(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {
        'dotfiles': {
            'ro': ['~/.zshrc', '~/.vimrc'],
            'rw': [],
        },
        '_container_home': '/home/dfarrow',
    }
    opts = feature_dotfiles(config)
    assert '--volume=/Users/dfarrow/.zshrc:/home/dfarrow/.zshrc:ro' in opts
    assert '--volume=/Users/dfarrow/.vimrc:/home/dfarrow/.vimrc:ro' in opts


def test_feature_dotfiles_rw(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {
        'dotfiles': {
            'ro': [],
            'rw': ['~/.zsh_history'],
        },
        '_container_home': '/home/dfarrow',
    }
    opts = feature_dotfiles(config)
    assert '--volume=/Users/dfarrow/.zsh_history:/home/dfarrow/.zsh_history' in opts
    # must NOT have :ro suffix
    assert '--volume=/Users/dfarrow/.zsh_history:/home/dfarrow/.zsh_history:ro' not in opts


def test_feature_ports_returns_port_flags():
    config = {'ports': ['8080:80', '8443:443']}
    opts = feature_ports(config)
    assert '-p' in opts
    assert '8080:80' in opts
    assert '8443:443' in opts


def test_feature_ssh_returns_agent_forwarding(tmp_path, monkeypatch):
    sock = tmp_path / 'ssh-auth.sock'
    sock.touch()
    monkeypatch.setenv('SSH_AUTH_SOCK', str(sock))
    opts = feature_ssh({})
    assert any('ssh-agent' in o for o in opts)
    assert '-e' in opts
    assert 'SSH_AUTH_SOCK=/ssh-agent' in opts


def test_feature_ssh_warns_if_no_socket(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    monkeypatch.setattr('dax._DOCKER_DESKTOP_SSH_SOCK', str(tmp_path / 'missing.sock'))
    opts = feature_ssh({})
    assert opts == []


def test_feature_webpreview_uses_explicit_port(monkeypatch):
    monkeypatch.setattr('dax._is_port_free', lambda p: True)
    config = {
        'cwd': '/home/user/project',
        'webpreview': {'port': 9000},
        '_container_home': '/home/user',
        'workdir': {'container': 'work'},
    }
    opts = feature_webpreview(config)
    assert '-p' in opts
    assert '9000:9000' in opts
    assert 'DAX_PREVIEW_PORT=9000' in config['_shell_cmd']
    assert 'DAX_PREVIEW_DIR=/home/user/work' in config['_shell_cmd']


def test_feature_webpreview_auto_assigns_port(monkeypatch):
    monkeypatch.setattr('dax._is_port_free', lambda p: True)
    config = {
        'cwd': '/home/user/project',
        '_container_home': '/home/user',
        'workdir': {'container': 'work'},
    }
    opts = feature_webpreview(config)
    assert '-p' in opts
    port_mapping = next(o for o in opts if ':' in o and o != '-p')
    host_port = int(port_mapping.split(':')[0])
    assert 8000 <= host_port < 9000
    assert 'DAX_PREVIEW_DIR=/home/user/work' in config['_shell_cmd']


def test_find_preview_port_is_deterministic():
    import dax
    orig = dax._is_port_free
    dax._is_port_free = lambda p: True
    try:
        p1 = _find_preview_port('/home/user/project-a')
        p2 = _find_preview_port('/home/user/project-a')
        assert p1 == p2
    finally:
        dax._is_port_free = orig


def test_find_preview_port_different_dirs_differ():
    import dax
    orig = dax._is_port_free
    dax._is_port_free = lambda p: True
    try:
        p1 = _find_preview_port('/home/user/project-a')
        p2 = _find_preview_port('/home/user/project-b')
        assert p1 != p2
    finally:
        dax._is_port_free = orig


def test_find_preview_port_skips_busy_ports(monkeypatch):
    import hashlib
    cwd = '/home/user/project'
    digest = int(hashlib.md5(cwd.encode()).hexdigest(), 16)
    first = 8000 + (digest % 1000)
    second = 8000 + ((first - 8000 + 1) % 1000)

    monkeypatch.setattr('dax._is_port_free', lambda p: p != first)
    port = _find_preview_port(cwd)
    assert port == second
