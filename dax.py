#!/usr/bin/env python3

import argparse
import hashlib
import os
import shutil
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
        dax_print("[!] no ~/.dax.yaml found. Copy .dax.yaml.example to ~/.dax.yaml and configure it.")
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

    from dax_creds.config import dir_basename
    defaults['workdir_name'] = dir_basename(cwd)

    local_features = local.pop('features', [])
    defaults.update(local)
    defaults['features'].extend(local_features)

    return defaults




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
    container = os.path.join(_container_home(config), config['workdir_name'])
    return ['--volume={}:{}'.format(config['cwd'], container)]


def feature_optdir(config):
    return _add_volume(config, 'optdir')


def feature_aws(config):
    dax_print("[!]   WARNING: your AWS access tokens are available inside the dax container.")
    return _add_volume(config, 'awsdir')


def feature_claude(config):
    return _add_volume(config, 'claudedir')


# Shared across every tenant (decision 11 in the design doc): plugins/,
# skills/, commands/, and caches - nested into each project's own state tree
# so Claude Code finds them at their usual CLAUDE_CONFIG_DIR-relative path
# without duplicating them per tenant. 'cache' is an inferred addition
# (observed as a real top-level Claude Code directory during manual testing
# 2026-07-27) alongside the three the design doc names explicitly - not
# contractual, worth re-checking after version bumps like the rest of the
# seed mechanics.
_CLAUDE_TENANT_SHARED_DIRS = ('plugins', 'skills', 'commands', 'cache')


def feature_claude_tenant_state(config):
    # Sequencing step 6 (docs/design/2026-07-27-tenant-isolation.md): the
    # opt-in replacement for feature_claude's wholesale ~/.claude mount. A
    # project adds 'claude_tenant_state' to its own features: list (not
    # feature_claude's name) to switch this on - see the design doc's
    # Sequencing section for why this stays a separate feature rather than
    # replacing feature_claude outright.
    #
    # Host directories referenced here may not exist yet on first use for a
    # brand-new tenant/project. Verified 2026-07-27: Docker auto-creates
    # missing bind-mount host paths owned by the actual host user (not root),
    # so the container can write into them immediately - see the design
    # doc's Sequencing step 6 note for how this was checked.
    from dax_creds.tenant import all_tenant_projects

    project_name = config['workdir_name']
    multi_tenant = bool(config.get('multi_tenant'))
    tenant_subdir = config.get('tenant_subdir', '')
    repo_root = Path(config['cwd'])
    container_home = _container_home(config)
    host_root = os.path.expanduser('~/.local/state/dax')
    container_root = os.path.join(container_home, '.local/state/dax')

    opts = [
        '-e', 'DAX_TENANT_STATE=1',
        '-e', 'DAX_PROJECT_NAME={}'.format(project_name),
    ]
    if multi_tenant:
        opts += ['-e', 'DAX_MULTI_TENANT=1']
    if tenant_subdir:
        opts += ['-e', 'DAX_TENANT_SUBDIR={}'.format(tenant_subdir)]

    for tenant, project in sorted(all_tenant_projects(repo_root, multi_tenant, tenant_subdir)):
        host_dir = os.path.join(host_root, 'tenants', tenant, project)
        container_dir = os.path.join(container_root, 'tenants', tenant, project)
        opts.append('--volume={}:{}'.format(host_dir, container_dir))

        for shared_dir in _CLAUDE_TENANT_SHARED_DIRS:
            host_shared = os.path.join(host_root, 'shared', shared_dir)
            container_shared = os.path.join(container_dir, shared_dir)
            opts.append('--volume={}:{}'.format(host_shared, container_shared))

    return opts


def feature_auggie(config):
    return _add_volume(config, 'auggiedir')


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


_DOCKER_DESKTOP_SSH_SOCK = '/run/host-services/ssh-auth.sock'

def feature_ssh(config):
    sock = config.get('_ephemeral_ssh_sock', '')
    if not sock or not os.path.exists(sock):
        sock = os.environ.get('SSH_AUTH_SOCK', '')
    if not sock or not os.path.exists(sock):
        sock = _DOCKER_DESKTOP_SSH_SOCK
    if not sock or not os.path.exists(sock):
        dax_print("[!] No SSH agent socket found. Run ssh-add first.")
        return []
    return [
        '--volume', '{}:/ssh-agent'.format(sock),
        '-e', 'SSH_AUTH_SOCK=/ssh-agent',
        '--group-add', '0',
    ]


def _is_port_free(port):
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        try:
            s.bind(('', port))
            return True
        except OSError:
            return False


def _find_preview_port(cwd, base=8000, spread=1000):
    digest = int(hashlib.md5(cwd.encode()).hexdigest(), 16)
    candidate = base + (digest % spread)
    for _ in range(spread):
        if _is_port_free(candidate):
            return candidate
        candidate = base + ((candidate - base + 1) % spread)
    raise RuntimeError("no free port found in range {}-{}".format(base, base + spread - 1))


