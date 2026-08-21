#!/usr/bin/env python3

import argparse
import hashlib
import os
import shlex
import shutil
import sys
import socket as _socket
import subprocess
from pathlib import Path
import yaml

from dax_creds.config import CLAUDE_SHARED_FILES, dir_basename, sync_claude_shared_files


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


# Every env wants these, unconditionally — mounting the project itself,
# dotfiles, and the webpreview server are not optional per-project choices.
# Baked in here rather than sourced from ~/.dax.yaml's top-level `features:`
# key (removed 2026-08) because that list was the actual cause of the
# additive-only, no-opt-out problem: an env could never decline something
# every other env also got by default. Per-env `features:` (on the project
# entry, a cwd-local .dax.yaml, or `-f`) remain purely opt-in, which is fine —
# nobody was ever trying to opt *out* of those.
_ALWAYS_ON_FEATURES = ('workdir', 'dotfiles', 'webpreview')


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

    # Never silently dropped: a leftover top-level `features:` key still gets
    # read here, just to flag it rather than let it quietly stop mattering.
    legacy_features = defaults.get('features') or []
    if legacy_features:
        dax_print("[!] ~/.dax.yaml's top-level `features:` ({}) is no longer read — "
                  "{} are always on; move anything else to a project's own "
                  "features:, a cwd-local .dax.yaml, or -f".format(
                      ', '.join(legacy_features), ', '.join(_ALWAYS_ON_FEATURES)))

    local_features = local.pop('features', [])
    defaults.update(local)
    defaults['features'] = list(_ALWAYS_ON_FEATURES) + local_features

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


# Mounted from the host's own ~/.claude into every state tree, nested at their
# usual CLAUDE_CONFIG_DIR-relative paths (decision B2, which replaced decision
# 11's four-directory `shared/` tree and its seeding copy).
#
# `commands` is the real user state — 11 commands in active use, and lost per
# project without this. `plugins` earns its place weakly: nothing is installed
# there, only the marketplace catalog Claude Code auto-installs and refreshes
# itself, so this avoids N redundant multi-megabyte clones rather than preserving
# anything.
#
# `skills` and `cache` were dropped. Skills arrive from the discernment sidecar
# (decision D), so a copy here would go stale against the repo. Cache is derived,
# generic, cheap to refetch, and the only one of the four where fetched content
# can turn account-specific.
_CLAUDE_HOST_SHARED_DIRS = ('commands', 'plugins')

# User-level *files* that a tree mount would otherwise replace, taking your global
# instructions and settings with it. Found 2026-07-31, after `fabric` had already
# been running without them: no error, just absent — which is the worst shape a
# loss can take.
#
# Attempted fix, same day: mount these read-only, nested inside the tree mount,
# the same way `_CLAUDE_HOST_SHARED_DIRS` nests `commands`/`plugins`. Abandoned
# within hours — unlike the directories, a single-*file* bind mount nested inside
# the tree mount did not deliver the host's content at all: the container saw an
# empty file, not the host's real `CLAUDE.md`. (Separately, a writable version of
# this would have hit the write-temp-plus-rename failure described in the design
# doc's Concurrency section; moot, since the read side never worked.)
#
# Replaced with a plain copy at `dax run` time — see
# `dax_creds.config.sync_claude_shared_files`. `_CLAUDE_HOST_SHARED_FILES` is
# `dax_creds.config.CLAUDE_SHARED_FILES`, imported at the top of this file;
# kept under this name here for the existing tests that import it from `dax`.
_CLAUDE_HOST_SHARED_FILES = CLAUDE_SHARED_FILES


