#!/usr/bin/env python3

import argparse
import os
import sys
import socket as _socket
import subprocess
from pathlib import Path
import yaml


def dax_print(msg):
    msg = msg.replace("[+]", '\033[92m' + "[+]" + '\033[0m')
    msg = msg.replace("[-]", '\033[93m' + "[-]" + '\033[0m')
    msg = msg.replace("[!]", '\033[91m' + "[!]" + '\033[0m')
    print(msg)


def _template_path():
    return Path(__file__).parent / 'Dockerfile.tmpl'


def render_dockerfile(user, shell, passwd, ca_cert_block=''):
    content = _template_path().read_text()
    content = content.replace('%%USER%%', user)
    content = content.replace('%%SHELL%%', shell)
    content = content.replace('%%PASSWD%%', passwd)
    content = content.replace('%%CA_CERT_BLOCK%%', ca_cert_block)
    return content


def _get_ca_cert_path():
    try:
        with open(os.path.join(os.environ['HOME'], '.dax.yaml'), 'r') as f:
            config = yaml.safe_load(f)
        path = config.get('ca_cert')
        if not path:
            return None
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            dax_print("[!] ca_cert not found at {}".format(path))
            return None
        return path
    except FileNotFoundError:
        return None


def load_config():
    home = os.environ['HOME']
    cwd = os.getcwd()

    if not cwd.startswith(home):
        dax_print("[!] dax must be run from somewhere under your home dir")
        sys.exit(-1)

    try:
        with open(os.path.join(home, '.dax.yaml'), 'r') as f:
            defaults = yaml.safe_load(f)
    except FileNotFoundError:
        dax_print("[!] no ~/.dax.yaml found. Copy dax-defaults.yaml to ~/.dax.yaml and configure it.")
        sys.exit(-1)

    defaults['cwd'] = cwd

    dax_print("[+] looking for config file in {}".format(cwd))
    local_cfg_path = os.path.join(cwd, '.dax.yaml')
    local = {}
    if os.path.isfile(local_cfg_path):
        dax_print("[-]   config file is in {}".format(local_cfg_path))
        with open(local_cfg_path, 'r') as f:
            local = yaml.safe_load(f) or {}

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
    entry = config[feature_key]
    if 'mount' in entry:
        host = os.path.expanduser(entry['mount'])
        container = entry['mount'].replace('~', _container_home(config), 1)
    else:
        host = os.path.expanduser(entry['host'])
        container = entry['container'].replace('~', _container_home(config), 1)
    return ['--volume={}:{}'.format(host, container)]


def feature_workdir(config):
    container = os.path.join(_container_home(config), config['workdir']['container'])
    return ['--volume={}:{}'.format(config['cwd'], container)]


def feature_optdir(config):
    return _add_volume(config, 'optdir')


def feature_aws(config):
    dax_print("[!]   WARNING: your AWS access tokens are available inside the dax container.")
    return _add_volume(config, 'awsdir')


def feature_claude(config):
    return _add_volume(config, 'claudedir')


def feature_github(config):
    return _add_volume(config, 'githubdir')


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
        host_path = os.path.expanduser(f.rstrip('/'))
        dest = os.path.join(container_home, Path(host_path).name)
        opts.append('--volume={}:{}:ro'.format(host_path, dest))
    for f in config.get('dotfiles', {}).get('rw', []):
        host_path = os.path.expanduser(f.rstrip('/'))
        dest = os.path.join(container_home, Path(host_path).name)
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


def _get_version():
    with open(Path(__file__).parent / 'VERSION', 'r') as f:
        return f.readline().split()[0]


def _get_dax_passwd():
    pwfile = Path(__file__).parent / '.daxpw'
    try:
        mode = os.stat(pwfile).st_mode
        if oct(mode)[-4:] == '0600':
            dax_print("[-]   reading password from .daxpw")
            return pwfile.read_text().split()[0]
        else:
            dax_print("[!] .daxpw must have permissions 0600")
            sys.exit(-1)
    except FileNotFoundError:
        dax_print("[-]   if you tire of typing a password, put it in .daxpw with chmod 0600")
        return input("Enter a password for the dax container: ")