def feature_webpreview(config):
    port = config.get('webpreview', {}).get('port') or _find_preview_port(config['cwd'])
    shell = os.environ.get('SHELL', '/bin/zsh')
    container_home = _container_home(config)
    preview_dir = os.path.join(container_home, config['workdir_name'])
    dax_print("[-]   webpreview port: {}".format(port))
    config['_shell_cmd'] = 'DAX_PREVIEW_PORT={} DAX_PREVIEW_DIR={} dax-preview & exec {}'.format(
        port, preview_dir, shell)
    return ['-p', '{}:{}'.format(port, port)]


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
    if not _template_path().exists():
        dax_print("[!] Dockerfile.tmpl not found in current directory.")
        dax_print("[!] Run `dax build` from the dax source directory.")
        sys.exit(1)
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


def _start_creds_daemon(credentials, socket_path):
    import json
    cmd = [
        sys.executable, '-m', 'dax_creds.daemon',
        '--socket', str(socket_path),
        '--credentials', json.dumps(credentials),
    ]
    log_path = socket_path.with_suffix('.log')
    log_file = open(log_path, 'a')
    dax_print("[+] starting credential daemon (log: {})".format(log_path))
    return subprocess.Popen(cmd, cwd=str(Path(__file__).parent),
                            stdout=log_file, stderr=log_file)


def _start_login_daemon(credentials, port):
    import json
    cmd = [
        sys.executable, '-m', 'dax_creds.daemon',
        '--tcp-port', str(port),
        '--credentials', json.dumps(credentials),
    ]
    log_path = Path.home() / '.dax' / f'login-{port}.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, 'a')
    dax_print("[+] starting login credential daemon")
    proc = subprocess.Popen(cmd, cwd=str(Path(__file__).parent),
                             stdout=log_file, stderr=log_file)
    proc._dax_log_path = log_path
    return proc


def _find_free_port():
    import socket as _sock
    with _sock.socket() as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_for_socket(path, timeout=5.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _wait_for_tcp(host, port, timeout=5.0):
    import time, socket as _sock
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with _sock.socket() as s:
                s.settimeout(0.1)
                s.connect((host, port))
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.05)
    return False


_DOCKER_TWO_TOKEN_FLAGS = {'--name', '-h', '-v', '--volume', '-e', '-p', '--group-add', '-c'}