def feature_claude_tenant_state(config):
    """Decision B: this env's state tree mounts at `~/.claude` itself.

    The opt-in replacement for `feature_claude`'s wholesale `~/.claude` mount —
    the two are mutually exclusive, since both target the same destination, and
    `cmd_run` refuses rather than letting Docker fail on a duplicate mount point.

    `CLAUDE_CONFIG_DIR` is a **constant**, not a selector: no cwd resolution, no
    per-session computation. It is set only because it is what relocates
    `.claude.json` *into* the tree. Unset, Claude Code writes `~/.claude.json` — a
    sibling of `~/.claude`, so outside the mount, container-local, and discarded
    on teardown, taking per-project trust and the `projects{}` block with it. The
    symptom is the first-run wizard reappearing, not an error.

    Deliberately does not set `DAX_TENANT_STATE`. That was Gate B's master switch,
    telling the wrapper to resolve a tenant from cwd and compute the path itself.
    The path is now handed down, so a wrapper that still contains Gate B (any
    image built before this change) simply skips it and honours what is set here —
    which is what makes this testable without an image rebuild.

    Tenant comes from the env's `~/.dax.yaml` entry (decision A: a declared
    grouping label), not a `.dax-tenant` file. Without one there is no path to
    mount, and pooling into a default is the isolation failure this design exists
    to prevent, so it refuses.

    Host directories may not exist yet for a brand-new env, and this creates
    `host_tree` itself rather than leaving it for Docker to auto-create.
    "Verified 2026-07-27: Docker auto-creates missing bind-mount host paths
    owned by the real host user rather than root" was true, but only ever
    tested on macOS Docker Desktop's own virtualized bind-mount layer -
    found live 2026-08 that native Linux (a real WSL2 setup) creates missing
    bind-mount host directories as root instead, so the container's own
    non-root user gets EACCES writing into its own state tree. It went
    unnoticed this long because `sync_claude_shared_files` below happens to
    `mkdir` the tree as an incidental side effect of seeding shared files
    into it - but only if the host already has at least one of
    CLAUDE_SHARED_FILES to seed, which anyone who's used Claude Code
    natively before already does. A genuinely first-time user, with no
    prior native Claude Code use on that host at all, has none of them -
    exactly the case that slipped through. Creating it explicitly here,
    unconditionally, removes the dependency on both Docker's
    platform-specific behavior and that incidental side effect.
    """
    project_name = config['workdir_name']
    tenant = config.get('tenant')
    if not tenant:
        dax_print('[!] claude_tenant_state needs a tenant for {}, and none is '
                  'declared.'.format(project_name))
        dax_print('    The state tree lives at '
                  '~/.local/state/dax/tenants/<tenant>/{}/, so there is nowhere'.format(
                      project_name))
        dax_print('    to mount without one. Set it with:')
        dax_print('      dax env set {} tenant <name>'.format(project_name))
        sys.exit(1)

    container_home = _container_home(config)
    cfg_dir = os.path.join(container_home, '.claude')
    host_tree = os.path.expanduser(
        os.path.join('~/.local/state/dax/tenants', tenant, project_name))

    # Created here, by dax's own process (running as the real host user),
    # rather than left for Docker to auto-create - on native Linux that
    # happens as root, which then locks the container's non-root user out
    # of its own state tree with EACCES. Idempotent and harmless if it
    # already exists.
    Path(host_tree).mkdir(parents=True, exist_ok=True)

    opts = [
        '-e', 'CLAUDE_CONFIG_DIR={}'.format(cfg_dir),
        '--volume={}:{}'.format(host_tree, cfg_dir),
    ]

    # Decision B2: mounted straight from the host's own ~/.claude rather than
    # copied into a shared/ tree, so there is one source of truth and no seeding
    # step. Nested inside the tree mount above — Docker orders mounts by
    # destination depth, so the deeper paths land after it.
    #
    # Skipped when absent rather than letting Docker create them: a host with no
    # commands of its own should not acquire an empty ~/.claude/commands as a side
    # effect of running a container.
    for shared_dir in _CLAUDE_HOST_SHARED_DIRS:
        host_shared = os.path.expanduser(os.path.join('~/.claude', shared_dir))
        if not os.path.isdir(host_shared):
            continue
        opts.append('--volume={}:{}'.format(
            host_shared, os.path.join(cfg_dir, shared_dir)))

    # Copied in rather than mounted (see the comment on _CLAUDE_HOST_SHARED_FILES
    # above) — a plain file write into the tree, picked up by the volume mount
    # already assembled for `host_tree` further up.
    for drifted in sync_claude_shared_files(tenant, project_name):
        dax_print('[!] {}: {} in state tree differs from both host and '
                  'last-synced copy — leaving it alone.'.format(project_name, drifted))
        dax_print('    Run `dax env accept-shared-files {}` to adopt the host '
                  'version, or inspect the diff yourself.'.format(project_name))

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


def _start_ssh_agent_bridge(agent_sock, port):
    """TCP-forward this run's ephemeral SSH agent socket into the
    container's reach.

    Bind-mounting the socket directly doesn't survive a Docker Desktop VM
    restart or a host sleep/wake cycle even when both real endpoints stay up
    the whole time - the container keeps the mount, but the live connection
    across the VM boundary can die. Exactly the class of problem
    `_start_creds_daemon` already solved for OAuth credentials by using TCP
    over `host.docker.internal` instead of a bind-mounted socket; this is the
    same fix applied to agent forwarding.

    Runs `dax_creds.ssh_bridge` (asyncio, in-process) rather than shelling out
    to `socat` on the host - `socat` is guaranteed inside the container image
    (`Dockerfile.tmpl`), never on the host, and stock macOS doesn't ship it.
    Found live 2026-08 when this first shipped: shelling to `socat` here
    worked in every sandbox that happened to have it installed and failed
    with ENOENT on a real Mac. Python is already guaranteed on the host - it's
    what's running this file - so there's nothing left to require.

    The module reconnects to the agent socket fresh on every incoming TCP
    connection, matching what `socat ...,fork ...` did: every agent request
    from the container re-resolves whatever the host socket currently is, not
    whatever it was when this bridge started.
    """
    cmd = [sys.executable, '-m', 'dax_creds.ssh_bridge',
           '--agent-sock', agent_sock, '--port', str(port)]
    log_path = Path.home() / '.dax' / 'ssh-bridge-{}.log'.format(port)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, 'a')
    dax_print("[+] starting SSH agent bridge (log: {})".format(log_path))
    return subprocess.Popen(cmd, cwd=str(Path(__file__).parent),
                            stdout=log_file, stderr=log_file)


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
    """Starts dax-preview in the background, then hands off to whatever the
    container's foreground process should be.

    Sets `config['_shell_cmd_prefix']` rather than building the whole
    foreground command itself: webpreview is a baseline feature (always
    on), so it runs before any opt-in feature that might also want a say in
    what the foreground process is (see `feature_auto_claude`) - `cmd_run`
    is what actually decides the final `exec` target, composing whatever
    prefix this sets with it. Baseline features always run first
    (`_ALWAYS_ON_FEATURES` is prepended unconditionally in `load_config`),
    so this ordering is reliable, not incidental.
    """
    port = config.get('webpreview', {}).get('port') or _find_preview_port(config['cwd'])
    container_home = _container_home(config)
    preview_dir = os.path.join(container_home, config['workdir_name'])
    dax_print("[-]   webpreview port: {}".format(port))
    config['_shell_cmd_prefix'] = 'DAX_PREVIEW_PORT={} DAX_PREVIEW_DIR={} dax-preview & '.format(
        port, preview_dir)
    return ['-p', '{}:{}'.format(port, port)]