def _get_user_build_args():
    username = _get_username()
    return [
        '--build-arg', 'user={}'.format(username),
        '--build-arg', 'user_id={}'.format(os.geteuid()),
        '--build-arg', 'user_gid={}'.format(os.getgid()),
    ]


def _runcmd(cmd, test_only=False):
    dax_print("[-]   " + ' '.join(cmd))
    if not test_only:
        subprocess.run(cmd)


def cmd_build(args):
    import shutil
    dax_print("[+] building Dockerfile from template")
    user = _get_username()
    shell = os.environ.get('SHELL', '/bin/zsh')
    passwd = _get_dax_passwd()

    ca_cert_path = _get_ca_cert_path()
    ca_cert_block = ''
    if ca_cert_path:
        dax_print("[-]   installing CA cert from {}".format(ca_cert_path))
        shutil.copy(ca_cert_path, './ca.crt')
        ca_cert_block = (
            '# install custom CA certificate\n'
            'COPY ca.crt /usr/local/share/ca-certificates/\n'
            'RUN update-ca-certificates\n'
        )

    dockerfile = render_dockerfile(user, shell, passwd, ca_cert_block)
    with open('Dockerfile', 'w') as f:
        f.write(dockerfile)

    version = _get_version()
    image_tag = 'dax:{}'.format(version)
    latest_tag = 'dax:latest'

    dax_print("[+] building container")
    build_cmd = ['docker', 'build'] + _get_user_build_args() + ['--platform=linux/amd64']
    if args.clean:
        build_cmd.append('--no-cache')
    build_cmd += ['-t', image_tag, '.']
    _runcmd(build_cmd, args.test_only)

    dax_print("[+] tagging container")
    _runcmd(['docker', 'rmi', latest_tag], args.test_only)
    _runcmd(['docker', 'tag', image_tag, latest_tag], args.test_only)

    _runcmd(['/bin/rm', '-f', './Dockerfile', './ca.crt'], args.test_only)
    dax_print("[+] Commence to take over the world...")


def cmd_run(args):
    config = load_config()
    username = _get_username()
    config['_container_home'] = '/home/{}'.format(username)

    name = config['envname']
    cmd = [
        'docker', 'run', '-it', '--rm',
        '--platform=linux/amd64',
        '--name', name,
        '-h', '{}.fatsec.docker'.format(name),
    ]

    features = list(config['features'])
    if args.features:
        features.extend(args.features.split(','))

    if args.ports:
        config.setdefault('ports', []).extend(args.ports)
        if 'ports' not in features:
            features.append('ports')

    for feature in features:
        cmd.extend(_add_feature(feature, config))

    cmd.append(config['image'])

    dax_print("[+] running: " + ' '.join(cmd))
    if not args.test_only:
        subprocess.run(cmd)


def cmd_features(args):
    _print_features()


def main():
    parser = argparse.ArgumentParser(description='DAX - Docker-based environment manager')
    parser.add_argument('-t', dest='test_only', action='store_true',
                        help='test only - print the docker command and exit')

    subparsers = parser.add_subparsers(dest='command', required=True)

    build_p = subparsers.add_parser('build', help='Build the dax Docker image')
    build_p.add_argument('-c', dest='clean', action='store_true',
                         help='clean build - do not use cached images')

    run_p = subparsers.add_parser('run', help='Launch a dax container')
    run_p.add_argument('-f', dest='features', help='comma-separated features to include')
    run_p.add_argument('-p', dest='ports', action='append',
                       help='port mapping host:container (repeatable)')

    subparsers.add_parser('features', help='List available features')

    args = parser.parse_args()

    if args.command == 'build':
        cmd_build(args)
    elif args.command == 'run':
        cmd_run(args)
    elif args.command == 'features':
        cmd_features(args)


if __name__ == '__main__':
    main()