def _format_docker_cmd(cmd):
    """One flag (with its value, if it takes a separate one) per line.

    `cmd` mixes combined single tokens (`--volume=host:container`) and
    flag/value pairs (`-e`, `KEY=val`) depending on which feature built them;
    this reads correctly either way rather than assuming one form throughout.
    """
    lines = []
    i = 0
    while i < len(cmd):
        token = cmd[i]
        if token in _DOCKER_TWO_TOKEN_FLAGS and i + 1 < len(cmd):
            lines.append('{} {}'.format(token, cmd[i + 1]))
            i += 2
        else:
            lines.append(token)
            i += 1
    return '\n  '.join(lines)


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

    daemon_proc = None
    project_creds = {}
    try:
        from dax_creds.config import (
            load_dax_config, find_named_project_by_dir, find_enclosing_project,
            get_project_credentials, daemon_socket_path,
        )
        from dax_creds.providers.ssh import SshProvider, start_ephemeral_agent, stop_ephemeral_agent
        dax_config = load_dax_config()
        try:
            project_name, project = find_named_project_by_dir(dax_config, Path.cwd())
        except KeyError:
            enclosing = find_enclosing_project(dax_config, Path.cwd())
            if enclosing is not None:
                enclosing_name, enclosing_project = enclosing
                project_dir = Path(enclosing_project['dir']).expanduser()
                dax_print(
                    "[!] {} is inside registered project '{}' at {} but is "
                    "not its root. Run `dax run` from {}, then cd here "
                    "inside the container.".format(
                        Path.cwd(), enclosing_name, project_dir, project_dir))
                sys.exit(1)
            dax_print("[+] project not registered — starting dax init")
            from dax_creds.init import run_init
            dax_config = run_init(dax_config, Path.cwd())
            project_name, project = find_named_project_by_dir(dax_config, Path.cwd())
        try:
            project_creds = get_project_credentials(dax_config, project, project_name)
        except ValueError as e:
            # A bare provider token with no tenant to derive from. Refusing
            # beats silently falling back to a shared credential, which is the
            # exact isolation failure this convention exists to prevent.
            dax_print(f'[!] {e}')
            sys.exit(1)

        # Make synthesized definitions for derived names visible to everything
        # downstream that looks credentials up by name in the config — notably
        # cmd_creds_login below, which would otherwise raise KeyError on an
        # env's first run and have it swallowed by this block's except clause,
        # silently skipping both the login and the daemon.
        #
        # In memory only, deliberately: after a successful login the secret is
        # in Keychain, and the next run re-derives the same name and finds it.
        # Nothing needs persisting, so `dax run` stays a non-writer.
        for _name, _cdef in project_creds.items():
            dax_config.setdefault('credentials', {}).setdefault(_name, _cdef)
        config['multi_tenant'] = bool(project.get('multi_tenant'))
        config['tenant_subdir'] = project.get('tenant_subdir', '')

        ssh_creds = {n: d for n, d in project_creds.items() if d.get('provider') == 'ssh'}
        if ssh_creds:
            dax_print("[+] starting ephemeral SSH agent")
            agent_sock, agent_pid = start_ephemeral_agent()
            config['_ephemeral_ssh_sock'] = agent_sock
            config['_ephemeral_ssh_pid'] = agent_pid
            ssh_provider = SshProvider()
            for cred_name, cred_def in ssh_creds.items():
                dax_print(f"[-]   loading {cred_name} from Keychain")
                try:
                    ssh_provider.setup(cred_def, agent_sock=agent_sock)
                except RuntimeError as e:
                    dax_print(f"[!] failed to load {cred_name}: {e}")
        non_ssh_creds = {n: d for n, d in project_creds.items() if d.get('provider') != 'ssh'}
        if non_ssh_creds:
            from dax_creds.init import ensure_project_credentials
            missing = ensure_project_credentials(dax_config, non_ssh_creds)
            for cred_name in missing:
                dax_print(f'[!] {cred_name} not in Keychain.')
                answer = input(f'    Run `dax creds login {cred_name}` now? [Y/n] ').strip().lower()
                if answer in ('', 'y', 'yes'):
                    cmd_creds_login(cred_name, dax_config)
    except (FileNotFoundError, KeyError):
        pass

    features = list(config['features'])
    if args.features:
        features.extend(args.features.split(','))

    if args.ports:
        config.setdefault('ports', []).extend(args.ports)
        if 'ports' not in features:
            features.append('ports')

    if 'claude_tenant_state' in features:
        # Gate A, before mounts are computed: interactively fill in anything
        # undeclared (silent no-op if everything already is) - this is what
        # makes dax_creds/tenant.py's "no unattributed fallback, refuse
        # instead" rule not just friction. Safe to prompt here specifically
        # because nothing has launched yet; see _ensure_tenants_classified's
        # own docstring for why the same isn't true mid-session.
        _ensure_tenants_classified(config)

    for feature in features:
        cmd.extend(_add_feature(feature, config))

    try:
        if project_creds:
            from dax_creds.config import daemon_socket_path
            sock_path = daemon_socket_path(Path.cwd())
            daemon_proc = _start_creds_daemon(project_creds, sock_path)
            if _wait_for_socket(sock_path):
                container_sock = '/run/dax-creds.sock'
                cmd += ['-v', '{}:{}'.format(sock_path, container_sock)]
                cmd += ['-v', '{}:/run/dax-state:ro'.format(sock_path.parent)]
                cmd += ['-e', 'DAX_CREDS_SOCK={}'.format(container_sock)]
                cmd += ['-e', 'DAX_CREDS_NAMES={}'.format(','.join(project_creds.keys()))]
                seen_providers = set()
                for cred_name, cred_def in project_creds.items():
                    provider = cred_def.get('provider', '').upper()
                    if provider and provider not in seen_providers:
                        cmd += ['-e', 'DAX_CREDS_{}={}'.format(provider, cred_name)]
                        seen_providers.add(provider)
                dax_print("[-]   credentials: {}".format(list(project_creds.keys())))
            else:
                dax_print("[!] credential daemon socket did not appear — skipping")
                daemon_proc.terminate()
                daemon_proc = None
    except Exception as e:
        dax_print(f"[!] credential daemon error: {e}")

    cmd.append(config['image'])

    if '_shell_cmd' in config:
        cmd += ['/bin/sh', '-c', config['_shell_cmd']]

    dax_print("[+] running:\n  " + _format_docker_cmd(cmd))
    try:
        if not args.test_only:
            subprocess.run(cmd)
    finally:
        if daemon_proc:
            dax_print("[+] stopping credential daemon")
            daemon_proc.terminate()
            daemon_proc.wait()
        if config.get('_ephemeral_ssh_pid'):
            dax_print("[+] stopping ephemeral SSH agent")
            from dax_creds.providers.ssh import stop_ephemeral_agent
            stop_ephemeral_agent(config['_ephemeral_ssh_pid'])


def cmd_init(args):
    from dax_creds.config import load_dax_config
    from dax_creds.init import run_init
    try:
        config = load_dax_config()
    except FileNotFoundError:
        config = {'defaults': {'image': 'dax-base'}, 'credentials': {}, 'projects': {}}
    run_init(config, Path.cwd())


_LOGIN_PROVIDERS = {
    'github': {
        'mounts': ['~/.config/gh'],
        'auth_command': 'gh auth login --hostname github.com --git-protocol https --web',
    },
    'claude': {
        'mounts': ['~/.claude', '~/.dax-debug'],
        'auth_command': 'claude auth login',
    },
}


def _cmd_creds_login_auggie(cred_name, cred_def, config):
    login_url = cred_def.get('login_url', 'https://auth.augmentcode.com')
    dax_print(f'[+] dax creds login: auggie OAuth login URL: {login_url}')
    dax_print('')
    dax_print('    Auggie uses a localhost OAuth callback that cannot be automatically')
    dax_print('    brokered across the container/host boundary. To authenticate manually:')
    dax_print('')
    dax_print('    1. Pick a free port, e.g. 9876.')
    dax_print('    2. In one terminal, start auggie in a container and note the callback port:')
    dax_print(f'         docker run -it --rm --platform=linux/amd64 \\')
    dax_print(f'           -p 9876:9876 \\')
    dax_print(f'           -v ~/.augment:/home/{_get_username()}/.augment \\')
    dax_print(f'           dax:latest auggie --login-url {login_url} login --headless')
    dax_print('    3. When auggie prints its callback URL (e.g. http://127.0.0.1:35917/callback),')
    dax_print('       in another terminal start socat to forward your chosen port to it:')
    dax_print('         docker exec <container> socat TCP-LISTEN:9876,fork TCP:127.0.0.1:35917')
    dax_print('    4. In your browser, open the auth URL but replace the redirect_uri port')
    dax_print('       with your chosen port (9876).  Complete login.')
    dax_print('    5. Once ~/.augment/session.json is written, run:')
    dax_print(f'         dax creds add {cred_name}')
    dax_print('')
    _post_login_import(cred_name, cred_def, config, 'auggie')