def feature_auto_claude(config):
    """Skip the login shell: start tmux, name its one window after the
    project, and run `claude` directly as that window's command.

    Sets `config['_final_exec']` rather than building `_shell_cmd` outright,
    so this composes with `feature_webpreview`'s background dax-preview
    launcher (see `cmd_run`'s shell_cmd assembly) instead of silently
    dropping it — webpreview is a baseline feature and always runs, so its
    background launcher must keep running here too.

    Deliberately runs `claude` as the window's own command, not
    `claude; exec $SHELL`: exiting claude (`/exit` or otherwise) closes the
    window, which — since it's the session's only window — ends the tmux
    session, which ends the container's foreground process, which (`docker
    run --rm`) tears the container down. No shell left behind to fall into
    and nothing to reattach to, by design: the container itself is gone
    with it. `shlex.quote` because `workdir_name` can contain spaces or
    parens (an already-supported case — see feature_workdir's own tests).
    """
    window_name = shlex.quote(config['workdir_name'])
    config['_final_exec'] = 'tmux new-session -n {} claude'.format(window_name)
    return []


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


def feature_mounts(config):
    """Extra host directories mounted read-write as siblings under $HOME, on
    top of the project's own workdir mount — e.g. a migrated discernment
    process's own repo plus the discernment sidecar repo alongside it.

    Deliberately siblings under $HOME rather than nested inside another mount:
    nesting is exactly the class of bug that broke the CLAUDE.md/settings.json
    file mounts (see sync_claude_shared_files and the design doc's decision
    B2) — this sidesteps it rather than relying on directory nesting (which
    does work) staying that way.

    Named `mounts` on the project entry, comma-split by `dax env set` the same
    way `creds`/`features` are. Opt-in via `features: [mounts]`, same shape as
    `feature_ports`.

    Each entry is `<host_path>`, `<host_path>:<container_name>`, or
    `<host_path>:<container_name>:ro` — the name defaults to
    `dir_basename(host_path)` when omitted, but an explicit one is what lets
    a host directory be mounted under a name other than its own basename,
    e.g. the host's own `~/.claude` mounted as `~/host-claude` to inspect its
    real content from inside a container without colliding with whatever
    `claude_tenant_state` already mounted at `~/.claude` itself. A trailing
    `:ro` (the default container name still applies, e.g. `~/discernment::ro`)
    marks that one mount read-only — the same trailing-`:ro` Docker's own
    `--volume` syntax uses, and the same convention `feature_dotfiles`
    already applies to `dotfiles.ro`. Read-write unless present; nothing to
    opt into for the common case.
    """
    opts = []
    mounts = config.get('mounts', [])
    if not mounts:
        dax_print("[!] no mounts defined for this env")
        return opts
    container_home = _container_home(config)
    for entry in mounts:
        host_path, _sep, rest = entry.partition(':')
        container_name, _sep, mode = rest.partition(':')
        ro = mode == 'ro'
        host = os.path.expanduser(host_path)
        container = os.path.join(container_home, container_name or dir_basename(host))
        opts.append('--volume={}:{}{}'.format(host, container, ':ro' if ro else ''))
    return opts


def feature_substrate(config):
    """Mounts a virgil-style substrate repo (rw) and exposes it as
    $SUBSTRATE_ROOT — the container path both the substrate's own hooks and
    the `claude` wrapper's gate/settings-delivery logic read (see
    docs/design/VALIDATION.md's dax contract).

    A single path, unlike the generic `mounts` list feature_mounts handles:
    dax needs to know specifically which mount holds `shared/wire.sh` and
    `shared/new-process.sh`, not just that something extra is mounted.

    `wire.sh --quiet` itself runs once per container boot (dax-entrypoint.sh,
    baked into the image), not here and not on every `claude` launch — see
    the design doc's Part 4 for why that distinction is load-bearing.
    """
    substrate = config.get('substrate')
    if not substrate:
        dax_print("[!] no substrate configured for this env "
                  "(dax env set <name> substrate <path>)")
        return []
    host = os.path.expanduser(substrate)
    container = os.path.join(_container_home(config), dir_basename(host))
    return [
        '--volume={}:{}'.format(host, container),
        '-e', 'SUBSTRATE_ROOT={}'.format(container),
    ]


def _add_feature(feature, config):
    fn_name = 'feature_{}'.format(feature)
    fn = globals().get(fn_name)
    if fn is None:
        dax_print("[!] unknown feature: {}".format(feature))
        _print_features()
        sys.exit(-1)
    dax_print("[+] adding {}".format(feature))
    return fn(config)


def _feature_names():
    """Every feature a config may name, from the feature_* functions themselves."""
    return {name[len('feature_'):] for name in globals()
            if name.startswith('feature_')}


def _print_features():
    dax_print("[!] available features:")
    for name in sorted(_feature_names()):
        dax_print("\t{}".format(name))


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
    """Returns the real exit code (0 for a no-op test_only run) so callers
    that need to know whether this actually succeeded - `cmd_build`'s
    `docker build`/`docker tag` steps in particular - can check it. Nothing
    checked this before, which is how a failed `docker build` still reached
    "Commence to take over the world..." - subprocess.run() alone doesn't
    raise or report failure, it just silently returns."""
    dax_print("[-]   " + ' '.join(cmd))
    if test_only:
        return 0
    return subprocess.run(cmd).returncode


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
    if _runcmd(build_cmd, args.test_only) != 0:
        dax_print("[!] docker build failed — leaving ./Dockerfile in place to inspect")
        sys.exit(1)

    dax_print("[+] tagging container")
    # Expected to fail harmlessly on a first-ever build, when no prior
    # dax:latest tag exists yet to remove — not checked, unlike the two below.
    _runcmd(['docker', 'rmi', latest_tag], args.test_only)
    if _runcmd(['docker', 'tag', image_tag, latest_tag], args.test_only) != 0:
        dax_print("[!] docker tag failed — leaving ./Dockerfile in place to inspect")
        sys.exit(1)

    _runcmd(['/bin/rm', '-f', './Dockerfile', './ca.crt'], args.test_only)
    dax_print("[+] Commence to take over the world...")


