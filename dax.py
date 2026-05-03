#!/usr/bin/env python3

import os
import sys
import socket as _socket
from pathlib import Path
import yaml
from util import dax_print


def _template_path():
    return Path(__file__).parent / 'Dockerfile.tmpl'


def render_dockerfile(user, shell, passwd):
    content = _template_path().read_text()
    content = content.replace('%%USER%%', user)
    content = content.replace('%%SHELL%%', shell)
    content = content.replace('%%PASSWD%%', passwd)
    return content


def load_config():
    home = os.environ['HOME']
    cwd = os.getcwd()

    if not cwd.startswith(home):
        dax_print("[!] dax must be run from somewhere under your home dir")
        sys.exit(-1)

    with open(os.path.join(home, '.dax.yaml'), 'r') as f:
        defaults = yaml.safe_load(f)

    defaults['cwd'] = cwd

    dax_print("[+] looking for config file in {}".format(cwd))
    local_cfg_path = os.path.join(cwd, '.dax.yaml')
    local = {}
    if os.path.isfile(local_cfg_path):
        dax_print("[-]   config file is in {}".format(local_cfg_path))
        with open(local_cfg_path, 'r') as f:
            local = yaml.safe_load(f) or {}

    defaults['cfgdir'] = cwd
    defaults['envname'] = cwd.replace(home, '').lstrip('/').replace('/', '-')

    local_features = local.pop('features', [])
    defaults.update(local)
    defaults['features'].extend(local_features)

    return defaults


# Docker Desktop on Mac exposes the SSH agent via this fixed path
_SSH_AGENT_SOCK = '/run/host-services/ssh-auth.sock'


def _get_username():
    return os.environ.get('USER') or os.environ.get('LOGNAME')


def _container_home(config):
    return config.get('_container_home', '/home/{}'.format(_get_username()))


def _add_volume(config, feature_key):
    return ['--volume={}:{}'.format(config[feature_key]['host'], config[feature_key]['container'])]


def feature_workdir(config):
    cwd = config['cwd']
    if not cwd.startswith(os.environ.get('HOME', '')):
        dax_print("[+] DAX is running outside home... mounting cwd as working dir")
    return ['--volume={}:{}'.format(cwd, config['workdir']['container'])]


def feature_optdir(config):
    return _add_volume(config, 'optdir')


def feature_aws(config):
    dax_print("[!]   WARNING: your AWS access tokens are available inside the dax container.")
    return _add_volume(config, 'awsdir')


def feature_msf(config):
    opts = _add_volume(config, 'msfdir')
    opts += ['-p', '4444:4444']
    return opts


def feature_ovpn(config):
    dax_print("[!]   WARNING: ovpn feature not fully implemented.")
    return [
        '--cap-add=NET_ADMIN',
        '--device=/dev/net/tun',
        '--sysctl net.ipv6.conf.all.disable_ipv6=0',
    ]


def feature_ssh(config):
    if not os.path.exists(_SSH_AGENT_SOCK):
        dax_print("[!] SSH agent socket not found at {}. Run ssh-add first.".format(_SSH_AGENT_SOCK))
        return []
    return [
        '--mount', 'type=bind,src={},target=/ssh-agent'.format(_SSH_AGENT_SOCK),
        '-e', 'SSH_AUTH_SOCK=/ssh-agent',
    ]


def feature_dotfiles(config):
    opts = []
    container_home = _container_home(config)
    for f in config.get('dotfiles', {}).get('ro', []):
        host_path = os.path.expanduser(f)
        dest = os.path.join(container_home, os.path.basename(f))
        opts.append('--volume={}:{}:ro'.format(host_path, dest))
    for f in config.get('dotfiles', {}).get('rw', []):
        host_path = os.path.expanduser(f)
        dest = os.path.join(container_home, os.path.basename(f))
        opts.append('--volume={}:{}'.format(host_path, dest))
    return opts


def feature_X11(config):
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return [
        '-e', 'DISPLAY={}:0'.format(ip),
        '--volume', '/tmp/.X11-unix:/tmp/.X11-unix',
    ]


def feature_ports(config):
    opts = []
    ports = config.get('ports', [])
    if not ports:
        dax_print("[!] no ports defined in config")
        return opts
    for port in ports:
        opts += ['-p', port]
    return opts


def _add_feature(feature, config):
    fn_name = 'feature_{}'.format(feature)
    fn = globals().get(fn_name)
    if fn is None:
        dax_print("[!] unknown feature: {}".format(feature))
        _print_features()
        sys.exit(-1)
    dax_print("[+] adding {}".format(feature))
    return fn(config)


def _print_features():
    dax_print("[!] available features:")
    for name in sorted(globals()):
        if name.startswith('feature_'):
            dax_print("\t{}".format(name[len('feature_'):]))
