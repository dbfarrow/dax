#!/usr/bin/env python3

import argparse
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import sys
import socket as _socket
import subprocess
import tarfile
import tempfile
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


def _sync_and_clear_claude_credential(config, features, project_creds):
    """Write a `claude_tenant_state` env's live `.credentials.json` back to
    Keychain when the container stops, then delete it from disk — restoring
    the property the Keychain design was originally built around
    (credentials don't sit on disk when nothing is running), which the
    persistent per-env state tree quietly broke: the tree survives a
    container stop by design (that's the whole point of
    `claude_tenant_state`), and nothing ever copied its refreshed token back
    to Keychain, so it just sat there in plaintext indefinitely.

    Runs entirely host-side, in `cmd_run`'s existing teardown `finally:`
    (alongside stopping the credential daemon and SSH bridge) — the state
    tree is a plain host bind mount, so the file is just sitting on this
    machine's disk the whole time; no need to reach into the container, no
    wrapper changes, no signal handling.

    Deliberately best-effort, not gated on confirming the write actually
    landed before deleting: the failure mode of "delete anyway, write-back
    silently didn't happen" is a stale-but-usable (or, worst case, invalid)
    Keychain entry, which just means the next `dax creds login` for this
    env is a normal re-authentication — identical in kind to a first-ever
    login, not a special broken state. There's no unique secret being
    destroyed either way; an OAuth grant is trivially re-mintable.

    Once this deletes the file, the next launch's injection picks up
    whatever's freshest in Keychain — see `dax_creds/wrappers/claude`,
    which also had to learn this same "non-empty but dead" shape: its
    original `[ ! -s "$_dax_creds_file" ]` (non-zero size) inject condition
    had the identical blind spot this function did, so a dead-but-present
    file could survive a full stop/login/restart cycle untouched on that
    side too.
    """
    if 'claude_tenant_state' not in features:
        return
    cred_name = next((n for n, d in project_creds.items()
                       if d.get('provider') == 'claude'), None)
    if not cred_name:
        return

    from dax_creds.config import state_tree_path
    tenant = config.get('tenant')
    if not tenant:
        return
    cred_file = state_tree_path(tenant, config['workdir_name']) / '.credentials.json'
    if not cred_file.is_file() or cred_file.stat().st_size == 0:
        return

    try:
        from dax_creds.providers.claude import ClaudeProvider
        provider = ClaudeProvider()
        blob = provider.import_from_disk({}, credentials_file=cred_file)
        if blob is None:
            # Found live, 2026-08-31: Claude Code writes exactly this shape
            # when a refresh dies — a real, non-empty, JSON-valid
            # claudeAiOauth object with scopes/subscriptionType/rateLimitTier
            # intact but accessToken/refreshToken blanked to "". Not simply
            # "unknown data to be cautious about" - it's a confirmed-dead
            # credential at a path with exactly one legitimate content shape,
            # and *keeping* it is actively harmful: it's non-empty, so both
            # this check and the wrapper's own `-s` (non-zero size) inject
            # test treat it as "already have a credential" forever, which
            # permanently blocks re-injection from Keychain. There is no
            # version of this file that becomes useful later, so delete
            # unconditionally rather than leaving it in place "to be safe" —
            # the earlier, more cautious version of this function created a
            # real bug (a stale login this env could never recover from
            # short of manually deleting the file) trying to prevent a
            # non-existent risk (there's nothing here worth preserving).
            cred_file.unlink()
            dax_print('[!] {} was not a usable Claude credentials blob (dead '
                      'refresh token) — cleared so the next launch can '
                      'inject from Keychain'.format(cred_file))
            return
        provider.store(cred_name, blob)
        cred_file.unlink()
        dax_print('[-]   synced Claude credential to Keychain ({})'.format(cred_name))
    except Exception as e:
        dax_print('[!] could not sync Claude credential to Keychain: {}'.format(e))


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
        image = (project.get('image') or config.get('image') or
                dax_config.get('defaults', {}).get('image') or 'dax-base')
        # `dax-base` is what `dax init`/`dax process new` write into *every*
        # project entry as routine scaffolding boilerplate - it has never
        # been a real image (dax build only ever produces dax:<version>/
        # dax:latest) and was never meant to override anything. It was
        # harmless only because the promotion above didn't exist yet, so a
        # real top-level `image: dax:latest` always won regardless of what
        # every project entry happened to carry. The instant project-specific
        # actually started winning, every real project's inert placeholder
        # became load-bearing at once. cmd_creds_login already rewrites this
        # exact value for the exact same reason; cmd_run needs it too.
        if image in ('dax-base', 'dax-base:latest'):
            image = 'dax:latest'
        config['image'] = image

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
    # with a bare KeyError regardless of what happened above. dax:latest, not
    # dax-base - the latter has never been a real image (see the comment
    # above on the same confusion actually breaking a real run).
    config.setdefault('image', 'dax:latest')
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
        if not args.test_only:
            # -t is a pure preview (prints the docker command, launches
            # nothing) - deleting the real credential file as a side effect
            # of a dry run would be a bad surprise, so this is the one
            # teardown step in this block that's skipped for it.
            _sync_and_clear_claude_credential(config, features, project_creds)


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