def _start_creds_daemon(credentials, port):
    """TCP, not a bind-mounted Unix socket — Docker Desktop's Mac-VM file
    sharing does not reliably forward a live macOS-native Unix socket's
    connect/accept semantics into a container (regular-file bind mounts
    mostly work; a socket crossing that same boundary is much shakier).
    `dax creds login`'s daemon (`_start_login_daemon`) already hit this and
    switched to `tcp:host.docker.internal:<port>`; found 2026-08 that
    `cmd_run`'s persistent per-project daemon never got the same fix, so two
    or more projects running concurrently would each start their own daemon
    fine, but only the most recently started container's socket-forwarding
    actually worked — every other one saw ECONNREFUSED from `dax-creds list`
    despite its daemon process being alive and well on the host.
    """
    import json
    cmd = [
        sys.executable, '-m', 'dax_creds.daemon',
        '--tcp-port', str(port),
        '--credentials', json.dumps(credentials),
    ]
    log_path = Path.home() / '.dax' / f'creds-{port}.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
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


def _keyring_importable():
    """Whether `sys.executable` - the same interpreter `_start_creds_daemon`/
    `_start_login_daemon` spawn as a subprocess - can import `keyring`.

    `dax_creds.daemon`'s `__main__` block constructs `KeyringTokenStore()`
    unconditionally, with no fallback, so a missing `keyring` crashes the
    daemon subprocess immediately on an ImportError - one that never reaches
    the caller's terminal (stdout/stderr are redirected to its log file), so
    it just looks like "credential daemon did not start" after a silent
    5-second timeout with no indication why. Checking here, before spawning
    it, turns that into an immediate, actionable message instead. `keyring`
    is not declared anywhere in pyproject.toml (host or container extras) -
    it is only ever an out-of-band `pip install keyring`, which is exactly
    what's missing when this returns False.
    """
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False