def _cmd_creds_login_google(cred_name, cred_def, config):
    from dax_creds.providers.google import GoogleProvider
    provider = GoogleProvider(cred_def['provider'])

    browser = cred_def.get('browser', 'default')
    chrome_profile = cred_def.get('chrome_profile')
    if browser == 'chrome' and chrome_profile:
        from dax_creds.chrome import open_url_in_profile
        opener = lambda url: open_url_in_profile(url, chrome_profile)
    elif browser and browser != 'default':
        _app = {'firefox': 'Firefox', 'safari': 'Safari', 'arc': 'Arc',
                'brave': 'Brave Browser'}.get(browser, browser.title())
        opener = lambda url: subprocess.Popen(['open', '-a', _app, url])
    else:
        opener = lambda url: subprocess.Popen(['open', url])

    try:
        provider.acquire(cred_def, cred_name, prompter=dax_print, opener=opener)
    except (RuntimeError, ValueError) as e:
        dax_print(f'[!] {cred_name}: {e}')
        sys.exit(1)


def _login_credential_def(config, cred_name):
    """The definition to log in with — registered in `credentials:` or derived.

    A per-env Claude credential normally has *no* registry entry: its name is
    derived from the env's bare `claude` token (decision C2), and
    `_post_login_import` stores a Keychain secret without ever writing a
    `credentials:` block. So resolving against the registry alone refused every
    derived name, and since `dax creds add`'s claude path is the unguarded
    copy-from-disk route decision C3 exists to prevent, that left *no* sanctioned
    way to mint a per-env credential at all. `creds list`, `creds remove`, and
    `env show` already resolved derived names; login was missed. Found by manual
    step 4 on 2026-07-30 — see decision C5.

    An explicit registration wins, matching resolution everywhere else. The
    derived definition arrives via `synthesized_credential()`, so it carries the
    `credential_defaults` browser/profile settings (C4) rather than falling back
    to the default browser.

    Raises KeyError for a name that is neither, which `cmd_creds` renders as the
    "Unknown credential" message.
    """
    from dax_creds.config import derived_credentials

    cred_def = config.get('credentials', {}).get(cred_name)
    if cred_def is None:
        cred_def = derived_credentials(config).get(cred_name)
    if cred_def is None:
        raise KeyError(cred_name)
    return cred_def


def cmd_creds_login(cred_name, config):
    cred_def = _login_credential_def(config, cred_name)

    provider = cred_def.get('provider')
    if provider == 'auggie':
        _cmd_creds_login_auggie(cred_name, cred_def, config)
        return

    if provider in ('gmail', 'drive'):
        _cmd_creds_login_google(cred_name, cred_def, config)
        return

    provider_cfg = _LOGIN_PROVIDERS.get(provider)
    if not provider_cfg:
        print(f"dax creds login: no login flow defined for provider '{provider}'")
        sys.exit(1)

    image = config.get('defaults', {}).get('image', 'dax:latest')
    if not image.endswith(':latest') and ':' not in image:
        image = f'{image}:latest'
    if image == 'dax-base:latest':
        image = 'dax:latest'

    home = str(Path.home())
    port = _find_free_port()

    # Start daemon in TCP mode (avoids Docker Desktop Unix socket permission issues)
    creds_for_daemon = {cred_name: cred_def}
    daemon_proc = _start_login_daemon(creds_for_daemon, port)
    if not _wait_for_tcp('127.0.0.1', port):
        dax_print('[!] credential daemon did not start')
        daemon_proc.terminate()
        sys.exit(1)

    cmd = [
        'docker', 'run', '-it', '--rm',
        '--platform=linux/amd64',
        '--add-host=host.docker.internal:host-gateway',
        '-e', f'DAX_CREDS_SOCK=tcp:host.docker.internal:{port}',
        '-e', f'DAX_CREDS_LOGIN_CRED={cred_name}',
        '-e', 'BROWSER=dax-creds-open-url',
    ]

    for mount in provider_cfg['mounts']:
        host_path = os.path.expanduser(mount)
        container_path = host_path.replace(home, f'/home/{_get_username()}')
        Path(host_path).mkdir(parents=True, exist_ok=True)
        cmd += ['-v', f'{host_path}:{container_path}']

    cmd += [image, '/bin/sh', '-c', provider_cfg['auth_command']]

    # What the provider's on-disk location holds *before* the flow runs. The
    # import below reads that same location, and cannot otherwise tell a token
    # the login just wrote from one that was already sitting there — so an
    # abandoned login silently stored the pre-existing shared credential under
    # the new name, producing two Keychain entries backed by one OAuth grant.
    # That is the exact sharing the per-env naming exists to prevent, and it
    # presented as success. Observed 2026-07-30.
    before = _disk_token_for(cred_def, provider)

    dax_print(f'[+] dax creds login: starting auth flow for {cred_name} ({provider})')
    try:
        subprocess.run(cmd)
    finally:
        daemon_proc.terminate()
        dax_print(f'[-] login daemon log: {daemon_proc._dax_log_path}')

    # Post-login: import token to Keychain
    _post_login_import(cred_name, cred_def, config, provider, before=before)


