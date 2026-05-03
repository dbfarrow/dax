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
)


def test_feature_workdir_mounts_cwd():
    config = {
        'cwd': '/Users/dfarrow/work/project',
        'workdir': {'container': '/home/dfarrow/work'},
    }
    opts = feature_workdir(config)
    assert '--volume=/Users/dfarrow/work/project:/home/dfarrow/work' in opts


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
    monkeypatch.setattr('dax._SSH_AGENT_SOCK', str(sock))
    opts = feature_ssh({})
    assert any('ssh-agent' in o for o in opts)
    assert '-e' in opts
    assert 'SSH_AUTH_SOCK=/ssh-agent' in opts


def test_feature_ssh_warns_if_no_socket(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('dax._SSH_AGENT_SOCK', str(tmp_path / 'missing.sock'))
    opts = feature_ssh({})
    assert opts == []