_DOCKER_TWO_TOKEN_FLAGS = {'--name', '-h', '-v', '--volume', '-e', '-p', '-w', '--group-add', '-c'}


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

    # Same check `dax backup` runs by hand, just automatic now — quiet
    # unless something actually changed. A failure here (permissions, disk
    # full) is a warning, never a reason to block the actual container
    # launch, which is the thing this command is actually for.
    try:
        _run_backup(config, verbose=False)
    except Exception as e:
        dax_print("[!] backup check failed: {}".format(e))

    name = config['envname']
    # Lands the shell at the project mount instead of $HOME — workdir is one
    # of the always-on baseline features, so this path is exactly where
    # feature_workdir mounts it, every time.
    workdir = os.path.join(_container_home(config), config['workdir_name'])
    cmd = [
        'docker', 'run', '-it', '--rm',
        '--platform=linux/amd64',
        '--name', name,
        '-h', '{}.fatsec.docker'.format(name),
        '-w', workdir,
    ]

    daemon_proc = None
    ssh_bridge_proc = None
    project_creds = {}
    try:
        from dax_creds.config import (
            load_dax_config, find_named_project_by_dir, find_enclosing_project,
            get_project_credentials,
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
        # Decision A: a declared grouping label on the env's entry. This is what
        # feature_claude_tenant_state builds the state-tree path from — no
        # `.dax-tenant` file, no cwd resolution.
        config['tenant'] = project.get('tenant')

        # Per-env feature opt-in, which was never actually wired up: features came
        # only from the global `features:` list, a `.dax.yaml` in cwd, and `-f`.
        # The design doc, CLAUDE.md, and feature_claude_tenant_state's own
        # docstring all describe adding a feature to an env's own `features:` list
        # — and doing so did nothing at all. Without this there is no per-env
        # opt-in, so `claude_tenant_state` could only be switched on for every env
        # at once or passed by hand on every launch.
        for feature in project.get('features') or []:
            if feature not in config['features']:
                config['features'].append(feature)

        config['multi_tenant'] = bool(project.get('multi_tenant'))
        config['tenant_subdir'] = project.get('tenant_subdir', '')

        # feature_mounts reads this the same way feature_claude_tenant_state
        # reads config['tenant'] above — project entries aren't otherwise
        # promoted into the flat config feature functions see.
        config['mounts'] = project.get('mounts') or []
        config['substrate'] = project.get('substrate')

        # Two competing conventions have accumulated for "the default image":
        # a bare top-level `image:` (what load_config() above reads, and what
        # .dax.yaml.example documents) and `defaults: {image: ...}` (what
        # `dax process new`/`dax creds login`/`dax init`'s own fallback use).
        # A project's own `image:` was never promoted at all - silently
        # ignored, since this promotion never existed for it the way it does
        # for tenant/mounts/substrate above. Found live: a real ~/.dax.yaml
        # written in the `defaults:` shape, with no bare top-level `image:`
        # and no promotion for the project's own override, crashed `dax run`
        # outright with a bare KeyError. Project-specific wins, then whichever
        # of the two global-default spellings is actually set, then a
        # hardcoded fallback matching cmd_init's own — never a crash on a
        # missing key again.
        config['image'] = (project.get('image') or config.get('image') or
                           dax_config.get('defaults', {}).get('image') or 'dax-base')

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

            # Forwarding into the container turns on purely because an `ssh`
            # credential is configured — no separate `ssh` feature to also
            # remember (removed 2026-08; see docs/design/2026-08-04-codebase
            # -review.md). There is only ever one source now: this run's own
            # ephemeral agent above. No more ambient SSH_AUTH_SOCK or Docker
            # Desktop bridge-socket fallback — a project with no ssh
            # credential gets no forwarding, full stop, matching the same
            # explicit-over-ambient move already made for GitHub.
            try:
                port = _find_free_port()
                ssh_bridge_proc = _start_ssh_agent_bridge(agent_sock, port)
                if _wait_for_tcp('127.0.0.1', port):
                    if '--add-host=host.docker.internal:host-gateway' not in cmd:
                        cmd += ['--add-host=host.docker.internal:host-gateway']
                    cmd += ['-e', 'DAX_SSH_AGENT_TCP_PORT={}'.format(port)]
                else:
                    dax_print("[!] SSH agent bridge did not start — skipping")
                    ssh_bridge_proc.terminate()
                    ssh_bridge_proc = None
            except Exception as e:
                dax_print(f"[!] SSH agent bridge error: {e}")
                ssh_bridge_proc = None
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

    if 'claude_tenant_state' in features and 'claude' in features:
        # Both mount ~/.claude, so Docker would fail on a duplicate mount point
        # with a message that says nothing about which feature to remove. The
        # tenant-state tree is the *replacement* for the shared mount, not an
        # addition to it (decision B).
        dax_print('[!] features `claude` and `claude_tenant_state` both mount '
                  '~/.claude — pick one.')
        dax_print('    claude_tenant_state replaces the shared mount with this '
                  'env\'s own state tree.')
        dax_print('    Check this env\'s own `features:` list (and any -f flag) '
                  'and drop one of the two.')
        sys.exit(1)

    for feature in features:
        cmd.extend(_add_feature(feature, config))

    try:
        if project_creds and not _keyring_importable():
            dax_print("[!] keyring not importable in this Python environment — "
                      "skipping the credential daemon and launching without "
                      "credentials.")
            dax_print("    pip install keyring on whatever host is running `dax`.")
        elif project_creds:
            port = _find_free_port()
            daemon_proc = _start_creds_daemon(project_creds, port)
            if _wait_for_tcp('127.0.0.1', port):
                if '--add-host=host.docker.internal:host-gateway' not in cmd:
                    cmd += ['--add-host=host.docker.internal:host-gateway']
                cmd += ['-e', 'DAX_CREDS_SOCK=tcp:host.docker.internal:{}'.format(port)]
                cmd += ['-e', 'DAX_CREDS_NAMES={}'.format(','.join(project_creds.keys()))]
                seen_providers = set()
                for cred_name, cred_def in project_creds.items():
                    provider = cred_def.get('provider', '').upper()
                    if provider and provider not in seen_providers:
                        cmd += ['-e', 'DAX_CREDS_{}={}'.format(provider, cred_name)]
                        seen_providers.add(provider)
                dax_print("[-]   credentials: {}".format(list(project_creds.keys())))
            else:
                dax_print("[!] credential daemon did not start — skipping")
                daemon_proc.terminate()
                daemon_proc = None
    except Exception as e:
        dax_print(f"[!] credential daemon error: {e}")

    # Backstop for the case above's own `except (FileNotFoundError, KeyError):
    # pass` swallowing everything before config['image'] ever got set (no
    # ~/.dax.yaml yet, or some other early KeyError) - this must never crash
    # with a bare KeyError regardless of what happened above.
    config.setdefault('image', 'dax-base')
    cmd.append(config['image'])

    # The composition point for whatever the container's foreground process
    # should be. `_shell_cmd_prefix` (feature_webpreview's background
    # dax-preview launcher) and `_final_exec` (feature_auto_claude's tmux+
    # claude) are independent knobs set by different features that may or
    # may not both be active - neither overwrites the other outright, so
    # webpreview's background launcher survives whether or not auto_claude
    # also wants to replace the login shell with something else.
    if '_shell_cmd_prefix' in config or '_final_exec' in config:
        final = config.get('_final_exec') or os.environ.get('SHELL', '/bin/zsh')
        shell_cmd = config.get('_shell_cmd_prefix', '') + 'exec {}'.format(final)
        cmd += ['/bin/sh', '-c', shell_cmd]

    dax_print("[+] running:\n  " + _format_docker_cmd(cmd))
    try:
        if not args.test_only:
            subprocess.run(cmd)
    finally:
        if daemon_proc:
            dax_print("[+] stopping credential daemon")
            daemon_proc.terminate()
            daemon_proc.wait()
        if ssh_bridge_proc:
            dax_print("[+] stopping SSH agent bridge")
            ssh_bridge_proc.terminate()
            ssh_bridge_proc.wait()
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

    if not _keyring_importable():
        dax_print('[!] keyring not importable in this Python environment — the '
                  'credential daemon needs it to reach Keychain, and crashes '
                  'immediately without it.')
        dax_print('    If this is inside a dax container: credential logins are '
                  'host-only — exit and run `dax creds login` from your host '
                  'terminal instead.')
        dax_print('    Otherwise: pip install keyring on whatever host is '
                  'running `dax`.')
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
    from dax_creds.init import run_env_accept_shared_files, run_env_set, run_env_show

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
            run_env_set(config, name, args.field, args.value,
                        valid_features=_feature_names())
        elif args.env_command == 'accept-shared-files':
            run_env_accept_shared_files(config, name)
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


def _read_process_types(substrate_root):
    """[(type, skill, description), ...] from the substrate's own registry.

    Never hardcoded here — dax reads whatever the substrate declares, so
    adding a process type there never needs a dax release (see the "What dax
    must not do" section of docs/design/VALIDATION.md).
    """
    tsv = Path(substrate_root) / 'shared' / 'process-types.tsv'
    if not tsv.is_file():
        raise ValueError('no process-types.tsv at {} — is {} a substrate repo?'.format(
            tsv, substrate_root))
    types = []
    for line in tsv.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) >= 3:
            types.append((parts[0], parts[1], parts[2]))
    return types