def _disk_token_for(cred_def, provider):
    """Whatever the provider would import from disk right now, or None."""
    try:
        if provider == 'github':
            from dax_creds.providers.github import GitHubProvider
            return GitHubProvider().import_from_disk(cred_def)
        if provider == 'claude':
            from dax_creds.providers.claude import ClaudeProvider
            return ClaudeProvider().import_from_disk(cred_def)
        if provider == 'auggie':
            from dax_creds.providers.auggie import AuggieProvider
            return AuggieProvider().import_from_disk(cred_def)
    except Exception:
        pass
    return None


def _report_grant_collision(cred_name, other_name):
    from dax_creds.providers.claude import grant_collision_message

    lines = grant_collision_message(cred_name, other_name)
    dax_print(f'[!] {cred_name}: {lines[0]}')
    for line in lines[1:]:
        dax_print(f'    {line}')


def _post_login_import(cred_name, cred_def, config, provider, before=None):
    """Store whatever the auth flow just wrote to disk into Keychain.

    `before` is what that same on-disk location held beforehand. When the flow
    leaves it unchanged — the user abandoned the login, closed the browser, or
    it failed — importing would store a credential the flow did not create. For
    Claude that means copying an existing grant under a new name, silently
    defeating per-env isolation, so an unchanged token is refused rather than
    imported.
    """
    if before is not None:
        after = _disk_token_for(cred_def, provider)
        if after == before:
            dax_print(f'[!] {cred_name}: the auth flow did not write a new credential.')
            dax_print(f'    Nothing was imported — the token already on disk predates this '
                      f'login and storing it would share an existing grant.')
            dax_print(f'    Re-run `dax creds login {cred_name}` and complete the browser flow.')
            return

    if provider == 'github':
        from dax_creds.providers.github import GitHubProvider
        provider_obj = GitHubProvider()
        token = provider_obj.import_from_disk(cred_def)
        if token:
            provider_obj.store(cred_name, token)
            dax_print(f'[+] {cred_name}: token imported to Keychain.')
            _clear_gh_hosts_token()
        else:
            dax_print(f'[!] {cred_name}: no token found after auth flow.')
    elif provider == 'claude':
        from dax_creds.config import credential_names_for_provider
        from dax_creds.providers.claude import ClaudeProvider
        provider_obj = ClaudeProvider()
        token = provider_obj.import_from_disk(cred_def)
        if token:
            # The `before` comparison above only answers whether the file
            # changed, not whether the grant did — a Claude Code token refresh
            # landing inside the login window makes an abandoned login look
            # successful. Checking the grant itself closes that (decision C6).
            clash = provider_obj.grant_collision(
                cred_name, token, credential_names_for_provider(config, 'claude'))
            if clash:
                _report_grant_collision(cred_name, clash)
                return
            provider_obj.store(cred_name, token)
            dax_print(f'[+] {cred_name}: token imported to Keychain.')
        else:
            dax_print(f'[!] {cred_name}: no token found after auth flow.')
    elif provider == 'auggie':
        from dax_creds.providers.auggie import AuggieProvider
        provider_obj = AuggieProvider()
        token = provider_obj.import_from_disk(cred_def)
        if token:
            provider_obj.store(cred_name, token)
            dax_print(f'[+] {cred_name}: session imported to Keychain.')
            _clear_auggie_session()
        else:
            dax_print(f'[!] {cred_name}: no session found after auth flow.')


def _clear_gh_hosts_token():
    hosts_file = Path.home() / '.config' / 'gh' / 'hosts.yml'
    if not hosts_file.exists():
        return
    try:
        import yaml
        with open(hosts_file) as f:
            data = yaml.safe_load(f) or {}
        changed = False
        gh = data.get('github.com') or {}
        if 'oauth_token' in gh:
            del gh['oauth_token']
            changed = True
        for user_data in (gh.get('users') or {}).values():
            if 'oauth_token' in user_data:
                del user_data['oauth_token']
                changed = True
        if changed:
            with open(hosts_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False)
            dax_print('[-]   token removed from ~/.config/gh/hosts.yml')
    except Exception as e:
        dax_print(f'[!] could not clear hosts.yml: {e}')


def _clear_auggie_session():
    session_file = Path.home() / '.augment' / 'session.json'
    if not session_file.exists():
        return
    try:
        session_file.unlink()
        dax_print('[-]   session removed from ~/.augment/session.json')
    except Exception as e:
        dax_print(f'[!] could not clear ~/.augment/session.json: {e}')