def _process_dir_conflict(config, process_dir):
    """Returns an error message if process_dir is a structurally bad choice
    for a new process directory, or None if it's fine. Pure, non-fatal core
    of `_check_process_dir` — reused by `process restore`'s interactive
    move-restore prompt, which needs to report a bad answer and ask again
    rather than exit the whole restore session over one bad directory.

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
        return ('{} is not under your home directory ({}) — `dax run` '
                 'refuses to launch from outside $HOME'.format(process_dir, home))

    enclosing = find_enclosing_project(config, process_dir)
    if enclosing is not None:
        enclosing_name, enclosing_project = enclosing
        return '{} is inside already-registered project {!r} at {}'.format(
            process_dir, enclosing_name, Path(enclosing_project['dir']).expanduser().resolve())

    for other_name, other_project in (config.get('projects') or {}).items():
        other_dir = other_project.get('dir')
        if not other_dir:
            continue
        other_dir = Path(other_dir).expanduser().resolve()
        if other_dir == process_dir:
            return '{} is already registered as project {!r}'.format(process_dir, other_name)
        if process_dir in other_dir.parents:
            return '{} would enclose already-registered project {!r} at {}'.format(
                process_dir, other_name, other_dir)
    return None


def _check_process_dir(config, process_dir):
    """Refuse structurally bad choices for a new process directory, before
    anything is created or registered. See `_process_dir_conflict` for what
    counts as bad — this is just the fatal (sys.exit) wrapper around it."""
    problem = _process_dir_conflict(config, process_dir)
    if problem:
        dax_print('[!] {}'.format(problem))
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


# Rebuildable dependency/cache directories: excluded from `dax process
# export` because the project's own env setup (npm install, pip install,
# ...) regenerates them on the destination the first time it's used there,
# so shipping them across machines just costs archive size and transfer
# time for nothing. Matched by directory name at any depth, not just the
# top level - a monorepo's nested node_modules gets skipped too.
_EXPORT_SKIP_DIRS = {
    'node_modules', '__pycache__', '.venv', 'venv', 'venv-2.7', 'venv-3',
    '.pytest_cache', '.mypy_cache', '.tox',
}


def _export_tar_filter(tarinfo):
    if Path(tarinfo.name).name in _EXPORT_SKIP_DIRS:
        return None
    return tarinfo


def _state_tree_tar_filter(include_credential):
    """Excludes .credentials.json unless --include-credential was passed.

    The sibling shared-files manifest (<tenant>/<project>.shared-files.json)
    needs no explicit exclusion here at all - it lives beside
    state_tree_path(), never inside it, so it is never part of what gets
    handed to tar.add() in the first place. Carrying it over would risk a
    false drift warning on the destination's first `dax run` anyway, since
    "host" there means a different machine's global CLAUDE.md/settings.json.
    """
    def filt(tarinfo):
        if not include_credential and Path(tarinfo.name).name == '.credentials.json':
            return None
        return tarinfo
    return filt


def _extractall_permissive(tar, path):
    """`tar.extractall(path)`, using the 'tar' extraction filter when the
    running Python supports it, falling back to plain `extractall()`
    otherwise.

    'tar' (not the stricter default 'data' filter) matters because a
    substrate-backed process's state tree legitimately contains symlinks
    with absolute targets (wire.sh links each skill directory into the
    tree at container boot), which 'data's `AbsoluteLinkError` rejects
    outright — found live when a real `dax process import` failed on
    exactly this. 'tar' still refuses any member path that would escape
    the extraction directory, just permissive about symlink targets — the
    right tier for an archive dax itself just built, not arbitrary/
    untrusted input.

    Found live, 2026-08-30: `extractall()`'s `filter=` keyword didn't exist
    at all before Python 3.12 (PEP 706) — passing it unconditionally raised
    `TypeError: extractall() got an unexpected keyword argument 'filter'`
    on the real host, which runs its own Python 3.9 (this repo's
    `pyproject.toml` declares `requires-python = ">=3.7"`, so 3.9 is a
    real, supported target, not an edge case). The dev/test sandbox runs a
    newer Python, so every prior test run exercised only the 3.12+ path
    and never caught this. `hasattr(tarfile, 'tar_filter')` is the
    documented feature-detection check for the whole PEP 706 filter
    mechanism — pre-3.12, extractall() has no filtering concept at all, so
    the fallback is simply its old, always-permissive-about-symlinks
    behavior, which is what every version before this fix effectively ran
    anyway.
    """
    if hasattr(tarfile, 'tar_filter'):
        tar.extractall(path, filter='tar')
    else:
        tar.extractall(path)


def _dir_size_excluding(path, skip_dirs):
    total = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            try:
                total += os.lstat(os.path.join(root, fname)).st_size
            except OSError:
                continue  # vanished mid-walk — not worth failing a size estimate over
    return total


def _default_backup_dir(config=None):
    """The base directory `dax backup` and `dax process archive`/`restore`
    write into. A top-level `backup_dir:` in ~/.dax.yaml overrides it;
    otherwise this falls back to backup/ under this repo checkout.

    That fallback is deliberately not ~/.local/state/dax/...: only paths a
    container's own feature functions explicitly bind-mount survive that
    container's teardown, and this repo's own workdir mount is exactly such
    a path (see _run_backup's own docstring for the fuller history of why
    that matters here). Pointing `backup_dir:` at a path outside any mount
    (an external drive, a NAS share, ~/backups) trades that guarantee away —
    worth doing for real BCP/DR media, but the tradeoff is on the user
    making that choice, not silently assumed.
    """
    configured = (config or {}).get('backup_dir')
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).parent / 'backup'


def _archive_path_for(backup_dir, name, today=None):
    """backup_dir/daily/<name>-<YYYY-MM-DD>.tar.gz. Flat within the tier
    rather than nested per project — the filename and the manifest inside
    the tarball already carry every bit of identity a per-project directory
    would add, so that extra nesting bought nothing. `daily` anticipates
    rotation (not yet built): weekly/monthly/quarterly will land as sibling
    flat directories, so rotating a file is just moving it between two
    directories, never restructuring a per-project tree. `today` is
    injectable so tests get a deterministic filename instead of depending
    on the real date."""
    if today is None:
        today = datetime.date.today().isoformat()
    return backup_dir / 'daily' / '{}-{}.tar.gz'.format(name, today)


def _write_process_archive(config, name, out_path, include_credential=False):
    """Build the manifest and tarball for one registered env — the shared
    core of `process export` and `process archive`. Returns the manifest
    dict so the caller can print its own summary.

    Resolves the project/tenant/state-tree lookups itself rather than
    taking them from the caller, so it stays a single self-contained unit
    reusable from both commands regardless of whatever richer preview each
    one prints beforehand.
    """
    from dax_creds.config import state_tree_path

    projects = config.get('projects') or {}
    if name not in projects:
        known = ', '.join(sorted(projects)) or '(none registered)'
        dax_print('[!] no env named {!r}. Known envs: {}'.format(name, known))
        sys.exit(1)
    project = projects[name]

    process_dir = Path(project.get('dir', '')).expanduser().resolve()
    if not process_dir.is_dir():
        dax_print('[!] {} does not exist — nothing to archive'.format(process_dir))
        sys.exit(1)

    tenant = project.get('tenant')
    state_dir = state_tree_path(tenant, name) if tenant else None
    has_state = bool(state_dir and state_dir.is_dir())

    manifest = {
        'name': name,
        'dir': str(process_dir),
        'tenant': tenant,
        'image': project.get('image'),
        'creds': project.get('creds') or [],
        'features': project.get('features') or [],
        'mounts': project.get('mounts') or [],
        'substrate': project.get('substrate'),
        'had_state_tree': has_state,
    }

    out_path = Path(out_path).expanduser().resolve()
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / 'dax-export-manifest.json'
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, 'w:gz') as tar:
            tar.add(manifest_path, arcname='dax-export-manifest.json')
            tar.add(process_dir, arcname='project', filter=_export_tar_filter)
            if has_state:
                tar.add(state_dir, arcname='state',
                        filter=_state_tree_tar_filter(include_credential))

    return manifest


def _run_process_export(config, args):
    """Package one registered env's project directory and Claude state tree
    (if any) into a tarball for moving to another machine.

    Deliberately excludes the live Claude credential unless
    --include-credential is passed. Keychain-issued OAuth grants are per
    machine by design (see the credential-isolation decisions this whole
    project is built around) - copying the refresh token to a second
    machine while the first still exists means one grant authenticating
    from two places, with no local guard able to catch the collision, since
    Keychain never leaves the source machine for this to check against.
    Every *other* credential (github, gmail, auggie, ssh) never lived in
    either archived directory to begin with - they are Keychain-only, so
    the destination needs its own login for all of them regardless of this
    flag, not just the Claude one.

    substrate/mounts travel as the bare path strings from the registry
    entry, not their contents - the destination machine is expected to
    already have equivalent paths there, same as it needs its own logins.
    """
    from dax_creds.config import state_tree_path
    from dax_creds.init import _tree_stats, _human_size

    projects = config.get('projects') or {}
    name = args.name
    if name not in projects:
        known = ', '.join(sorted(projects)) or '(none registered)'
        dax_print('[!] no env named {!r}. Known envs: {}'.format(name, known))
        sys.exit(1)
    project = projects[name]

    process_dir = Path(project.get('dir', '')).expanduser().resolve()
    if not process_dir.is_dir():
        dax_print('[!] {} does not exist — nothing to export'.format(process_dir))
        sys.exit(1)

    tenant = project.get('tenant')
    state_dir = state_tree_path(tenant, name) if tenant else None
    has_state = bool(state_dir and state_dir.is_dir())
    has_claude_cred = has_state and (state_dir / '.credentials.json').exists()

    out_path = Path(args.out).expanduser().resolve() if args.out else \
        Path.cwd() / '{}-dax-export.tar.gz'.format(name)

    dir_size = _dir_size_excluding(process_dir, _EXPORT_SKIP_DIRS)
    state_size = _tree_stats(state_dir)[0] if has_state else 0

    creds = project.get('creds') or []

    print()
    print('About to export {}:'.format(name))
    print('  directory    {}   [{}]'.format(process_dir, _human_size(dir_size)))
    if has_state:
        print('  state tree   {}   [{}]'.format(state_dir, _human_size(state_size)))
        if has_claude_cred:
            if args.include_credential:
                print('  credential   included (--include-credential) — the live Claude '
                      'OAuth grant travels with this archive')
            else:
                print('  credential   excluded — `dax creds login` will be needed for it '
                      'on the destination')
    else:
        print('  state tree   (none — claude_tenant_state not in use)')
    if creds:
        print('  creds        {}   (Keychain-only — none of these travel; '
              're-authenticate all of them on the destination)'.format(', '.join(creds)))
    if project.get('substrate'):
        print('  substrate    {}   (path only — not its contents)'.format(project['substrate']))
    if project.get('mounts'):
        print('  mounts       {}   (paths only — not their contents)'.format(
            ', '.join(project['mounts'])))
    print('  output       {}'.format(out_path))
    print()

    if args.dry_run:
        dax_print('[-]   dry run — nothing written')
        return

    _write_process_archive(config, name, out_path, include_credential=args.include_credential)
    dax_print('[+] exported {} to {}'.format(name, out_path))


def _run_process_import(config, args):
    """Unpack an archive built by `dax process export` and register it as a
    new env on this machine.

    Runs the same _check_process_dir guardrails `process new` uses -
    landing an imported project outside $HOME or nested inside/around an
    already-registered project is exactly as bad here as it is there. Never
    pre-populates any credential beyond whatever the archive itself
    literally carried (nothing, unless the export side used
    --include-credential) - every provider needs its own fresh login here
    regardless, since Keychain never travels.

    Reads just the manifest out of the tar first (no full extraction) so a
    bad archive or a name collision fails immediately rather than after
    unpacking a potentially large project directory for nothing.
    """
    from dax_creds.init import _q_confirm, register_project, run_env_set, save_config
    from dax_creds.config import state_tree_path, resolve_credential_names

    archive = Path(args.archive).expanduser().resolve()
    if not archive.is_file():
        dax_print('[!] {} not found'.format(archive))
        sys.exit(1)

    with tarfile.open(archive) as tar:
        try:
            manifest_member = tar.getmember('dax-export-manifest.json')
        except KeyError:
            dax_print('[!] {} is not a dax export archive (no manifest found)'.format(archive))
            sys.exit(1)
        manifest = json.loads(tar.extractfile(manifest_member).read())

        name = args.name or manifest['name']
        if name in (config.get('projects') or {}):
            dax_print('[!] {!r} is already registered — pick a different --name'.format(name))
            sys.exit(1)

        process_dir = Path(args.dir).expanduser().resolve()
        _check_process_dir(config, process_dir)

        tenant = args.tenant or manifest.get('tenant')
        if manifest.get('had_state_tree') and not tenant:
            dax_print('[!] this archive has a Claude state tree but no tenant was given '
                      '(pass --tenant)')
            sys.exit(1)

        if process_dir.exists() and any(process_dir.iterdir()):
            dax_print('[!] {} already exists and is not empty — refusing to overwrite'.format(
                process_dir))
            sys.exit(1)
        dest_state = state_tree_path(tenant, name) if tenant else None
        if dest_state and dest_state.exists():
            dax_print('[!] {} already exists — refusing to overwrite'.format(dest_state))
            sys.exit(1)

        creds = manifest.get('creds') or []
        try:
            resolved_names = [n for n, _p in resolve_credential_names(
                {'creds': creds, 'tenant': tenant}, name)]
        except ValueError as e:
            resolved_names = None
            dax_print('[!] {}'.format(e))

        print()
        print('About to import {}:'.format(name))
        print('  directory    {}'.format(process_dir))
        print('  tenant       {}'.format(tenant or '(none)'))
        print('  image        {}'.format(manifest.get('image')))
        if creds:
            shown = ', '.join(resolved_names) if resolved_names else ', '.join(creds)
            print('  creds        {}   (none authenticated yet — run `dax creds login` '
                  'for each after this finishes)'.format(shown))
        if manifest.get('substrate'):
            print('  substrate    {}   (path only — must already exist on this machine)'.format(
                manifest['substrate']))
        if manifest.get('mounts'):
            print('  mounts       {}   (paths only — must already exist on this machine)'.format(
                ', '.join(manifest['mounts'])))
        print('  state tree   {}'.format(dest_state or '(none)'))
        print()

        if args.dry_run:
            dax_print('[-]   dry run — nothing imported or registered')
            return

        if not _q_confirm('Proceed?', default=True):
            dax_print('[-]   aborted — nothing imported or registered')
            return

        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            _extractall_permissive(tar, tmp)

            project_src = tmp / 'project'
            process_dir.parent.mkdir(parents=True, exist_ok=True)
            if project_src.is_dir():
                shutil.move(str(project_src), str(process_dir))
            else:
                process_dir.mkdir(parents=True, exist_ok=True)

            state_src = tmp / 'state'
            if manifest.get('had_state_tree') and state_src.is_dir():
                dest_state.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(state_src), str(dest_state))
                if manifest.get('dir'):
                    _relocate_claude_project_key(dest_state, manifest['dir'], process_dir)

        register_project(config, name=name, project_dir=process_dir,
                         image=manifest.get('image') or 'dax-base', creds=creds)
        save_config(config)
        if tenant:
            run_env_set(config, name, 'tenant', tenant)
        if manifest.get('features'):
            run_env_set(config, name, 'features', ','.join(manifest['features']),
                        valid_features=_feature_names())
        if manifest.get('mounts'):
            run_env_set(config, name, 'mounts', ','.join(manifest['mounts']))
        if manifest.get('substrate'):
            run_env_set(config, name, 'substrate', manifest['substrate'])

    dax_print('[+] imported {} at {}.'.format(name, process_dir))
    for cred_name in (resolved_names if creds and resolved_names else creds):
        dax_print('    dax creds login {}'.format(cred_name))
    dax_print('    then `dax run` from {} to launch it.'.format(process_dir))


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


def _teardown_process(config, name, project):
    """Delete one registered process's directory, Claude state tree (plus
    its sibling shared-files manifest), and any per-env derived credential —
    the shared core of `process destroy` and `process archive --remove`.

    Deliberately does not touch config['projects'] or call save_config: the
    caller owns the registry edit and when to save, since destroy saves
    once per name inside its own loop while `archive --remove` batches the
    registry edits and saves once after the whole set.
    """
    from dax_creds.config import state_tree_path, _shared_files_manifest_path
    from dax_creds.init import run_creds_remove

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


def _print_table(headers, rows):
    """Generic aligned-table printer: header row, dashed separator,
    per-column width computed from headers+data, left-justified cells.
    Same convention `dax envs list` (`run_envs_list`) already established.
    Standing preference (2026-08-30): any CLI output listing multiple
    records that each carry the same set of fields renders this way, not
    as repeated per-record "label   value" paragraphs — scanning N records'
    fields down aligned columns beats reading N separate paragraphs.
    """
    all_rows = [headers] + list(rows)
    widths = [max(len(str(r[i])) for r in all_rows) for i in range(len(headers))]

    def _line(cells):
        return '  '.join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print()
    print(_line(headers))
    print(_line(tuple('-' * w for w in widths)))
    for row in rows:
        print(_line(row))
    print()


def _print_teardown_table(rows):
    """rows: [(name, directory_cell, state_tree_cell, credential_cell), ...].
    Shared by `process destroy` and `process archive --remove`, which tear
    down the same four things."""
    _print_table(('name', 'directory', 'state tree', 'credential'), rows)


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
    from dax_creds.config import state_tree_path
    from dax_creds.init import (
        _q_checkbox, _container_name_for, _is_container_running,
        _tree_stats, _human_size, _human_age, _home_relative, save_config,
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

    rows = []
    for name in names:
        project = projects[name]
        dir_path = Path(project.get('dir', '')).expanduser()
        tenant = project.get('tenant')

        if dir_path.exists():
            note = '{} item(s)'.format(sum(1 for _ in dir_path.iterdir()))
            git_note = _uncommitted_git_note(dir_path)
            if git_note:
                note += '; ' + git_note
            dir_cell = '{}  [{}]'.format(_home_relative(dir_path), note)
        else:
            dir_cell = '{}  [already gone]'.format(_home_relative(dir_path))

        if tenant:
            state = state_tree_path(tenant, name)
            if state.exists():
                size, used = _tree_stats(state)
                state_cell = '{}  [{}, {} ago]'.format(
                    _home_relative(state), _human_size(size), _human_age(used))
            else:
                state_cell = '{}  [already gone]'.format(_home_relative(state))
        else:
            state_cell = '(no tenant declared)'

        derived = _derived_creds_for(project, name)
        cred_cell = ', '.join(derived) if derived else '(none derived)'

        rows.append((name, dir_cell, state_cell, cred_cell))

    _print_teardown_table(rows)
    print('{} process(es) above will be permanently destroyed (including each one\'s '
          '~/.dax.yaml entry). This cannot be undone.'.format(len(names)))
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
        _teardown_process(config, name, project)
        del config['projects'][name]
        save_config(config)
        dax_print('[+] destroyed {}'.format(name))


def _run_process_archive(config, args):
    """Snapshot one or more registered processes into
    backup_dir/daily/<name>-<date>.tar.gz — the BCP/DR backup mechanism
    itself, not a tidiness convenience (see
    docs/design/2026-08-22-process-archive-restore.md). Archiving is always
    non-destructive; `--remove` additionally tears each env down afterward,
    but only once every archive in the batch has already succeeded, and
    only after one combined confirmation covering the whole batch.

    Output is a single status line per target under one header, rather
    than a separate "about to..." preview followed by a repeated "archived
    X to Y" line — with backup_dir a shared prefix across every target,
    printing the full destination path twice per target was mostly noise;
    now it prints once, in the header.
    """
    from dax_creds.config import state_tree_path
    from dax_creds.init import (
        _q_checkbox, _q_confirm, _container_name_for, _is_container_running,
        _tree_stats, _human_size, _human_age, _home_relative, save_config,
    )
    import questionary

    projects = config.get('projects') or {}

    if args.all:
        names = sorted(projects)
        if not names:
            dax_print('[!] no registered envs to archive')
            return
    else:
        names = list(args.name or [])
        if not names:
            if not projects:
                dax_print('[!] no registered envs to archive')
                return
            choices = []
            for pname, project in sorted(projects.items()):
                tenant = project.get('tenant')
                label = '{}   [{}{}]'.format(
                    pname, project.get('dir', '?'),
                    ', tenant {}'.format(tenant) if tenant else ', no tenant')
                choices.append(questionary.Choice(label, value=pname))
            names = _q_checkbox('Select processes to archive (nothing pre-checked):', choices)
            if not names:
                dax_print('[-]   nothing selected')
                return

    unknown = [n for n in names if n not in projects]
    if unknown:
        dax_print('[!] not registered: {}'.format(', '.join(unknown)))
        sys.exit(1)

    def _dir_for(name):
        return Path(projects[name].get('dir', '')).expanduser()

    def _has_dir(name):
        return _dir_for(name).is_dir()

    # A stale/moved directory must skip that one target, not abort the
    # whole batch — this is the BCP mechanism ("back up everything on the
    # machine in one call"), so one bad registry entry can't be allowed to
    # take every other env's backup down with it. Checked up front (not
    # deep inside _write_process_archive, which used to hard sys.exit(1)
    # and take the whole batch with it) so --dry-run reports the same
    # outcome the real run would have.
    if not any(_has_dir(n) for n in names):
        dax_print('[!] nothing left to archive — every selected env is missing its directory')
        return

    # Same batch-atomicity guard as `destroy`: checked for every candidate
    # with a directory before any archive is written, so a bad --remove
    # target doesn't get discovered only after the rest of the batch
    # already succeeded. A target already missing its directory skips on
    # its own below regardless, so it's excluded here rather than treated
    # as "running".
    if args.remove:
        for name in names:
            if not _has_dir(name):
                continue
            container = _container_name_for(_dir_for(name))
            if _is_container_running(container):
                dax_print('[!] {} is running — stop it first (`docker stop {}`)'.format(
                    name, container))
                sys.exit(1)

    backup_dir = _default_backup_dir(config)
    today = datetime.date.today().isoformat()
    daily_dir = backup_dir / 'daily'
    out_paths = {name: _archive_path_for(backup_dir, name, today=today) for name in names}

    name_width = max(len(n) for n in names)

    print()
    print('Archiving {} process(es) to {} ({}):'.format(len(names), daily_dir, today))
    for name in names:
        padded = name.ljust(name_width)
        if not _has_dir(name):
            dax_print('  [!] {}   directory not found ({}), skipping'.format(
                padded, _dir_for(name)))
            continue
        if not args.dry_run:
            _write_process_archive(config, name, out_paths[name],
                                   include_credential=args.include_credential)
        dax_print('  [+] {}   {}'.format(padded, _dir_for(name)))

    available = [n for n in names if _has_dir(n)]

    if not args.remove:
        if args.dry_run:
            print()
            dax_print('[-]   dry run — nothing written')
        return

    rows = []
    for name in available:
        project = projects[name]
        dir_path = _dir_for(name)
        tenant = project.get('tenant')

        dir_cell = _home_relative(dir_path)
        if tenant:
            state = state_tree_path(tenant, name)
            if state.exists():
                size, used = _tree_stats(state)
                state_cell = '{}  [{}, {} ago]'.format(
                    _home_relative(state), _human_size(size), _human_age(used))
            else:
                state_cell = _home_relative(state)
        else:
            state_cell = '(no tenant declared)'
        derived = _derived_creds_for(project, name)
        cred_cell = ', '.join(derived) if derived else '(none derived)'

        rows.append((name, dir_cell, state_cell, cred_cell))

    print()
    if args.dry_run:
        print('Would then remove (after every archive above succeeds), pending one '
              'combined confirmation:')
        _print_teardown_table(rows)
        dax_print('[-]   dry run — nothing written')
        return

    print('All archives above succeeded — already backed up. About to remove:')
    _print_teardown_table(rows)

    if not _q_confirm('Remove {} archived process(es) now?'.format(len(available)), default=False):
        dax_print('[-]   archived only — nothing removed')
        return

    for name in available:
        _teardown_process(config, name, projects[name])
        del config['projects'][name]
    save_config(config)
    for name in available:
        dax_print('[+] removed {}'.format(name))


_ARCHIVE_FILENAME_RE = re.compile(r'^(?P<name>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.tar\.gz$')


def _available_archives(backup_dir):
    """{name: (archive_path, date_str)} — the newest dated archive per env
    name found under backup_dir/daily/. Filenames are
    <name>-YYYY-MM-DD.tar.gz; parsed back via a fixed-shape date suffix
    rather than a naive split, since `name` itself may contain hyphens."""
    daily_dir = backup_dir / 'daily'
    if not daily_dir.is_dir():
        return {}
    found = {}
    for f in sorted(daily_dir.glob('*.tar.gz')):
        m = _ARCHIVE_FILENAME_RE.match(f.name)
        if not m:
            continue
        name, date = m.group('name'), m.group('date')
        if name not in found or date > found[name][1]:
            found[name] = (f, date)
    return found


def _read_archive_manifest(archive):
    with tarfile.open(archive) as tar:
        try:
            member = tar.getmember('dax-export-manifest.json')
        except KeyError:
            dax_print('[!] {} is not a dax archive (no manifest found)'.format(archive))
            sys.exit(1)
        return json.loads(tar.extractfile(member).read())


def _relocate_claude_project_key(dest_state, old_dir, new_dir):
    """Rename `dest_state`'s Claude `projects/<key>/` directory so it's
    keyed to the *new* project location's basename, not the archived
    source's.

    Claude Code's own session index (`projects/<mangled-container-cwd>/`,
    which `/resume` reads) is keyed by the container's absolute cwd —
    `<container_home>/<basename-of-project-dir>`, exactly where
    `feature_workdir` mounts a project. Restoring a state tree copies that
    directory verbatim, so a name/location change (move-restore, or a
    plain restore's `--dir` landing on a differently-named path) leaves
    the transcripts sitting under the *old* basename's key — invisible to
    `/resume`, which looks up the *new*, current cwd's key.

    Found live, 2026-08-30: `fabric` restored (moved) as `denim` showed the
    right directory contents but no `/resume` history, because Claude Code
    was looking for `-home-<user>-denim` and only `-home-<user>-fabric`
    existed in the tree.

    Deliberately doesn't need the container's home path or username: the
    mangled key only ever differs in its trailing segment (the basename),
    so this matches on that suffix alone and rewrites just that part,
    whatever prefix precedes it. A no-op when the basename didn't change
    (the common in-place-restore case) or when there's nothing to rename.
    """
    old_base = Path(old_dir).name
    new_base = Path(new_dir).name
    if old_base == new_base:
        return
    projects_dir = dest_state / 'projects'
    if not projects_dir.is_dir():
        return
    old_suffix = '-' + old_base
    new_suffix = '-' + new_base
    for entry in list(projects_dir.iterdir()):
        if entry.is_dir() and entry.name.endswith(old_suffix):
            target = projects_dir / (entry.name[:-len(old_suffix)] + new_suffix)
            if not target.exists():
                entry.rename(target)


def _extract_process_archive(config, archive, manifest, name, process_dir, tenant):
    """Extract one archive's project/ (and state/, if the manifest says it
    had one) into place, deliberately replacing whatever's already there
    (a restore is always a replace, never a merge), then register `name`
    at `process_dir` with `tenant` and the manifest's
    image/creds/features/mounts/substrate. The one extraction+registration
    core shared by every restore path — in place, moved to a new name/
    location, or batch/--all — so it exists exactly once. Returns the
    manifest's creds list, for the caller to print login reminders from.
    """
    from dax_creds.init import register_project, run_env_set, save_config
    from dax_creds.config import state_tree_path

    dest_state = state_tree_path(tenant, name) if tenant else None

    with tarfile.open(archive) as tar, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _extractall_permissive(tar, tmp)

        project_src = tmp / 'project'
        if process_dir.exists():
            shutil.rmtree(process_dir)
        process_dir.parent.mkdir(parents=True, exist_ok=True)
        if project_src.is_dir():
            shutil.move(str(project_src), str(process_dir))
        else:
            process_dir.mkdir(parents=True, exist_ok=True)

        state_src = tmp / 'state'
        if manifest.get('had_state_tree') and state_src.is_dir():
            if dest_state.exists():
                shutil.rmtree(dest_state)
            dest_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(state_src), str(dest_state))
            if manifest.get('dir'):
                _relocate_claude_project_key(dest_state, manifest['dir'], process_dir)

    creds = manifest.get('creds') or []
    register_project(config, name=name, project_dir=process_dir,
                     image=manifest.get('image') or 'dax-base', creds=creds)
    save_config(config)
    if tenant:
        run_env_set(config, name, 'tenant', tenant)
    if manifest.get('features'):
        run_env_set(config, name, 'features', ','.join(manifest['features']),
                    valid_features=_feature_names())
    if manifest.get('mounts'):
        run_env_set(config, name, 'mounts', ','.join(manifest['mounts']))
    if manifest.get('substrate'):
        run_env_set(config, name, 'substrate', manifest['substrate'])
    return creds


def _print_restore_login_reminders(creds, tenant, name):
    if not creds:
        return
    from dax_creds.config import resolve_credential_names
    try:
        resolved_names = [n for n, _p in resolve_credential_names(
            {'creds': creds, 'tenant': tenant}, name)]
    except ValueError as e:
        dax_print('[!] {}'.format(e))
        resolved_names = creds
    for cred_name in resolved_names:
        dax_print('    dax creds login {}'.format(cred_name))


def _print_restore_preview(heading, process_dir, tenant, manifest, dest_state, name_for_creds):
    """Full preview of exactly what would land in ~/.dax.yaml — directory,
    tenant, image, creds (resolved to the derived name a bare provider
    entry would produce, when resolvable), features, mounts, substrate,
    and state tree — printed identically for a dry run and a real run, so
    `--dry-run` is a genuine preview of the registry entry `register_project`/
    `run_env_set` would otherwise write, not just a same/different-dir note.
    Mirrors `_run_process_import`'s preview block, which this was modeled on.
    """
    from dax_creds.config import resolve_credential_names

    creds = manifest.get('creds') or []
    try:
        resolved_names = [n for n, _p in resolve_credential_names(
            {'creds': creds, 'tenant': tenant}, name_for_creds)]
    except ValueError:
        resolved_names = None

    print()
    print(heading)
    print('  directory    {}'.format(process_dir))
    print('  tenant       {}'.format(tenant or '(none)'))
    print('  image        {}'.format(manifest.get('image')))
    if creds:
        shown = ', '.join(resolved_names) if resolved_names else ', '.join(creds)
        print('  creds        {}   (a name already defined in ~/.dax.yaml is just '
              're-referenced, never redefined — run `dax creds login` for any that '
              'still need a fresh grant)'.format(shown))
    if manifest.get('features'):
        print('  features     {}'.format(', '.join(manifest['features'])))
    if manifest.get('substrate'):
        print('  substrate    {}   (path only — must already exist on this machine)'.format(
            manifest['substrate']))
    if manifest.get('mounts'):
        print('  mounts       {}   (paths only — must already exist on this machine)'.format(
            ', '.join(manifest['mounts'])))
    print('  state tree   {}'.format(dest_state or '(none)'))
    print()


def _restore_preview_row(config, name, archive, args):
    """One row for the batch (`--all`/multiple explicit names) `--dry-run`
    summary table: name, directory, tenant, image, creds, state tree, and
    what would happen. Mirrors `_restore_in_place`'s own branching
    (registered vs not, missing-tenant, occupied-path/`_process_dir_conflict`)
    but only computes and reports — never prompts, never extracts. A
    single-target interactive restore still gets the fuller
    `_print_restore_preview` treatment via `_restore_in_place` directly;
    this is specifically for previewing a whole batch at once, the same
    "many records, same fields, tabular" shape `_print_teardown_table`
    already established for destroy/archive --remove.
    """
    from dax_creds.config import resolve_credential_names, state_tree_path
    from dax_creds.init import _home_relative

    manifest = _read_archive_manifest(archive)
    projects = config.get('projects') or {}
    registered = name in projects

    if registered:
        process_dir = Path(projects[name]['dir']).expanduser().resolve()
    elif args.dir:
        process_dir = Path(args.dir).expanduser().resolve()
    else:
        process_dir = Path(manifest['dir']).expanduser().resolve()

    tenant = manifest.get('tenant')
    creds = manifest.get('creds') or []
    try:
        resolved_names = [n for n, _p in resolve_credential_names(
            {'creds': creds, 'tenant': tenant}, name)]
    except ValueError:
        resolved_names = None
    creds_cell = ', '.join(resolved_names) if resolved_names else (', '.join(creds) or '(none)')

    dest_state = state_tree_path(tenant, name) if tenant else None

    if manifest.get('had_state_tree') and not tenant:
        outcome = 'would skip — no tenant recorded'
    elif registered:
        outcome = 'would prompt to confirm overwrite'
    elif process_dir.exists() and any(process_dir.iterdir()):
        outcome = 'would skip — directory occupied'
    else:
        problem = _process_dir_conflict(config, process_dir)
        outcome = 'would skip — {}'.format(problem) if problem else 'would restore'

    return (name, _home_relative(process_dir), tenant or '(none)',
            manifest.get('image') or '?', creds_cell,
            _home_relative(dest_state) if dest_state else '(none)', outcome)


def _restore_in_place(config, name, archive, args):
    """Restore `name` at its manifest (or, if already registered, currently
    registered) directory — the shared logic behind a non-interactive
    `process restore <name>` and the interactive picker's "restore in
    place" choice.

    Deliberately targets the *currently registered* dir when `name` is
    already registered, not the manifest's — the project may have moved
    since it was archived, and the live registry entry is the more current
    answer to "where does this actually live now."
    """
    from dax_creds.init import _q_confirm
    from dax_creds.config import state_tree_path

    manifest = _read_archive_manifest(archive)
    projects = config.get('projects') or {}
    registered = name in projects

    if registered:
        process_dir = Path(projects[name]['dir']).expanduser().resolve()
    elif args.dir:
        process_dir = Path(args.dir).expanduser().resolve()
    else:
        process_dir = Path(manifest['dir']).expanduser().resolve()

    tenant = manifest.get('tenant')
    if manifest.get('had_state_tree') and not tenant:
        dax_print('[!] {} — archive has a Claude state tree but no tenant recorded, '
                  'skipping'.format(name))
        return

    if not registered:
        if process_dir.exists() and any(process_dir.iterdir()):
            dax_print('[!] {} — {} already exists and is not empty, skipping'.format(
                name, process_dir))
            return
        problem = _process_dir_conflict(config, process_dir)
        if problem:
            dax_print('[!] {} — {}, skipping'.format(name, problem))
            return

    dest_state = state_tree_path(tenant, name) if tenant else None
    heading = ('About to restore {} over its existing registration:'.format(name) if registered
               else 'About to restore {}:'.format(name))
    _print_restore_preview(heading, process_dir, tenant, manifest, dest_state, name)

    if registered:
        # --dry-run must never block on input — report what it would ask
        # rather than actually asking.
        if args.dry_run:
            dax_print('[-]   dry run — would prompt to confirm before restoring over the '
                      'existing registration')
            return
        if not _q_confirm('{} is already registered at {} — restore over it?'.format(
                name, process_dir), default=False):
            dax_print('[-]   skipped {}'.format(name))
            return
    elif args.dry_run:
        dax_print('[-]   dry run — nothing restored')
        return

    creds = _extract_process_archive(config, archive, manifest, name, process_dir, tenant)
    dax_print('[+] restored {} at {}'.format(name, process_dir))
    _print_restore_login_reminders(creds, tenant, name)


def _restore_moved(config, name, archive, manifest, args):
    """Restore an archived snapshot under a *new* name and directory,
    leaving whatever's currently registered under the original name
    (if anything) completely untouched — the interactive picker's "move
    restore" choice, for pulling an old snapshot back as a second, separate
    env alongside a still-live original, or relocating a process that no
    longer belongs at its original path.

    Both the new name and the new directory are re-prompted on a bad
    answer rather than aborting the whole restore session over one typo —
    `_process_dir_conflict` (the non-fatal core `_check_process_dir` wraps)
    exists specifically to make that possible.
    """
    from dax_creds.init import _prompt, _q_confirm
    from dax_creds.config import state_tree_path

    print()
    print('Move restore {!r}: choose a new name and location so the original '
          'stays untouched.'.format(name))

    new_name = None
    while True:
        candidate = _prompt('New env name')
        if not candidate:
            dax_print('[-]   cancelled')
            return
        if candidate in (config.get('projects') or {}):
            dax_print('[!] {!r} is already registered — pick a different name'.format(candidate))
            continue
        new_name = candidate
        break

    new_dir = None
    while True:
        candidate_dir = _prompt('New directory')
        if not candidate_dir:
            dax_print('[-]   cancelled')
            return
        candidate_dir = Path(candidate_dir).expanduser().resolve()
        if candidate_dir.exists() and any(candidate_dir.iterdir()):
            dax_print('[!] {} already exists and is not empty — pick a different '
                      'location'.format(candidate_dir))
            continue
        problem = _process_dir_conflict(config, candidate_dir)
        if problem:
            dax_print('[!] {}'.format(problem))
            continue
        new_dir = candidate_dir
        break

    tenant = manifest.get('tenant')
    if manifest.get('had_state_tree'):
        entered = _prompt('Tenant/grouping label', default=tenant)
        tenant = entered or tenant
        if not tenant:
            dax_print('[!] this archive has a Claude state tree but no tenant — '
                      'cannot move restore without one')
            return
    dest_state = state_tree_path(tenant, new_name) if tenant else None
    if dest_state and dest_state.exists():
        dax_print('[!] {} already exists — pick a different name'.format(dest_state))
        return

    _print_restore_preview('About to move-restore {} as {}:'.format(name, new_name),
                           new_dir, tenant, manifest, dest_state, new_name)

    if args.dry_run:
        dax_print('[-]   dry run — nothing restored')
        return

    if not _q_confirm('Proceed?', default=True):
        dax_print('[-]   cancelled')
        return

    creds = _extract_process_archive(config, archive, manifest, new_name, new_dir, tenant)
    dax_print('[+] restored {} as {} at {}'.format(name, new_name, new_dir))
    _print_restore_login_reminders(creds, tenant, new_name)


_RESTORE_CANCEL = object()


def _run_process_restore_interactive(config, available, args):
    """`dax process restore` with no name/--all: presents available
    archives as a single-select list (never a checkbox — restore is rare
    and low-volume), and for whichever one is picked, asks whether to
    restore it in place or move it to a new name/location. Handles exactly
    one restore, then returns — run the command again for another.

    Deliberately one-shot, not a loop back to the top-level list. An
    earlier version looped so "several can be handled in one sitting" —
    found live, 2026-08-30, that chaining several interactive prompts
    together is exactly the shape that let a leftover keystroke (from an
    earlier prompt, most likely a habitual double Enter) bleed into the
    next one and silently resolve it to its default choice before the user
    ever saw it. `_flush_stdin` (`dax_creds/init.py`) now guards every
    individual prompt against that regardless, but removing the loop
    closes off the whole class of chained-prompt risk for this flow rather
    than only defending against it, and it's also just what was actually
    wanted: restore the one thing, return to the shell.

    Uses a sentinel object for "Cancel" rather than `value=None` —
    found live on the real host: `questionary.Choice(title, value=None)`
    does not actually store `None`. questionary's own default for an
    omitted `value` is `None`, so passing it explicitly is
    indistinguishable from not passing it at all, and questionary silently
    substitutes the choice's *title string* instead — which made "Cancel"
    fall through into starting a move-restore instead of doing nothing. A
    plain `object()` sentinel round-trips through `Choice` correctly
    (confirmed: only an explicit `None` gets replaced), and is compared
    with `is`, never `==`, so it can never collide with a real env name or
    mode string.
    """
    from dax_creds.init import _q_select
    import questionary

    name_width = max((len(n) for n in available), default=0)

    projects = config.get('projects') or {}
    choices = []
    for name, (_archive, date) in sorted(available.items()):
        note = '   [currently registered]' if name in projects else ''
        choices.append(questionary.Choice(
            '{}   (archived {}){}'.format(name.ljust(name_width), date, note),
            value=name))
    choices.append(questionary.Choice('Cancel', value=_RESTORE_CANCEL))

    try:
        picked = _q_select('Select a process to restore:', choices)
    except KeyboardInterrupt:
        return
    if picked is _RESTORE_CANCEL:
        return

    archive, _date = available[picked]
    manifest = _read_archive_manifest(archive)

    try:
        mode = _q_select(
            'How do you want to restore {!r}?'.format(picked),
            [
                questionary.Choice('Restore in place (original name and location)',
                                   value='in_place'),
                questionary.Choice('Move restore (new name and location on this machine)',
                                   value='move'),
                questionary.Choice('Cancel', value=_RESTORE_CANCEL),
            ])
    except KeyboardInterrupt:
        return
    if mode is _RESTORE_CANCEL:
        return
    elif mode == 'in_place':
        _restore_in_place(config, picked, archive, args)
    else:
        _restore_moved(config, picked, archive, manifest, args)


def _run_process_restore(config, args):
    """Restore one or more processes from `dax process archive`'s backups —
    the recovery half of the BCP/DR mechanism (see
    docs/design/2026-08-22-process-archive-restore.md).

    Explicit name(s)/--all skip straight to a non-interactive restore-in-
    place per name (the scripted/full-machine-rebuild path); omitting both
    drops into the interactive one-at-a-time picker, which additionally
    offers a "move restore" to a new name/location per pick.
    """
    if args.dir and (args.all or not args.name or len(args.name) != 1):
        dax_print('[!] --dir only makes sense with exactly one explicit name')
        sys.exit(1)

    backup_dir = _default_backup_dir(config)
    available = _available_archives(backup_dir)
    if not available:
        dax_print('[!] no archives found under {}'.format(backup_dir / 'daily'))
        return

    if args.all or args.name:
        names = sorted(available) if args.all else list(args.name)
        unknown = [n for n in names if n not in available]
        if unknown:
            dax_print('[!] no archive found for: {}'.format(', '.join(unknown)))
            sys.exit(1)

        if args.dry_run:
            # One combined table for the whole batch rather than N repeated
            # full-preview paragraphs — --dry-run has no interleaved
            # per-target prompt to interrupt it, so the whole batch can be
            # reported at once, same shape as destroy/archive --remove.
            rows = [_restore_preview_row(config, name, available[name][0], args)
                    for name in names]
            _print_table(('name', 'directory', 'tenant', 'image', 'creds', 'state tree',
                          'outcome'), rows)
            dax_print('[-]   dry run — nothing restored')
            return

        for name in names:
            _restore_in_place(config, name, available[name][0], args)
        return

    _run_process_restore_interactive(config, available, args)


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
    elif args.process_command == 'export':
        _run_process_export(config, args)
    elif args.process_command == 'import':
        _run_process_import(config, args)
    elif args.process_command == 'archive':
        _run_process_archive(config, args)
    elif args.process_command == 'restore':
        _run_process_restore(config, args)


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
        backup_dir = _default_backup_dir(config)

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

    process_export_p = process_sub.add_parser(
        'export',
        help='Package a registered process/env (project dir + Claude state tree) '
             'into a tarball for moving to another machine')
    process_export_p.add_argument('name', help='Env name to export')
    process_export_p.add_argument(
        '--out', help='Output path (default: <name>-dax-export.tar.gz in the cwd)')
    process_export_p.add_argument(
        '--include-credential', action='store_true',
        help='Include the live Claude OAuth credential from the state tree. '
             'Off by default — Keychain grants are per machine by design; only pass '
             'this if you are retiring the source machine, not adding a second one')
    process_export_p.add_argument(
        '--dry-run', action='store_true',
        help='Print the summary of what would be exported, then exit without writing anything')

    process_import_p = process_sub.add_parser(
        'import',
        help='Unpack a `dax process export` archive and register it as a new env here')
    process_import_p.add_argument('archive', help='Path to the exported .tar.gz')
    process_import_p.add_argument('--dir', required=True, help='Destination directory on this host')
    process_import_p.add_argument('--name', help='Env name to register (default: the exported name)')
    process_import_p.add_argument(
        '--tenant', help='Tenant/grouping label (default: the exported tenant, if any)')
    process_import_p.add_argument(
        '--dry-run', action='store_true',
        help='Print the summary of what would be imported, then exit without changing anything')

    process_archive_p = process_sub.add_parser(
        'archive',
        help='Snapshot one or more processes into a permanent backup — the BCP/DR '
             'mechanism (see docs/design/2026-08-22-process-archive-restore.md)')
    process_archive_p.add_argument(
        'name', nargs='*',
        help='Env name(s) to archive (omit for an interactive picker over every registered env)')
    process_archive_p.add_argument(
        '--all', action='store_true', help='Archive every registered env, no picker')
    process_archive_p.add_argument(
        '--remove', action='store_true',
        help='Also remove each env after every archive in the batch succeeds '
             '(default: snapshot only, non-destructive)')
    process_archive_p.add_argument(
        '--include-credential', action='store_true',
        help='Include the live Claude OAuth credential in each state tree snapshot')
    process_archive_p.add_argument(
        '--dry-run', action='store_true',
        help='Print what would be archived (and removed, if --remove), then exit '
             'without writing anything')

    process_restore_p = process_sub.add_parser(
        'restore',
        help='Restore one or more archived processes — the recovery half of the BCP/DR '
             'mechanism (see docs/design/2026-08-22-process-archive-restore.md)')
    process_restore_p.add_argument(
        'name', nargs='*',
        help='Env name(s) to restore, non-interactively and in place (omit for an '
             'interactive picker, one at a time, with a move-restore option per pick)')
    process_restore_p.add_argument(
        '--all', action='store_true',
        help='Restore every process with an available archive, non-interactively and in place')
    process_restore_p.add_argument(
        '--dir',
        help='Relocate: restore to this path instead of the archived dir '
             '(only valid with exactly one explicit name)')
    process_restore_p.add_argument(
        '--dry-run', action='store_true',
        help='Print what would be restored, then exit without changing anything')

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