def _check_process_dir(config, process_dir):
    """Refuse structurally bad choices for a new process directory, before
    anything is created or registered.

    Two classes of mistake, both cheap to catch here and expensive to
    unwind later: nesting/colliding with an already-registered project (the
    same mistake `find_enclosing_project`/Gate 0 catches for `dax run`, just
    much earlier — before there's anything to untangle), and landing outside
    $HOME, where `dax run` would refuse to launch it anyway (`load_config`'s
    "dax must be run from somewhere under your home dir"), just much later.
    """
    from dax_creds.config import find_enclosing_project

    home = Path.home().resolve()
    try:
        process_dir.relative_to(home)
    except ValueError:
        dax_print('[!] {} is not under your home directory ({}) — `dax run` '
                  'refuses to launch from outside $HOME'.format(process_dir, home))
        sys.exit(1)

    enclosing = find_enclosing_project(config, process_dir)
    if enclosing is not None:
        enclosing_name, enclosing_project = enclosing
        dax_print('[!] {} is inside already-registered project {!r} at {}'.format(
            process_dir, enclosing_name, Path(enclosing_project['dir']).expanduser().resolve()))
        sys.exit(1)

    for other_name, other_project in (config.get('projects') or {}).items():
        other_dir = other_project.get('dir')
        if not other_dir:
            continue
        other_dir = Path(other_dir).expanduser().resolve()
        if other_dir == process_dir:
            dax_print('[!] {} is already registered as project {!r}'.format(
                process_dir, other_name))
            sys.exit(1)
        if process_dir in other_dir.parents:
            dax_print('[!] {} would enclose already-registered project {!r} at {}'.format(
                process_dir, other_name, other_dir))
            sys.exit(1)


def _run_process_new(config, args):
    """Scaffold a new substrate-backed process and register it as a dax env.

    Every field is either given on the command line or interactively
    prompted — never silently defaulted, and never left to new-process.sh's
    own basic prompting: dax resolves everything itself first, then invokes
    the script with a complete, fully-resolved flag set so its own `ask()`
    prompts never trigger regardless of stdio.

    Directory and substrate paths are resolved to absolute, symlink-free
    paths (`.resolve()`) as soon as they're known — `register_project`
    documents `dir` as an absolute host path, a relative or `~`-shorthand
    value stored verbatim would silently break every cwd-based env lookup,
    and the confirmation summary below is only trustworthy if the paths in
    it are the real ones.
    """
    from dax_creds.init import _prompt, _q_confirm, _q_select, register_project, run_env_set, save_config
    from dax_creds.config import state_tree_path
    import questionary

    name = args.name

    process_dir = args.dir or _prompt('Process directory')
    if not process_dir:
        dax_print('[!] a process directory is required')
        sys.exit(1)
    process_dir = Path(process_dir).expanduser().resolve()

    _check_process_dir(config, process_dir)

    tenant = args.tenant or _prompt_tenant(process_dir, _known_tenant_names())

    substrate = args.substrate or _prompt('Substrate path', default=config.get('substrate'))
    if not substrate:
        dax_print('[!] a substrate path is required (pass --substrate, or set a top-level '
                  '`substrate:` default in ~/.dax.yaml)')
        sys.exit(1)
    substrate_root = Path(substrate).expanduser().resolve()

    title = args.title or _prompt('Process title')
    if not title:
        dax_print('[!] a process title is required')
        sys.exit(1)

    try:
        types = _read_process_types(substrate_root)
    except ValueError as e:
        dax_print('[!] {}'.format(e))
        sys.exit(1)

    if args.type:
        process_type = args.type
        skill = next((s for t, s, _d in types if t == process_type), None)
        if skill is None:
            dax_print('[!] unknown type {!r} — see {}/shared/process-types.tsv'.format(
                process_type, substrate_root))
            sys.exit(1)
    else:
        choices = [questionary.Choice('{} — {}'.format(t, desc), value=(t, skill))
                   for t, skill, desc in types]
        process_type, skill = _q_select('Process type:', choices)

    if args.git and args.no_git:
        dax_print('[!] pass --git or --no-git, not both')
        sys.exit(1)
    elif args.git:
        git_decision = True
    elif args.no_git:
        git_decision = False
    else:
        print('Keep this process under version control?')
        print('  git history cannot be selectively scrubbed later — a process that may')
        print('  need to be destroyed is cleaner without it.')
        git_decision = _q_confirm('git?', default=False)

    default_image = config.get('defaults', {}).get('image', 'dax-base')
    state_dir = state_tree_path(tenant, name)

    if process_dir.exists():
        count = sum(1 for _ in process_dir.iterdir())
        dir_note = 'already exists, empty' if count == 0 else \
            'already exists, {} item(s) inside'.format(count)
    else:
        dir_note = 'will be created'

    print()
    print('About to create a new process:')
    print('  env name    {}'.format(name))
    print('  directory   {}   [{}]'.format(process_dir, dir_note))
    print('  substrate   {}'.format(substrate_root))
    print('  tenant      {}'.format(tenant))
    print('  title       {}'.format(title))
    print('  type        {} (skill: {})'.format(process_type, skill))
    print('  git         {}'.format('yes' if git_decision else 'no'))
    print('  image       {}'.format(default_image))
    print('  creds       claude   (derived per-env credential)')
    print('  features    substrate, claude_tenant_state')
    print('  state tree  {}'.format(state_dir))
    print()

    if args.dry_run:
        dax_print('[-]   dry run — nothing created or registered')
        return

    if not _q_confirm('Proceed?', default=True):
        dax_print('[-]   aborted — nothing created or registered')
        return

    new_process_cmd = [
        str(substrate_root / 'shared' / 'new-process.sh'),
        '--dir', str(process_dir),
        '--title', title,
        '--type', process_type,
        '--git' if git_decision else '--no-git',
    ]
    result = subprocess.run(new_process_cmd)
    if result.returncode != 0:
        dax_print('[!] new-process.sh failed (exit {}) — not registering an env'.format(
            result.returncode))
        sys.exit(1)

    register_project(config, name=name, project_dir=process_dir,
                     image=default_image, creds=['claude'])
    save_config(config)
    run_env_set(config, name, 'tenant', tenant)
    run_env_set(config, name, 'substrate', str(substrate_root))
    # Always both, unconditionally — every substrate-backed process needs its
    # own Claude state tree as well as the substrate mount, never just one.
    run_env_set(config, name, 'features', 'substrate,claude_tenant_state',
                valid_features=_feature_names())

    dax_print('[+] registered {}. Run `dax run` from {} to launch it.'.format(name, process_dir))