def cmd_creds(args):
    from dax_creds.config import load_dax_config
    from dax_creds.init import run_creds_add, run_creds_list, run_creds_remove, run_creds_update
    try:
        config = load_dax_config()
    except FileNotFoundError:
        config = {'defaults': {'image': 'dax-base'}, 'credentials': {}, 'projects': {}}
    if args.creds_command == 'add':
        # A per-env credential's secret is only ever minted by its own login, so
        # `add` sets up the definition and then hands off rather than copying a
        # token off disk (decisions C, C6).
        config, pending_login = run_creds_add(config)
        if pending_login:
            print()
            cmd_creds_login(pending_login, config)
    elif args.creds_command == 'list':
        run_creds_list(config)
    elif args.creds_command == 'remove':
        try:
            run_creds_remove(config, args.name)
        except KeyError:
            print(f"Unknown credential '{args.name}'. Run `dax creds list` to see registered credentials.")
            sys.exit(1)
        except ValueError as e:
            dax_print(f'[!] {e}')
            sys.exit(1)
    elif args.creds_command == 'update':
        try:
            run_creds_update(config, args.name)
        except KeyError as e:
            print(f"Unknown credential {e}. Run `dax creds list` to see registered credentials.")
            sys.exit(1)
    elif args.creds_command == 'login':
        try:
            cmd_creds_login(args.name, config)
        except KeyError:
            print(f"Unknown credential '{args.name}'. Run `dax creds list` to see registered credentials.")
            sys.exit(1)


def cmd_envs(args):
    from dax_creds.config import load_dax_config
    from dax_creds.init import run_envs_list
    try:
        config = load_dax_config()
    except FileNotFoundError:
        config = {'defaults': {'image': 'dax-base'}, 'credentials': {}, 'projects': {}}
    if args.envs_command == 'list':
        run_envs_list(config)


def _resolve_env_name(config, explicit):
    """The name given on the command line, or the env that contains cwd.

    Defaulting to cwd is what makes `dax env show` answer "what am I in right
    now" without having to remember the registered name. Falls back to the
    *enclosing* project so it also works from a subdirectory, where `dax run`
    itself refuses (Gate 0).
    """
    if explicit:
        return explicit

    from dax_creds.config import find_enclosing_project

    cwd = Path.cwd()
    for name, proj in config.get('projects', {}).items():
        if Path(proj.get('dir', '')).expanduser() == cwd:
            return name

    enclosing = find_enclosing_project(config, cwd)
    if enclosing:
        return enclosing[0]

    dax_print(f'[!] {cwd} is not inside a registered env — pass a name explicitly')
    sys.exit(1)


def cmd_env(args):
    from dax_creds.config import load_dax_config
    from dax_creds.init import run_env_set, run_env_show

    try:
        config = load_dax_config()
    except FileNotFoundError:
        dax_print('[!] no ~/.dax.yaml found — run `dax init` first')
        sys.exit(1)

    name = _resolve_env_name(config, args.name)
    try:
        if args.env_command == 'show':
            run_env_show(config, name)
        elif args.env_command == 'set':
            run_env_set(config, name, args.field, args.value)
    except (KeyError, ValueError) as e:
        dax_print('[!] {}'.format(e.args[0] if e.args else e))
        sys.exit(1)


def cmd_features(args):
    _print_features()


def _known_tenant_names():
    """Every tenant name declared anywhere across every registered project on
    this host - the pick-list for the classification prompt below."""
    from dax_creds.config import load_dax_config
    from dax_creds.tenant import all_tenant_projects

    try:
        dax_config = load_dax_config()
    except FileNotFoundError:
        return set()

    names = set()
    for project_cfg in dax_config.get('projects', {}).values():
        proj_dir = Path(project_cfg.get('dir', '')).expanduser()
        if not proj_dir.is_dir():
            continue
        multi_tenant = bool(project_cfg.get('multi_tenant'))
        tenant_subdir = project_cfg.get('tenant_subdir', '')
        for tenant, _ in all_tenant_projects(proj_dir, multi_tenant, tenant_subdir):
            names.add(tenant)
    return names


_NEW_TENANT_CHOICE = '(new tenant)'


def _q_select_tenant(prompt, choices, default):
    import questionary
    return questionary.select(prompt, choices=choices, default=default).ask()


def _q_text_tenant(prompt, default):
    import questionary
    return questionary.text(prompt, default=default).ask()


def _prompt_tenant(subdir, known_tenants, default=None, _select=None, _text=None):
    """Interactively ask which tenant `subdir` belongs to.

    Offers a pick-list of tenants already known host-wide (reduces typo'd
    variants like 'Ysecurity' vs 'ysecurity' fragmenting one tenant into
    two), with an escape hatch to type a new one - falls straight to free
    text if nothing is known yet. `_select`/`_text` are injectable for
    testing, matching the `_picker` pattern already used in dax_creds/init.py
    - real questionary prompts otherwise.
    """
    _select = _select or _q_select_tenant
    _text = _text or _q_text_tenant

    choices = sorted(known_tenants)
    if choices:
        picked = _select("Tenant for {}:".format(subdir), choices + [_NEW_TENANT_CHOICE],
                          default if default in choices else None)
        if picked is None:
            raise KeyboardInterrupt
        if picked != _NEW_TENANT_CHOICE:
            return picked
    result = _text("Tenant name for {}:".format(subdir), default or '')
    if result is None or not result.strip():
        raise KeyboardInterrupt
    return result.strip()