def _uncommitted_git_note(dir_path):
    """A short "N uncommitted change(s)" note, or None if the directory isn't
    a git repo or has nothing outstanding. Surfaced in the destroy summary
    rather than silently lost — a `--git` process with no remote means this
    is the only copy of that work."""
    if not (dir_path / '.git').is_dir():
        return None
    try:
        result = subprocess.run(['git', '-C', str(dir_path), 'status', '--porcelain'],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    if not changed:
        return None
    return '! {} uncommitted git change(s)'.format(len(changed))


def _derived_creds_for(project, name):
    """[derived credential name, ...] for one project — never an
    explicitly-named/shared credential, only ones dax itself derived for
    this env specifically (see BARE_PROVIDER_CREDS)."""
    from dax_creds.config import resolve_credential_names
    try:
        resolved = resolve_credential_names(project, name)
    except ValueError:
        # A bare provider entry with no tenant to derive from — already
        # broken in a way `dax run`/`env show` would refuse on, and not
        # this command's job to fix. Nothing resolvable to clean up.
        return []
    return [cred_name for cred_name, provider in resolved if provider]


def _run_process_destroy(config, args):
    """Tear down one or more registered processes: directory, Claude state
    tree (plus its sibling shared-files manifest), any per-env derived
    credential, and the ~/.dax.yaml entry itself.

    docs/design/VALIDATION.md's `destroy-process` (item 6), generalized past
    substrate processes specifically — the same cleanup (dir + state tree +
    credential + registry entry) applies to any tenant-isolated env, and
    restricting this to substrate-tagged ones would only get in the way of
    burning down other test cruft.
    """
    from dax_creds.config import state_tree_path, _shared_files_manifest_path
    from dax_creds.init import (
        _q_checkbox, _container_name_for, _is_container_running,
        _tree_stats, _human_size, _human_age, run_creds_remove, save_config,
    )
    import questionary

    projects = config.get('projects') or {}

    names = list(args.name or [])
    if not names:
        if not projects:
            dax_print('[!] no registered envs to destroy')
            return
        choices = []
        for pname, project in sorted(projects.items()):
            tenant = project.get('tenant')
            label = '{}   [{}{}]'.format(
                pname, project.get('dir', '?'),
                ', tenant {}'.format(tenant) if tenant else ', no tenant')
            choices.append(questionary.Choice(label, value=pname))
        names = _q_checkbox('Select processes to destroy (nothing pre-checked):', choices)
        if not names:
            dax_print('[-]   nothing selected')
            return

    unknown = [n for n in names if n not in projects]
    if unknown:
        dax_print('[!] not registered: {}'.format(', '.join(unknown)))
        sys.exit(1)

    # Guardrails, checked for every candidate before any deletion begins — a
    # problem with one candidate must never leave the batch half-destroyed.
    for name in names:
        dir_path = Path(projects[name].get('dir', '')).expanduser()
        container = _container_name_for(dir_path)
        if _is_container_running(container):
            dax_print('[!] {} is running — stop it first (`docker stop {}`)'.format(
                name, container))
            sys.exit(1)

    print()
    print('About to destroy:')
    for name in names:
        project = projects[name]
        dir_path = Path(project.get('dir', '')).expanduser()
        tenant = project.get('tenant')

        print()
        print(name)
        if dir_path.exists():
            count = sum(1 for _ in dir_path.iterdir())
            note = '{} item(s)'.format(count)
            git_note = _uncommitted_git_note(dir_path)
            if git_note:
                note += '; ' + git_note
            print('  directory   {}   [{}]'.format(dir_path, note))
        else:
            print('  directory   {}   [already gone]'.format(dir_path))

        if tenant:
            state = state_tree_path(tenant, name)
            if state.exists():
                size, used = _tree_stats(state)
                print('  state tree  {}   [{}, used {} ago]'.format(
                    state, _human_size(size), _human_age(used)))
            else:
                print('  state tree  {}   [already gone]'.format(state))
        else:
            print('  state tree  (no tenant declared — none)')

        derived = _derived_creds_for(project, name)
        if derived:
            print('  credential  {}   (derived — also removed from Keychain)'.format(
                ', '.join(derived)))
        else:
            print('  credential  (none derived)')

        print('  registry    ~/.dax.yaml entry')

    print()
    print('{} process(es) above will be permanently destroyed. This cannot be undone.'.format(
        len(names)))
    print()

    if args.dry_run:
        dax_print('[-]   dry run — nothing destroyed')
        return

    typed = input('Type DESTROY to confirm: ')
    if typed != 'DESTROY':
        dax_print('[-]   aborted — nothing destroyed')
        return

    for name in names:
        project = projects[name]
        dir_path = Path(project.get('dir', '')).expanduser()
        tenant = project.get('tenant')

        if dir_path.exists():
            shutil.rmtree(dir_path)
            dax_print('[-]   removed {}'.format(dir_path))

        if tenant:
            state = state_tree_path(tenant, name)
            if state.exists():
                shutil.rmtree(state)
                dax_print('[-]   removed {}'.format(state))
            manifest = _shared_files_manifest_path(tenant, name)
            if manifest.exists():
                manifest.unlink()

        for cred_name in _derived_creds_for(project, name):
            try:
                run_creds_remove(config, cred_name)
            except Exception as e:
                dax_print('[!] could not remove credential {}: {}'.format(cred_name, e))

        del config['projects'][name]
        save_config(config)
        dax_print('[+] destroyed {}'.format(name))


def cmd_process(args):
    from dax_creds.config import load_dax_config

    try:
        config = load_dax_config()
    except FileNotFoundError:
        dax_print('[!] no ~/.dax.yaml found — run `dax init` first')
        sys.exit(1)

    if args.process_command == 'new':
        _run_process_new(config, args)
    elif args.process_command == 'destroy':
        _run_process_destroy(config, args)


def _run_backup(config, verbose=True, backup_dir=None):
    """Copy configured dotfiles/backup paths into this repo's own backup/,
    byte-for-byte comparison so only actually-changed files get copied.
    Shared by `dax backup` (verbose=True, the full report) and `cmd_run`
    (verbose=False - a startup banner reporting "unchanged" for every
    dotfile on every ordinary launch is noise, not signal; an actual backup
    is still always reported, regardless of verbose).

    Stays inside the repo checkout, deliberately - `~/.local/state/dax/`
    (the state_tree_path() convention `dax_creds/config.py` uses for
    per-project Claude state) looked like the obvious fix after a real
    ~/.dax.yaml (real client names, and for some providers real OAuth client
    secrets) landed in a diff about to be pushed to this repo's public
    remote, but it was solving the wrong half of the problem: it's not
    reliably persistent. Only paths a container's own feature functions
    explicitly bind-mount survive that container's teardown (the project's
    own workdir, dotfiles, ~/.claude via claude_tenant_state/claude) -
    ~/.local/state/dax/backup is not one of those, so on dax's own
    self-hosted dev loop (this repo, developed from inside a dax container)
    it would quietly vanish on every rebuild. The project's own workdir does
    survive exactly that, since it IS the bind mount - so backup/ stays
    right here. What actually closes the leak is .gitignore, not location:
    a gitignored path can never be `git add`ed regardless of what real
    content lands in it, on any host.

    `backup_dir` is still injectable so tests can point it at a tmp_path
    instead of writing into the real one.
    """
    home = os.path.expanduser('~')
    if backup_dir is None:
        backup_dir = Path(__file__).parent / 'backup'

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
    if verbose:
        for e in unchanged:
            dax_print("[-] unchanged: {}".format(e))
        for e in skipped:
            dax_print("[-] skipped (not found): {}".format(e))

    return updated, unchanged, skipped


def cmd_backup(args):
    try:
        with open(os.path.join(os.path.expanduser('~'), '.dax.yaml'), 'r') as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        dax_print("[!] no ~/.dax.yaml found")
        sys.exit(1)

    _run_backup(config, verbose=True)


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

    env_accept_p = env_sub.add_parser(
        'accept-shared-files',
        help="Force-adopt the host's CLAUDE.md/settings.json/settings.local.json "
             "into this env's state tree, resolving a drift warning from `dax run`")
    env_accept_p.add_argument('name', nargs='?', help='Env name (default: the env containing cwd)')

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

    process_p = subparsers.add_parser('process', help='Manage virgil-style substrate processes')
    process_sub = process_p.add_subparsers(dest='process_command', required=True)
    process_new_p = process_sub.add_parser(
        'new', help='Scaffold a new substrate-backed process and register it as a dax env')
    process_new_p.add_argument('name', help='Env name to register')
    process_new_p.add_argument('--dir', help='Host directory for the new process (prompted if omitted)')
    process_new_p.add_argument('--tenant', help='Tenant/grouping label (prompted if omitted)')
    process_new_p.add_argument(
        '--substrate',
        help="Path to the substrate repo (prompted if omitted, defaulting to the top-level "
             "`substrate:` in ~/.dax.yaml if set)")
    process_new_p.add_argument('--title', help='Process title (prompted if omitted)')
    process_new_p.add_argument('--type', help='Process type (prompted if omitted)')
    process_new_git = process_new_p.add_mutually_exclusive_group()
    process_new_git.add_argument('--git', action='store_true',
                                 help='Keep the process under version control')
    process_new_git.add_argument('--no-git', action='store_true',
                                 help='Do not put the process under version control')
    process_new_p.add_argument(
        '--dry-run', action='store_true',
        help='Resolve every field and print the full-path summary, then exit '
             'without creating or registering anything')

    process_destroy_p = process_sub.add_parser(
        'destroy',
        help='Permanently remove one or more processes: directory, state tree, '
             'derived credential, and registry entry')
    process_destroy_p.add_argument(
        'name', nargs='*',
        help='Env name(s) to destroy (omit for an interactive picker over every registered env)')
    process_destroy_p.add_argument(
        '--dry-run', action='store_true',
        help='Print the full-path summary of what would be destroyed, then exit '
             'without changing anything')

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
    elif args.command == 'process':
        cmd_process(args)


if __name__ == '__main__':
    main()