def _ensure_tenants_classified(config, reclassify_all=False):
    """Interactively fill in (or, with reclassify_all, revise) tenant
    assignments for a claude_tenant_state project: the repo root, and every
    immediate child under tenant_subdir if multi-tenant.

    Runs at `dax run` time (Gate A), before mounts are computed - which is
    what makes doing this interactively safe here: nothing needs Docker to
    add a mount to an already-running container after the fact. A brand-new
    subdirectory discovered *mid-session* still just hard-refuses
    (dax_creds/tenant.py's resolve_tenant) - this function only ever runs
    before a container exists at all.

    reclassify_all=False (the automatic dax-run-time check): silently skips
    anything already declared - zero friction on routine use.
    reclassify_all=True (`dax tenant classify`): prompts for everything,
    defaulting to the current value, so pressing Enter keeps it and typing
    something new changes it.
    """
    from dax_creds.tenant import _read_tenant_label, TENANT_FILE, _NOT_A_PROJECT_SUBDIR

    repo_root = Path(config['cwd'])
    multi_tenant = bool(config.get('multi_tenant'))
    tenant_subdir = config.get('tenant_subdir', '')
    known = _known_tenant_names()

    def _classify(subdir):
        current = _read_tenant_label(subdir / TENANT_FILE)
        if current and not reclassify_all:
            return
        tenant = _prompt_tenant(subdir, known, default=current)
        (subdir / TENANT_FILE).write_text(tenant)
        known.add(tenant)

    _classify(repo_root)

    if multi_tenant:
        tenant_base = (repo_root / tenant_subdir) if tenant_subdir else repo_root
        if tenant_base.is_dir():
            for child in sorted(tenant_base.iterdir()):
                if not child.is_dir() or child.name in _NOT_A_PROJECT_SUBDIR:
                    continue
                _classify(child)


def cmd_tenant(args):
    if args.tenant_command == 'set':
        subdir = Path(args.subdir)
        if not subdir.is_dir():
            dax_print("[!] {} is not a directory".format(subdir))
            sys.exit(1)
        (subdir / '.dax-tenant').write_text(args.tenant)
        dax_print("[+] {}/.dax-tenant set to '{}'".format(subdir, args.tenant))
    elif args.tenant_command == 'classify':
        from dax_creds.config import load_dax_config, find_project_by_dir
        dax_config = load_dax_config()
        try:
            project = find_project_by_dir(dax_config, Path.cwd())
        except KeyError:
            dax_print("[!] {} is not a registered dax project - run `dax init` "
                      "first".format(Path.cwd()))
            sys.exit(1)
        config = {
            'cwd': str(Path.cwd()),
            'multi_tenant': bool(project.get('multi_tenant')),
            'tenant_subdir': project.get('tenant_subdir', ''),
        }
        _ensure_tenants_classified(config, reclassify_all=True)


def cmd_tenants(args):
    # Registry- and declaration-driven, not state-tree-driven: this re-derives
    # the live (tenant, project) set from ~/.dax.yaml's projects plus whatever
    # .dax-tenant files actually say right now, the same way dax run would -
    # so every declared tenant shows up immediately, not only ones that have
    # had a session, and a stale/renamed declaration can never show something
    # that no longer resolves that way.
    from dax_creds.config import load_dax_config, dir_basename
    from dax_creds.tenant import all_tenant_projects

    try:
        dax_config = load_dax_config()
    except FileNotFoundError:
        dax_print("[-] no ~/.dax.yaml found")
        return

    state_root = Path(os.path.expanduser('~/.local/state/dax/tenants'))
    rows = []  # (tenant, project, session, path) - matches display column order

    for project_cfg in dax_config.get('projects', {}).values():
        proj_dir = Path(project_cfg.get('dir', '')).expanduser()
        if not proj_dir.is_dir():
            continue
        multi_tenant = bool(project_cfg.get('multi_tenant'))
        tenant_subdir = project_cfg.get('tenant_subdir', '')
        tenant_base = (proj_dir / tenant_subdir) if (multi_tenant and tenant_subdir) else proj_dir
        reponame = dir_basename(proj_dir)

        for tenant, project in all_tenant_projects(proj_dir, multi_tenant, tenant_subdir):
            # project == reponame identifies the repo root's own entry (both
            # single-tenant and, since 2026-07-28, a multi-tenant repo's own
            # root project) - everything else is a labeled child under
            # tenant_base.
            path = proj_dir if project == reponame else (tenant_base / project)
            state_dir = state_root / tenant / project
            used = (state_dir / '.claude.json').exists() or (state_dir / '.credentials.json').exists()
            rows.append((tenant, project, 'yes' if used else 'no', str(path)))

    if not rows:
        dax_print("[-] no projects resolve to a tenant yet")
        return

    rows.sort(key=lambda r: (r[0], r[1]))

    headers = ('TENANT', 'PROJECT', 'SESSION', 'PATH')
    widths = [max(len(headers[i]), max(len(r[i]) for r in rows)) for i in range(4)]
    fmt = '  '.join('{{:<{}}}'.format(w) for w in widths)
    print()
    print(fmt.format(*headers))
    previous_tenant = None
    for row in rows:
        if previous_tenant is not None and row[0] != previous_tenant:
            print()
        print(fmt.format(*row))
        previous_tenant = row[0]
    print()


def cmd_backup(args):
    home = os.path.expanduser('~')
    repo_root = Path(__file__).parent
    backup_dir = repo_root / 'backup'

    try:
        with open(os.path.join(home, '.dax.yaml'), 'r') as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        dax_print("[!] no ~/.dax.yaml found")
        sys.exit(1)

    paths = []
    for f in config.get('dotfiles', {}).get('ro', []):
        paths.append(f)
    for f in config.get('dotfiles', {}).get('rw', []):
        paths.append(f)
    for f in config.get('backup', []):
        paths.append(f)

    seen = set()
    updated = []
    skipped = []
    unchanged = []

    for entry in paths:
        expanded = os.path.expanduser(entry.rstrip('/'))
        if expanded in seen:
            continue
        seen.add(expanded)

        if not os.path.exists(expanded):
            skipped.append(entry)
            continue

        rel = os.path.relpath(expanded, home)
        dest = backup_dir / rel

        if os.path.isdir(expanded):

            changed = False
            for src_root, dirs, files in os.walk(expanded):
                for fname in files:
                    src_file = Path(src_root) / fname
                    dst_file = dest / os.path.relpath(src_file, expanded)
                    dst_file.parent.mkdir(parents=True, exist_ok=True)
                    if not dst_file.exists() or src_file.read_bytes() != dst_file.read_bytes():
                        shutil.copy2(str(src_file), str(dst_file))
                        changed = True
            if changed:
                updated.append(entry)
            else:
                unchanged.append(entry)
        else:

            dest.parent.mkdir(parents=True, exist_ok=True)
            src_bytes = Path(expanded).read_bytes()
            if not dest.exists() or src_bytes != dest.read_bytes():
                shutil.copy2(expanded, str(dest))
                updated.append(entry)
            else:
                unchanged.append(entry)

    for e in updated:
        dax_print("[+] backed up: {}".format(e))
    for e in unchanged:
        dax_print("[-] unchanged: {}".format(e))
    for e in skipped:
        dax_print("[-] skipped (not found): {}".format(e))


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

    subparsers.add_parser('init', help='Register current directory as a dax project')
    subparsers.add_parser('features', help='List available features')
    subparsers.add_parser('backup', help='Back up dotfiles and config to backup/')

    creds_p = subparsers.add_parser('creds', help='Manage credentials')
    creds_sub = creds_p.add_subparsers(dest='creds_command', required=True)
    creds_sub.add_parser('add', help='Add or update a credential in the global store')
    creds_sub.add_parser('list', help='List registered credentials')
    remove_p = creds_sub.add_parser('remove', help='Remove a credential value from Keychain')
    remove_p.add_argument('name', help='Credential name to remove')
    update_p = creds_sub.add_parser('update', help='Update credential metadata (chrome profile, client_id, etc.)')
    update_p.add_argument('name', nargs='?', default=None, help='Credential name to update (omit to pick interactively)')
    login_p = creds_sub.add_parser('login', help='Authenticate a credential via its first-party auth flow')
    login_p.add_argument('name', help='Credential name to authenticate')

    envs_p = subparsers.add_parser('envs', help='Manage environments')
    envs_sub = envs_p.add_subparsers(dest='envs_command', required=True)
    envs_sub.add_parser('list', help='List registered environments')

    # Singular `env` acts on one environment, plural `envs` lists them — the
    # same split the existing `tenant`/`tenants` pair uses.
    from dax_creds.config import ENV_FIELDS, env_field_help
    env_p = subparsers.add_parser(
        'env', help='Inspect or edit a single environment',
        description='Operates on the env for the current directory when no name is given.')
    env_sub = env_p.add_subparsers(dest='env_command', required=True)

    env_show_p = env_sub.add_parser('show', help="Show one env's configuration")
    env_show_p.add_argument('name', nargs='?', help='Env name (default: the env containing cwd)')

    env_set_p = env_sub.add_parser(
        'set', help='Set a single field on an env',
        epilog=env_field_help(),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    env_set_p.add_argument('name', nargs='?', help='Env name (default: the env containing cwd)')
    env_set_p.add_argument('field', choices=sorted(ENV_FIELDS), help='Field to set')
    env_set_p.add_argument('value', help='New value')

    tenant_p = subparsers.add_parser('tenant', help='Manage tenant declarations')
    tenant_sub = tenant_p.add_subparsers(dest='tenant_command', required=True)
    tenant_set_p = tenant_sub.add_parser('set', help='Declare the tenant for a subdirectory')
    tenant_set_p.add_argument('subdir', help='Directory to declare (writes its .dax-tenant file)')
    tenant_set_p.add_argument('tenant', help='Tenant name to declare')
    tenant_sub.add_parser(
        'classify',
        help='Walk through every subdirectory needing a tenant (run from the project root); '
             're-prompts even already-declared ones, defaulting to the current value')

    subparsers.add_parser('tenants', help='List known tenants and their projects')

    args = parser.parse_args()

    if args.command == 'build':
        cmd_build(args)
    elif args.command == 'run':
        cmd_run(args)
    elif args.command == 'init':
        cmd_init(args)
    elif args.command == 'features':
        cmd_features(args)
    elif args.command == 'backup':
        cmd_backup(args)
    elif args.command == 'creds':
        cmd_creds(args)
    elif args.command == 'envs':
        cmd_envs(args)
    elif args.command == 'env':
        cmd_env(args)
    elif args.command == 'tenant':
        cmd_tenant(args)
    elif args.command == 'tenants':
        cmd_tenants(args)


if __name__ == '__main__':
    main()
