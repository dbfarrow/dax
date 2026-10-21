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
import time
from pathlib import Path, PurePosixPath
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
# dotfiles, the webpreview server, and Claude (its per-env state tree and
# implied credential, decision 35) are not optional per-project choices.
# Baked in here rather than sourced from ~/.dax.yaml's top-level `features:`
# key (removed 2026-08) because that list was the actual cause of the
# additive-only, no-opt-out problem: an env could never decline something
# every other env also got by default. Per-env `features:` (on the project
# entry, a cwd-local .dax.yaml, or `-f`) remain purely opt-in, which is fine —
# nobody was ever trying to opt *out* of those.
_ALWAYS_ON_FEATURES = ('workdir', 'dotfiles', 'webpreview', 'claude')


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
        _exit_missing_config()

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
    defaults['features'] = _merge_features(list(_ALWAYS_ON_FEATURES), local_features)

    return defaults




def _merge_features(base, extra):
    """`base` plus each feature in `extra` not already present, in order.

    Every per-env feature source (an env's own `features:`, a cwd-local
    .dax.yaml, `-f`) goes through here, so a feature listed twice is applied
    once (a duplicate mount would fail the launch), and retired names are
    dropped: `claude`/`claude_tenant_state` (Claude is baseline, decisions
    35, 36) and `auggie` (removed, decision 39).
    """
    from dax_creds.config import RETIRED_FEATURES
    merged = list(base)
    for feature in extra or []:
        feature = feature.strip() if isinstance(feature, str) else feature
        if not feature or feature in merged:
            continue
        if feature in RETIRED_FEATURES:
            continue  # `claude` is already in base (baseline); the others are retired
        merged.append(feature)
    return merged


def _warn_retired_entries(found):
    """One line per env still listing a feature or credential dax no longer
    uses (decisions 36, 39): ignored, and reported until hand-removed."""
    for env_name, entries in found.items():
        dax_print('[!] ~/.dax.yaml: env {!r} still lists {} — no longer needed (Claude is '
                  'automatic, `mounts:` switches mounts on by itself, auggie was removed); '
                  'remove {}'.format(env_name, ', '.join(entries),
                                     'it' if len(entries) == 1 else 'them'))


def _exit_missing_config():
    from dax_creds.config import missing_config_message
    dax_print('[!] no ~/.dax.yaml found.')
    print(missing_config_message())
    sys.exit(1)


def _get_username():
    return os.environ.get('USER') or os.environ.get('LOGNAME')


def _host_timezone():
    """The host's current IANA timezone name (e.g. 'America/New_York'), or
    None if it can't be determined.

    Read from `/etc/localtime` — the standard macOS/Linux convention is a
    symlink to the real zoneinfo file, e.g.
    `/etc/localtime -> /usr/share/zoneinfo/America/New_York`. Reading the
    live symlink (not baking a zone into the image) is what makes this
    reflect the host's *current* setting at every launch — it tracks
    changes (travel, a manual timezone change) without needing a rebuild,
    the same reasoning that keeps credentials and other host state out of
    the image.

    Deliberately best-effort: a host that doesn't use this convention (not
    a symlink, path doesn't contain 'zoneinfo/') just means TZ isn't set in
    the container — not a reason to block the launch over.
    """
    try:
        target = os.readlink('/etc/localtime')
    except OSError:
        return None
    marker = 'zoneinfo/'
    idx = target.find(marker)
    if idx == -1:
        return None
    return target[idx + len(marker):]


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


def feature_claude(config):
    """Decision B: this env's state tree mounts at `~/.claude` itself.

    Always on (decision 35): one of `_ALWAYS_ON_FEATURES`. It was the opt-in
    `claude_tenant_state` until the env-lifecycle consolidation, which
    deleted the old `claude` feature (a wholesale mount of the host's
    `~/.claude`, shared by every env) and gave this one its name. A per-env
    `claude`/`claude_tenant_state` in `features:` is now redundant: filtered
    out, and reported until removed (decision 36).

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
    # The registry name, which the credential name, destroy, archive and
    # restore all use too. It equals the folder name for every env the
    # wizard or restore registers (decision 5); a hand-edited entry where
    # they differ would otherwise get two trees.
    project_name = config.get('env_name') or config['workdir_name']
    tenant = config.get('tenant')
    if not tenant:
        dax_print('[!] {} needs a tenant, and none is declared.'.format(project_name))
        dax_print('    The state tree lives at '
                  '~/.local/state/dax/tenants/<tenant>/{}/, so there is nowhere'.format(
                      project_name))
        dax_print('    to mount without one. Add one to this env\'s entry in ~/.dax.yaml:')
        dax_print('      tenant: <name>')
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
    # above), once: a file the tree already has is never touched (decision 13).
    sync_claude_shared_files(tenant, project_name)

    return opts


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

    Named `mounts` on the project entry, a list the same way `creds`/`features`
    are. A non-empty list switches the feature on by itself (decision 40);
    `mounts` in `features:` is a retired name.

    Each entry is `<host_path>`, `<host_path>:<container_name>`, or
    `<host_path>:<container_name>:ro` — the name defaults to
    `dir_basename(host_path)` when omitted, but an explicit one is what lets
    a host directory be mounted under a name other than its own basename,
    e.g. the host's own `~/.claude` mounted as `~/host-claude` to inspect its
    real content from inside a container without colliding with whatever
    the `claude` feature already mounts at `~/.claude` itself. A trailing
    `:ro` (the default container name still applies, e.g. `~/discernment::ro`)
    marks that one mount read-only — the same trailing-`:ro` Docker's own
    `--volume` syntax uses, and the same convention `feature_dotfiles`
    already applies to `dotfiles.ro`. Read-write unless present; nothing to
    opt into for the common case.
    """
    opts = []
    mounts = config.get('mounts') or []
    if not mounts:
        dax_print("[!] no mounts defined for this env")
        return opts
    container_home = _container_home(config)
    for entry in mounts:
        host_path, _sep, rest = entry.partition(':')
        container_name, _sep, mode = rest.partition(':')
        ro = mode == 'ro'
        host = os.path.expanduser(host_path)
        # Docker silently auto-creates a missing bind-mount source as an
        # empty directory rather than failing, so a mount that's supposed
        # to come from elsewhere (a network share, a mount from another
        # machine, ...) but isn't currently there would otherwise start the
        # container anyway with nothing behind it — discovered only later,
        # from inside, if at all. Checked host-side, before Docker ever
        # runs, so a missing mount refuses the launch instead.
        if not os.path.isdir(host):
            dax_print("[!] mount source not found: {} — is it mounted? Refusing to "
                      "start without it.".format(host))
            sys.exit(1)
        container = os.path.join(container_home, container_name or dir_basename(host))
        opts.append('--volume={}:{}{}'.format(host, container, ':ro' if ro else ''))
    return opts


def feature_substrate(config):
    """Mounts the virgil-style substrate repo (rw) and exposes it as
    $SUBSTRATE_ROOT — the container path both the substrate's own hooks and
    the `claude` wrapper's gate/settings-delivery logic read (see
    docs/design/VALIDATION.md's dax contract).

    A single path, unlike the generic `mounts` list feature_mounts handles:
    dax needs to know specifically which mount holds `shared/wire.sh` and
    `shared/new-process.sh`, not just that something extra is mounted. It's
    one top-level `substrate:` setting shared by every process (decision 41);
    an env opts in with `features: [substrate]`.

    `wire.sh --quiet` itself runs once per container boot (dax-entrypoint.sh,
    baked into the image), not here and not on every `claude` launch — see
    the design doc's Part 4 for why that distinction is load-bearing.
    """
    substrate = config.get('substrate')
    if not substrate:
        # One global path shared by every process (decision 41). Refused
        # rather than launched without it: a substrate process can't work
        # without its substrate, and the claude wrapper's gate would refuse
        # inside the container anyway, later and less clearly.
        dax_print("[!] this env uses the substrate feature, but no substrate is set.")
        old = config.get('_per_env_substrate')
        if old:
            dax_print("    Its entry still has a per-env `substrate: {}`, which is no longer "
                      "read. Move that path to a top-level `substrate:` in ~/.dax.yaml "
                      "(`dax config`); every process shares it.".format(old))
        else:
            dax_print("    Add a top-level `substrate: <path>` to ~/.dax.yaml (`dax config`); "
                      "every process shares it.")
        sys.exit(1)
    host = os.path.expanduser(substrate)
    # Same reasoning as feature_mounts' own check: Docker auto-creates a
    # missing bind-mount source as an empty directory rather than failing,
    # so a substrate mounted from another machine (over a network share, a
    # mount that isn't always up, ...) would otherwise start the container
    # fine with an empty $SUBSTRATE_ROOT — `entrypoint.sh`'s own
    # `wire.sh`-missing check only catches this *after* the container has
    # already booted. Checking for the exact same marker
    # (`shared/wire.sh`, executable) host-side, before Docker ever runs,
    # refuses the launch instead of starting into a broken substrate.
    wire_sh = Path(host) / 'shared' / 'wire.sh'
    if not (wire_sh.is_file() and os.access(str(wire_sh), os.X_OK)):
        dax_print("[!] substrate not available at {} — shared/wire.sh is missing "
                  "or not executable. Is the mount up? Refusing to start "
                  "without it.".format(host))
        sys.exit(1)
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
        note = ('   (always on)' if name in _ALWAYS_ON_FEATURES else
                '   (on when an env has a `mounts:` list)' if name == 'mounts' else
                '   (opt in per env; path is the top-level `substrate:`)' if name == 'substrate'
                else '')
        dax_print("\t{}{}".format(name, note))


def _get_version():
    """The `VERSION` file beside this checkout; in the image, where dax is a
    plain pip install with no `VERSION` beside it, the installed package's
    own version, which pip took from that same file at build time."""
    try:
        with open(Path(__file__).parent / 'VERSION', 'r') as f:
            return f.readline().split()[0]
    except (OSError, IndexError):
        pass
    try:
        from importlib.metadata import version
        return version('dax')
    except Exception:
        return 'unknown'


# Image label `dax build` stamps with the version it built from, so `dax run`
# can tell when the image is older than the host's dax (decision 42). The
# in-image copy of dax/dax_creds is frozen at build time, so an un-rebuilt
# image silently runs old wrapper and credential code.
_VERSION_LABEL = 'dax.version'


def _image_version(image):
    """The `dax.version` label on `image`, '' when the image has none, or
    None when it can't be read (no docker, no such image)."""
    try:
        result = subprocess.run(
            ['docker', 'image', 'inspect', '--format',
             '{{ index .Config.Labels "%s" }}' % _VERSION_LABEL, image],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return '' if value in ('', '<no value>') else value


def _warn_if_image_is_stale(image):
    """Warn, never refuse, when the image wasn't built from this version.
    An image with no label at all is only reported if it is one of dax's own
    (`dax:*`); a custom image is the user's business."""
    built = _image_version(image)
    host = _get_version()
    if built is None or host == 'unknown':
        return
    if built == '':
        if image.split(':', 1)[0] == 'dax':
            dax_print("[!] image {} predates dax versioning; this is dax {}. "
                      "Run `dax build` to update it.".format(image, host))
        return
    if built != host:
        dax_print("[!] image {} was built by dax {}; this is dax {}. "
                  "Run `dax build` to update it.".format(image, built, host))


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
    build_cmd += ['--label', '{}={}'.format(_VERSION_LABEL, version),
                  '-t', image_tag, '.']
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


def _sync_and_clear_claude_credential(config, project_creds):
    """Write an env's live `.credentials.json` back to
    Keychain when the container stops, then delete it from disk — restoring
    the property the Keychain design was originally built around
    (credentials don't sit on disk when nothing is running), which the
    persistent per-env state tree quietly broke: the tree survives a
    container stop by design (that's the whole point of the per-env
    tree), and nothing ever copied its refreshed token back
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
    cred_name = next((n for n, d in project_creds.items()
                       if d.get('provider') == 'claude'), None)
    if not cred_name:
        return

    from dax_creds.config import state_tree_path
    tenant = config.get('tenant')
    if not tenant:
        return
    cred_file = state_tree_path(tenant, config.get('env_name') or config['workdir_name']) / '.credentials.json'
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
        # The same one-grant-per-name rule login enforces (decision C6). A
        # tree can hold a grant dax never tracked, e.g. an env that had a
        # state tree before it had a Claude credential, and storing that
        # under this name would share it with whichever name already holds
        # it. Cleared rather than kept: the next launch logs this env in
        # with a grant of its own.
        from dax_creds.config import credential_names_for_provider
        clash = provider.grant_collision(
            cred_name, blob, credential_names_for_provider(config, 'claude'))
        if clash:
            cred_file.unlink()
            dax_print('[!] {}: the credential in its state tree is the same grant as {!r} — '
                      'not stored, and cleared; the next launch logs it in fresh'.format(
                          cred_name, clash))
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
    # Unconditional, like workdir/dotfiles/webpreview — nobody wants their
    # container showing a different timezone than their own by default.
    # Read fresh at every launch, not baked into the image, so it tracks
    # the host's actual current setting (travel, a manual change) without
    # needing a rebuild. Falls back to `defaults.timezone` in ~/.dax.yaml
    # (the existing fallback-value block `defaults.image` already uses)
    # when the host's own setting can't be determined, rather than just
    # silently leaving the container with none.
    tz = _host_timezone() or config.get('defaults', {}).get('timezone')
    if tz:
        cmd += ['-e', 'TZ={}'.format(tz)]

    daemon_proc = None
    ssh_bridge_proc = None
    project_creds = {}
    try:
        from dax_creds.config import (
            load_dax_config, find_named_project_by_dir, find_enclosing_project,
            get_project_credentials, unknown_env_keys, describe_unknown_env_keys,
            unknown_tenant_entries,
            retired_entries,
        )
        from dax_creds.providers.ssh import SshProvider, start_ephemeral_agent, stop_ephemeral_agent
        dax_config = load_dax_config()
        # Quiet unless something's wrong, like the backup check above — and
        # across every registered env, not just this one, so a typo in an env
        # nobody has launched lately still surfaces on the next launch of any.
        for _env, _keys in unknown_env_keys(dax_config).items():
            dax_print('[!] ~/.dax.yaml: env {!r} has {}'.format(
                _env, describe_unknown_env_keys(_keys)))
        for _problem in unknown_tenant_entries(dax_config):
            dax_print('[!] ~/.dax.yaml: {}'.format(_problem))
        _warn_retired_entries(retired_entries(dax_config))
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
            dax_print("[+] {} isn't a registered env yet — setting it up".format(Path.cwd()))
            bootstrapped = _bootstrap_env(dax_config, Path.cwd().resolve())
            if bootstrapped is None:
                # Declined or cancelled: nothing registered, so no container
                # either. Without this, the outer `except (FileNotFoundError,
                # KeyError)` would swallow the failed lookup and launch anyway.
                sys.exit(0)
            project_name, project = bootstrapped
        try:
            project_creds = get_project_credentials(dax_config, project, project_name)
        except ValueError as e:
            # No tenant to derive the implied Claude credential from. Refusing
            # beats silently falling back to a shared credential, which is the
            # exact isolation failure this convention exists to prevent.
            dax_print(f'[!] {e}')
            sys.exit(1)
        except KeyError as e:
            # An explicitly named credential with no definition. Left to the
            # outer `except KeyError` this used to be swallowed, launching with
            # no credential daemon and, once Claude became baseline, ending in
            # a misleading "needs a tenant" from feature_claude.
            dax_print(f"[!] env '{project_name}' lists credential {e.args[0]!r}, which "
                      f'is not defined in ~/.dax.yaml (see `dax creds list`)')
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
        # Decision A: a declared grouping label on the env's entry. With the
        # registry name, this is what feature_claude builds the state-tree
        # path from — no `.dax-tenant` file, no cwd resolution.
        config['tenant'] = project.get('tenant')
        config['env_name'] = project_name

        # The env's own `features:` (per-env opt-ins on top of the always-on
        # baseline), merged so nothing applies twice and the retired Claude
        # names are dropped.
        config['features'] = _merge_features(config['features'], project.get('features'))

        # feature_mounts reads this the same way feature_claude
        # reads config['tenant'] above — project entries aren't otherwise
        # promoted into the flat config feature functions see.
        config['mounts'] = project.get('mounts') or []
        # A non-empty `mounts:` switches the feature on by itself (decision
        # 40); `mounts` listed in `features:` is filtered as retired above.
        if config['mounts'] and 'mounts' not in config['features']:
            config['features'].append('mounts')
        # No per-env `substrate:` any more (decision 41): feature_substrate
        # reads the one top-level path. Taken from the registry, not just from
        # load_config's earlier read, because the bootstrap wizard may have
        # just saved it (a first process env's first launch). An old per-env
        # path is passed along only so a refusal can say what to move.
        config['substrate'] = dax_config.get('substrate') or config.get('substrate')
        config['_per_env_substrate'] = project.get('substrate')

        # Two competing conventions have accumulated for "the default image":
        # a bare top-level `image:` (what load_config() above reads, and what
        # .dax.yaml.example documents) and `defaults: {image: ...}` (what
        # the bootstrap wizard and `dax creds login` use).
        # A project's own `image:` was never promoted at all - silently
        # ignored, since this promotion never existed for it the way it does
        # for tenant/mounts/substrate above. Found live: a real ~/.dax.yaml
        # written in the `defaults:` shape, with no bare top-level `image:`
        # and no promotion for the project's own override, crashed `dax run`
        # outright with a bare KeyError. Project-specific wins, then whichever
        # of the two global-default spellings is actually set, then a
        # hardcoded fallback matching the bootstrap wizard's own — never a crash on a
        # missing key again.
        image = (project.get('image') or config.get('image') or
                dax_config.get('defaults', {}).get('image') or 'dax-base')
        # `dax-base` is what the bootstrap wizard writes into *every*
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
        _warn_if_image_is_stale(image)

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
        missing = set()
        # No logins from a preview (`-t` must not open a browser or mint a
        # grant), and none without keyring: the login flow needs it and
        # exits without it, and every env has a Claude credential now, so
        # asking would turn every keyring-less launch into a dead end.
        can_log_in = not args.test_only and _keyring_importable()
        if not can_log_in:
            non_ssh_creds = {}
        if non_ssh_creds:
            from dax_creds.init import ensure_project_credentials
            missing = ensure_project_credentials(dax_config, non_ssh_creds)
            for cred_name in missing:
                dax_print(f'[!] {cred_name} not in Keychain.')
                answer = input(f'    Run `dax creds login {cred_name}` now? [Y/n] ').strip().lower()
                if answer in ('', 'y', 'yes'):
                    cmd_creds_login(cred_name, dax_config)

        # Proactive relogin, for every env since Claude is baseline. A credential already
        # in Keychain can still be a dead link in the refresh-token rotation
        # chain — the env's last session died before a teardown ever synced a
        # fresher token back — and there is no local signal on the blob
        # (expiresAt is the short-lived access token; refreshTokenExpiresAt is
        # documented as unreliable) that can tell a live grant from a dead one
        # short of actually trying it. Rather than guess, just run the login
        # flow before every launch — gated on `relogin_due`'s cooldown so
        # restarting the same env several times in a day (picking up a `dax
        # build`) doesn't pop a browser window on every single restart.
        # `missing` credentials are skipped: the loop above either just
        # logged them in or the user declined to, either way nothing further
        # to do here.
        from dax_creds.config import relogin_due
        for cred_name, cred_def in non_ssh_creds.items():
            if cred_def.get('provider') != 'claude' or cred_name in missing:
                continue
            if relogin_due(cred_name, dax_config):
                dax_print(f'[+] {cred_name}: stale — running the Claude '
                          'login flow before launch')
                cmd_creds_login(cred_name, dax_config)
            else:
                dax_print(f'[-] {cred_name}: logged in recently — skipping relogin')
    except (FileNotFoundError, KeyError):
        pass

    features = list(config['features'])
    if args.features:
        features = _merge_features(features, args.features.split(','))

    if args.ports:
        config.setdefault('ports', []).extend(args.ports)
        if 'ports' not in features:
            features.append('ports')

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
            _sync_and_clear_claude_credential(config, project_creds)


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

    # Claude logs in into a fresh, private folder mounted at the container's
    # ~/.claude, never the host's real ~/.claude (decision 32): the login
    # used to land the token there and leave it behind, so the host always
    # held a plaintext copy of the newest env grant. Made before the daemon
    # starts, so a failure here can't leave a daemon running.
    login_dir = _claude_login_dir() if provider == 'claude' else None

    # Start daemon in TCP mode (avoids Docker Desktop Unix socket permission issues)
    creds_for_daemon = {cred_name: cred_def}
    daemon_proc = _start_login_daemon(creds_for_daemon, port)
    if not _wait_for_tcp('127.0.0.1', port):
        dax_print('[!] credential daemon did not start')
        daemon_proc.terminate()
        if login_dir is not None:
            shutil.rmtree(login_dir, ignore_errors=True)
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
        if login_dir is not None and mount == '~/.claude':
            host_path = str(login_dir)
        else:
            Path(host_path).mkdir(parents=True, exist_ok=True)
        cmd += ['-v', f'{host_path}:{container_path}']

    cmd += [image, '/bin/sh', '-c', provider_cfg['auth_command']]

    # What the provider's on-disk location holds *before* the flow runs. The
    # import below reads that same location, and cannot otherwise tell a token
    # the login just wrote from one that was already sitting there — so an
    # abandoned login silently stored the pre-existing shared credential under
    # the new name, producing two Keychain entries backed by one OAuth grant.
    # That is the exact sharing the per-env naming exists to prevent, and it
    # presented as success. Observed 2026-07-30. For Claude the folder starts
    # empty, so "nothing written" is simply "no file".
    before = None if login_dir is not None else _disk_token_for(cred_def, provider)

    dax_print(f'[+] dax creds login: starting auth flow for {cred_name} ({provider})')
    try:
        try:
            subprocess.run(cmd)
        finally:
            daemon_proc.terminate()
            dax_print(f'[-] login daemon log: {daemon_proc._dax_log_path}')

        # Post-login: import token to Keychain
        _post_login_import(
            cred_name, cred_def, config, provider, before=before,
            credentials_file=(login_dir / '.credentials.json') if login_dir is not None else None)
    finally:
        if login_dir is not None:
            shutil.rmtree(login_dir, ignore_errors=True)

    from dax_creds.config import record_login
    record_login(cred_name)


def _claude_login_dir():
    """A fresh 0700 folder for one Claude login to write into, under
    ~/.local/state/dax/login/ (inside /Users on macOS, which Docker Desktop
    always shares, unlike Python's default /var/folders temp location).
    Folders left by a run killed before cleanup are swept first, but only
    ones over an hour old, so two logins at once can't delete each other's."""
    base = Path.home() / '.local' / 'state' / 'dax' / 'login'
    base.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 3600
    for stale in base.iterdir():
        try:
            if stale.is_dir() and stale.stat().st_mtime < cutoff:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix='claude-', dir=str(base)))


def _disk_token_for(cred_def, provider):
    """Whatever the provider would import from disk right now, or None."""
    try:
        if provider == 'github':
            from dax_creds.providers.github import GitHubProvider
            return GitHubProvider().import_from_disk(cred_def)
        if provider == 'claude':
            from dax_creds.providers.claude import ClaudeProvider
            return ClaudeProvider().import_from_disk(cred_def)
    except Exception:
        pass
    return None


def _report_grant_collision(cred_name, other_name):
    from dax_creds.providers.claude import grant_collision_message

    lines = grant_collision_message(cred_name, other_name)
    dax_print(f'[!] {cred_name}: {lines[0]}')
    for line in lines[1:]:
        dax_print(f'    {line}')


def _post_login_import(cred_name, cred_def, config, provider, before=None,
                       credentials_file=None):
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
        token = provider_obj.import_from_disk(cred_def, credentials_file=credentials_file)
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


def cmd_creds(args):
    from dax_creds.config import load_dax_config
    from dax_creds.init import run_creds_add, run_creds_list, run_creds_remove, run_creds_update
    try:
        config = load_dax_config()
    except FileNotFoundError:
        _exit_missing_config()
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
    from dax_creds.init import run_env_list, run_env_show

    try:
        config = load_dax_config()
    except FileNotFoundError:
        config = None

    # --explain works with no ~/.dax.yaml at all, so it's routed before the
    # missing-config exit below.
    if getattr(args, 'explain', False):
        explain_fn = {
            'archive': _env_archive_explain_text,
            'restore': _env_restore_explain_text,
        }.get(args.env_command)
        if explain_fn:
            print(explain_fn(config))
            return

    if config is None:
        _exit_missing_config()

    # The env name is resolved per subcommand, never up front: `_resolve_env_name`
    # exits when cwd isn't a registered env, which is right for single-env
    # inspection but wrong for `destroy`, whose no-argument form is a picker
    # over every env and is normally run from somewhere that isn't one.
    try:
        if args.env_command == 'list':
            run_env_list(config)
        elif args.env_command == 'show':
            run_env_show(config, _resolve_env_name(config, args.name))
        elif args.env_command == 'destroy':
            _run_env_destroy(config, args)
        elif args.env_command == 'archive':
            _run_env_archive(config, args)
        elif args.env_command == 'restore':
            _run_env_restore(config, args)
    except (KeyError, ValueError) as e:
        dax_print('[!] {}'.format(e.args[0] if e.args else e))
        sys.exit(1)


def cmd_features(args):
    _print_features()


# Installed by the dax image (Dockerfile.tmpl) and nowhere else, so its
# presence means "running inside a dax container".
_DAX_CONTAINER_MARKER = '/usr/local/bin/dax-entrypoint.sh'


def _in_dax_container():
    return os.path.exists(_DAX_CONTAINER_MARKER)


def _editor_command():
    """$VISUAL, then $EDITOR, then vi, split so `EDITOR="code -w"` works."""
    return shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')


def _config_shape_problem(config):
    """Why a config that parsed still can't be used, or None. Every dax
    command expects a mapping with a `projects:` mapping of mappings; an
    emptied file, a list, or an env written as a plain value would otherwise
    crash them all with a traceback."""
    if not isinstance(config, dict):
        return 'the file is empty or not a mapping of settings'
    projects = config.get('projects')
    if projects is not None and not isinstance(projects, dict):
        return '`projects:` must be a mapping of env names to settings'
    for name, entry in (projects or {}).items():
        if not isinstance(entry, dict):
            return 'env {!r} must be a mapping of settings, not {!r}'.format(name, entry)
    tenants = config.get('tenants')
    if tenants is not None and not isinstance(tenants, dict):
        return '`tenants:` must be a mapping of tenant names to settings'
    return None


def cmd_config(args):
    """`dax config`: edit ~/.dax.yaml in your editor, then check it
    (decision 38). Since `dax env set` was dropped (decision 14), hand-editing
    this file is the only way to change an env, and a broken file breaks
    every dax command until it's fixed, so the edit is bracketed:

    - the version from before an edit that changes the file is kept as
      ~/.dax.yaml.prev, so a bad edit is one `cp` from undone (written only
      on a change, so opening the file just to look never costs the undo);
    - after saving, the file is parsed the way every other command reads it
      (ruamel round-trip, so duplicate keys are caught too) and its shape is
      checked. On a problem the editor is offered again. Then the same
      warnings `dax run` prints: unknown keys and old Claude entries.
      Reported, never auto-fixed.

    Refuses inside a dax container: there ~/.dax.yaml is a single-file bind
    mount, and an editor that saves by write-then-rename silently detaches
    the container's copy from the host file, so a successful-looking edit
    may never reach the host. A missing file is decision 37's message; dax
    never creates one.
    """
    from dax_creds.config import (
        load_dax_config, unknown_env_keys, describe_unknown_env_keys,
        unknown_tenant_entries,
        retired_entries,
    )
    from dax_creds.init import _q_confirm

    if _in_dax_container():
        dax_print('[!] dax config only runs on the host. Inside a dax container ~/.dax.yaml '
                  'is a single-file bind mount, and an editor that saves by write-then-rename '
                  "silently detaches the container's copy, so the edit might never reach the host.")
        dax_print('    Run `dax config` in a host terminal.')
        sys.exit(1)

    path = Path.home() / '.dax.yaml'
    if not path.is_file():
        _exit_missing_config()

    prev = path.with_name('.dax.yaml.prev')
    before = path.read_bytes()

    def _keep_previous_if_changed():
        if path.read_bytes() != before:
            prev.write_bytes(before)
            return True
        return False

    editor = _editor_command()
    while True:
        try:
            result = subprocess.run(editor + [str(path)])
        except FileNotFoundError:
            _keep_previous_if_changed()
            dax_print('[!] editor {!r} not found. Set $VISUAL or $EDITOR to one that is '
                      'installed.'.format(editor[0]))
            sys.exit(1)
        if result.returncode != 0:
            dax_print('[!] the editor exited with status {}'.format(result.returncode))
        try:
            config = load_dax_config()
            problem = _config_shape_problem(config)
        except Exception as e:
            problem = 'it no longer parses:\n    ' + str(e).replace('\n', '\n    ')
        if problem is None:
            break
        dax_print('[!] ~/.dax.yaml has a problem: {}'.format(problem))
        try:
            again = _q_confirm('Reopen the editor to fix it?', default=True)
        except KeyboardInterrupt:
            again = False
        if not again:
            if _keep_previous_if_changed():
                dax_print('[!] left as is; the version from before this edit is at {} '
                          '(cp {} {} to undo)'.format(prev, prev, path))
            else:
                dax_print('[!] left as is (it was already like this before this edit)')
            sys.exit(1)

    if not _keep_previous_if_changed():
        dax_print('[-] no changes')
        return
    dax_print('[+] ~/.dax.yaml saved (previous version: {})'.format(prev))
    for env_name, keys in unknown_env_keys(config).items():
        dax_print('[!] env {!r} has {}'.format(env_name, describe_unknown_env_keys(keys)))
    for problem in unknown_tenant_entries(config):
        dax_print('[!] {}'.format(problem))
    _warn_retired_entries(retired_entries(config))


def _known_tenant_names():
    """Every tenant any registered env declares in its `tenant:` field: the
    pick-list `_prompt_tenant` offers, so one tenant isn't split into two by
    a typo ('Ysecurity' vs 'ysecurity')."""
    from dax_creds.config import load_dax_config

    try:
        dax_config = load_dax_config()
    except FileNotFoundError:
        return set()
    return {p.get('tenant') for p in (dax_config.get('projects') or {}).values()
            if (p or {}).get('tenant')}


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
    # A blank answer re-asks rather than cancelling (decision 8: a bad
    # *prompted* answer gets another try); only an explicit cancel (None,
    # i.e. Ctrl-C) stops.
    while True:
        result = _text("Tenant name for {}:".format(subdir), default or '')
        if result is None:
            raise KeyboardInterrupt
        if result.strip():
            return result.strip()
        dax_print('[!] a tenant is required')


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


def _bootstrap_dir_conflict(config, process_dir):
    """Returns an error message if process_dir is a structurally bad choice
    for a new env directory, or None if it's fine. Pure, non-fatal core of
    `_check_bootstrap_dir`, also used directly by `dax env restore`, which
    words its own exit around the message.

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


def _check_bootstrap_dir(config, process_dir):
    """Refuse structurally bad choices for a new process directory, before
    anything is created or registered. See `_bootstrap_dir_conflict` for what
    counts as bad — this is just the fatal (sys.exit) wrapper around it."""
    problem = _bootstrap_dir_conflict(config, process_dir)
    if problem:
        dax_print('[!] {}'.format(problem))
        sys.exit(1)


def _ask_tenant_browser(config, tenant):
    """For a brand-new tenant, which browser its Claude logins open
    (decision 43). Returns the `tenants.<tenant>.claude` block to save, or
    None for "same as default" (nothing written). Skipped when there's no
    browser to choose between."""
    from dax_creds.chrome import browser_label, enumerate_browsers
    from dax_creds.init import _q_select
    import questionary

    options = [b for b in enumerate_browsers() if b['browser'] != 'default']
    if not options:
        return None
    default = (config.get('credential_defaults') or {}).get('claude') or {}
    default_label = browser_label(default.get('browser', 'default'), default.get('chrome_profile'))
    print()
    print('{!r} is a new tenant. Its Claude logins can open their own browser profile.'.format(tenant))
    # Not value=None: questionary turns a None value into the title.
    picked = _q_select('Browser for {} Claude logins:'.format(tenant), [
        questionary.Choice('Same as default ({})'.format(default_label), value='same')] + [
        questionary.Choice(b['label'], value=b) for b in options])
    if picked == 'same':
        return None
    block = {'browser': picked['browser']}
    if picked['chrome_profile']:
        block['chrome_profile'] = picked['chrome_profile']
    if (block['browser'], block.get('chrome_profile')) == (
            default.get('browser', 'default'), default.get('chrome_profile')):
        return None  # the default, picked by name
    return block


def _tenant_browser_summary(config, tenant, tenant_browser):
    """The summary row for which browser the env's Claude login opens."""
    from dax_creds.chrome import browser_label
    from dax_creds.config import credential_settings_source, synthesized_credential

    if tenant_browser:
        return '{}   (will be saved for tenant {})'.format(
            browser_label(tenant_browser['browser'], tenant_browser.get('chrome_profile')), tenant)
    cred = synthesized_credential(config, 'claude', tenant)
    label = browser_label(cred.get('browser', 'default'), cred.get('chrome_profile'))
    if credential_settings_source(config, tenant, 'claude') == 'tenant':
        return '{}   (tenant {})'.format(label, tenant)
    return label


def _bootstrap_env(config, env_dir):
    """Register `env_dir` (cwd) as a new env: the wizard `dax run` starts
    when cwd isn't registered yet (decision 4: no `dax init`, no
    `dax process new`). Returns (name, entry) once it's saved, or None when
    the user declines or cancels; `cmd_run` then stops, so no container
    launches.

    The env is named after the folder (decision 5); nothing is passed as a
    flag (decision 6). Fixed inputs (cwd itself and the name derived from it)
    are fatal when wrong (decisions 8, 25); prompted answers re-ask.

    Flow: guardrails → image → tenant (always required) → leftover-state
    check → existing non-Claude credentials (no define-new loop, no Claude
    question: Claude is set up for every env) → coding project or
    substrate-backed process → for a process, substrate path, title, type,
    git → summary → confirm → (process) new-process.sh → one save.
    """
    from dax_creds.config import state_tree_path
    from dax_creds.init import _q_checkbox, _q_confirm, _q_path, _q_select, _q_text, save_config
    import questionary

    name = env_dir.name
    projects = config.get('projects') or {}

    problem = _bootstrap_dir_conflict(config, env_dir)
    if problem:
        dax_print('[!] {}'.format(problem))
        dax_print('    cd somewhere else and run `dax run` again.')
        sys.exit(1)
    if name in projects:
        dax_print('[!] an env named {!r} is already registered (at {}).'.format(
            name, projects[name].get('dir', '?')))
        dax_print('    Envs are named after their folder, so rename this folder, '
                  'or cd somewhere else and run `dax run` again.')
        sys.exit(1)

    try:
        print()
        print('This env will be named {!r}, after its folder. To name it something else,'.format(name))
        print('Ctrl-C now, rename the folder, and run `dax run` again.')
        print()
        image = _q_text('Image:', default=config.get('defaults', {}).get('image', 'dax-base'))

        tenant = _prompt_tenant(env_dir, _known_tenant_names())
        used_tenants = {(p or {}).get('tenant') for p in projects.values()}
        tenant_browser = None
        known_blocks = config.get('tenants') if isinstance(config.get('tenants'), dict) else {}
        if tenant not in used_tenants and tenant not in known_blocks:
            tenant_browser = _ask_tenant_browser(config, tenant)
        state_dir = state_tree_path(tenant, name)
        if state_dir.exists():
            dax_print('[!] {} already exists — leftover Claude state with no env '
                      'registered.'.format(state_dir))
            dax_print('    Remove it, `dax env restore` it if it is a real env, or use a '
                      'folder with a different name.')
            sys.exit(1)

        credentials = config.get('credentials') or {}
        other_creds = sorted(n for n, d in credentials.items()
                             if (d or {}).get('provider') != 'claude')
        if other_creds:
            selected = _q_checkbox(
                'Other credentials for this env (Claude is set up automatically):',
                [questionary.Choice(n) for n in other_creds])
        else:
            selected = []

        print()
        kind = _q_select('What kind of env is this?', [
            questionary.Choice('A coding project', value='coding'),
            questionary.Choice('A substrate-backed process', value='process'),
        ])

        process = None
        save_substrate = False
        if kind == 'process':
            # One substrate shared by every process (decision 41): the
            # top-level `substrate:`, asked for and saved only if it isn't
            # set yet. A set-but-broken one is fixed input, so it's fatal.
            global_substrate = config.get('substrate')
            while True:
                substrate = global_substrate or _q_path(
                    'Substrate path (saved as the one substrate every process shares):')
                if not substrate:
                    dax_print('[!] a substrate path is required')
                    continue
                substrate_root = Path(substrate).expanduser().resolve()
                script = substrate_root / 'shared' / 'new-process.sh'
                try:
                    types = _read_process_types(substrate_root)
                    problem = (None if script.is_file() else
                               'no shared/new-process.sh in {}'.format(substrate_root))
                    if problem is None and not types:
                        problem = '{} declares no process types'.format(substrate_root)
                except ValueError as e:
                    problem = str(e)
                if problem is None:
                    break
                dax_print('[!] {}'.format(problem))
                if global_substrate:
                    dax_print('    That is your global substrate; fix the top-level '
                              '`substrate:` in ~/.dax.yaml (`dax config`).')
                    sys.exit(1)
            save_substrate = not global_substrate
            while True:
                title = _q_text('Process title (the heading for its context.md, inbox.md and CLAUDE.md):')
                if title:
                    break
                dax_print('[!] a process title is required')
            process_type, skill = _q_select('Process type (picks the substrate skill Claude works it with):', [
                questionary.Choice('{} — {}'.format(t, desc), value=(t, sk))
                for t, sk, desc in types])
            print()
            print('Keep this process under version control?')
            print('  git history cannot be selectively scrubbed later — a process that may')
            print('  need to be destroyed is cleaner without it.')
            git = _q_confirm('git?', default=False)
            process = {'substrate': substrate_root, 'title': title, 'type': process_type,
                       'skill': skill, 'git': git, 'script': script}

        # Claude is baseline with an implied credential (decision 35), so
        # neither appears on the entry.
        creds = list(selected)
        features = ['substrate'] if process else []

        rows = [
            ('env', name),
            ('directory', str(env_dir)),
            ('image', image),
            ('tenant', tenant),
            ('credentials', ', '.join(selected) or '(none besides Claude)'),
            ('claude', 'claude-{}-{}, state tree {}'.format(tenant, name, state_dir)),
            ('claude login', _tenant_browser_summary(config, tenant, tenant_browser)),
            ('kind', 'substrate-backed process' if process else 'coding project'),
        ]
        if process:
            rows += [
                ('substrate', str(process['substrate']) + (
                    '   (will be saved as the global substrate)' if save_substrate
                    else '   (global)')),
                ('title', process['title']),
                ('type', '{} (skill: {})'.format(process['type'], process['skill'])),
                ('git', 'yes' if process['git'] else 'no'),
            ]
        _print_table(('field', 'value'), rows)

        if not _q_confirm('Create {} and launch it?'.format(name), default=True):
            dax_print('[-]   cancelled — nothing created or registered')
            return None
    except KeyboardInterrupt:
        print()
        dax_print('[-]   cancelled — nothing created or registered')
        return None

    if process:
        before = set(env_dir.iterdir())
        try:
            returncode = subprocess.run([
                str(process['script']),
                '--dir', str(env_dir),
                '--title', process['title'],
                '--type', process['type'],
                '--git' if process['git'] else '--no-git',
            ]).returncode
        except KeyboardInterrupt:
            returncode = 'interrupted'
        if returncode != 0:
            # Never deleted automatically: report exactly what the script
            # left, so a retry (which new-process.sh refuses while context.md
            # exists) is one deliberate cleanup away.
            created = sorted(p.name for p in set(env_dir.iterdir()) - before)
            dax_print('[!] new-process.sh failed ({}) — not registering an env'.format(
                'interrupted' if returncode == 'interrupted' else 'exit {}'.format(returncode)))
            if created:
                dax_print('    It left these in {}: {}'.format(env_dir, ', '.join(created)))
                dax_print('    Remove them, then run `dax run` here again.')
            sys.exit(1)

    entry = {'dir': str(env_dir), 'image': image, 'creds': creds, 'tenant': tenant}
    if features:
        entry['features'] = features
    if save_substrate:
        config['substrate'] = str(process['substrate'])
    if tenant_browser:
        if not isinstance(config.get('tenants'), dict):
            config['tenants'] = {}  # an emptied `tenants:` key loads as None
        config['tenants'][tenant] = {'claude': tenant_browser}
    config.setdefault('projects', {})[name] = entry
    save_config(config)
    dax_print('[+] registered {}'.format(name))
    return name, config['projects'][name]


# Rebuildable dependency/cache directories: excluded from `dax env
# archive` because the project's own env setup (npm install, pip install,
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


def _state_tree_tar_filter(tarinfo):
    """Never archives `.credentials.json` (decision 29). A stopped env's
    tree normally has none anyway, since teardown syncs it back to Keychain
    and deletes it, so a file found here is a crash leftover or a dead
    credential, which should never travel. backup_dir is also often
    cloud-synced, where a plaintext refresh token would outlive the archive.

    The sibling shared-files manifest (<tenant>/<project>.shared-files.json)
    needs no exclusion: it lives beside state_tree_path(), never inside it.
    """
    if Path(tarinfo.name).name == '.credentials.json':
        return None
    return tarinfo


# Archive manifest member names. Written as ARCHIVE_MANIFEST since the
# env-lifecycle consolidation (decision 23); every archive made before that
# carries the legacy name, and those must stay restorable forever, since
# these files exist for disaster recovery.
ARCHIVE_MANIFEST = 'dax-env-manifest.json'
_LEGACY_ARCHIVE_MANIFEST = 'dax-export-manifest.json'


def _archive_manifest_member(tar):
    """The manifest TarInfo in an open archive, new name first, legacy name
    as fallback, or None if it's not a dax archive at all."""
    for member_name in (ARCHIVE_MANIFEST, _LEGACY_ARCHIVE_MANIFEST):
        try:
            return tar.getmember(member_name)
        except KeyError:
            continue
    return None


def _verify_archive(path):
    """None if `path` reads back as a usable dax archive (a manifest that
    parses, and a `project/` member), else a one-line reason it doesn't.

    Run between "written" and "remove" (decision 34): removal deletes the
    only other copy, so an archive that can't be read back must stop it.
    Uses the same manifest lookup restore does, so "verified" means
    "restore can read it".
    """
    try:
        with tarfile.open(path) as tar:
            member = _archive_manifest_member(tar)
            if member is None:
                return 'no manifest found'
            json.loads(tar.extractfile(member).read())
            if not any(n == 'project' or n.startswith('project/') for n in tar.getnames()):
                return 'no project/ directory in the archive'
    except (tarfile.TarError, OSError, EOFError, ValueError) as e:
        return 'unreadable ({})'.format(e)
    return None


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
    """The base directory `dax backup` and `dax env archive`/`restore`
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


def _write_env_archive(config, name, out_path):
    """Build the manifest and tarball for one registered env, the core
    `dax env archive` writes with for both destinations (dax's managed
    backups or a specific file). Returns the manifest dict.

    Resolves the project/tenant/state-tree lookups itself rather than
    taking them from the caller, so it stays one self-contained unit.
    Credentials never travel (decision 29): the state tree is filtered, and
    every other provider's secret was Keychain-only to begin with.
    substrate/mounts travel as bare path strings, not their contents.
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
        'had_state_tree': has_state,
    }

    out_path = Path(out_path).expanduser().resolve()
    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / ARCHIVE_MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')

        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(out_path, 'w:gz') as tar:
            tar.add(manifest_path, arcname=ARCHIVE_MANIFEST)
            tar.add(process_dir, arcname='project', filter=_export_tar_filter)
            if has_state:
                tar.add(state_dir, arcname='state', filter=_state_tree_tar_filter)

    return manifest


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


def _derived_creds_for(project, name, config=None):
    """[derived credential name, ...] for one project: its implied Claude
    credential and any other per-env name dax derived for it, never an
    explicitly named one it merely lists. `config` lets resolution tell an
    explicit Claude entry (redundant, skipped) from a genuine explicit one."""
    from dax_creds.config import resolve_credential_names
    try:
        resolved = resolve_credential_names(project, name, config)
    except ValueError:
        # No tenant, so nothing was ever derived for it — already broken in
        # a way `dax run`/`env show` refuse on. Nothing to clean up.
        return []
    return [cred_name for cred_name, provider in resolved if provider]


def _teardown_process(config, name, project):
    """Delete one registered process's directory, Claude state tree (plus
    its sibling shared-files manifest), and any per-env derived credential —
    the shared core of `env destroy` and `env archive`'s removal.

    Deliberately does not touch config['projects'] or call save_config: the
    caller owns the registry edit and when to save, since destroy saves
    once per name inside its own loop while `archive --remove` batches the
    registry edits and saves once after the whole set.
    """
    from dax_creds.config import state_tree_path, shared_files_manifest_path
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
        # Leftover of the retired drift-tracking shared-files sync: real envs
        # still have one beside their tree.
        manifest = shared_files_manifest_path(tenant, name)
        if manifest.exists():
            manifest.unlink()

    for cred_name in _derived_creds_for(project, name, config):
        try:
            # The env being torn down doesn't count as still using its own
            # credential, which matters when the implied name is also
            # explicitly registered (e.g. `ot`'s claude-ysecurity-ot).
            run_creds_remove(config, cred_name, ignore_users=(name,))
        except Exception as e:
            dax_print('[!] could not remove credential {}: {}'.format(cred_name, e))


def _print_table(headers, rows):
    """Generic aligned-table printer: header row, dashed separator,
    per-column width computed from headers+data, left-justified cells.
    Same convention `dax env list` (`run_env_list`) already established.
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
    Shared by `env destroy` and `env archive`'s removal, which tear
    down the same four things."""
    _print_table(('name', 'directory', 'state tree', 'credential'), rows)


def _run_env_destroy(config, args):
    """Tear down one or more registered envs: directory, Claude state
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
        names = _q_checkbox('Select envs to destroy (nothing pre-checked):', choices)
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

        derived = _derived_creds_for(project, name, config)
        cred_cell = ', '.join(derived) if derived else '(none derived)'

        rows.append((name, dir_cell, state_cell, cred_cell))

    _print_teardown_table(rows)
    print('{} env(s) above will be permanently destroyed (including each one\'s '
          '~/.dax.yaml entry). This cannot be undone.'.format(len(names)))
    print()

    # No --dry-run (decision 30): the table above is the preview, and anything
    # but the typed literal below leaves everything untouched.
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


def _archive_envs(config, names, remove=False, out_path=None):
    """Archive `names` and, if `remove`, tear them down afterwards. The
    non-interactive core of `dax env archive`; the wizard
    (`_run_env_archive`) only gathers these arguments.

    `out_path` set: exactly one env, written to that file, never removed
    (the old `export`). Unset: each env goes to dax's managed
    backup_dir/daily/<name>-<date>.tar.gz (the old `archive`), the BCP/DR
    mechanism itself (see docs/design/2026-08-22-process-archive-restore.md).

    Removal only ever happens after every archive in the batch was written
    *and read back* successfully (decision 34), then one combined
    confirmation. A missing directory skips that one target rather than
    aborting the batch, since "back up everything" must not be taken down by
    one stale registry entry.
    """
    from dax_creds.config import state_tree_path
    from dax_creds.init import (
        _q_confirm, _container_name_for, _is_container_running,
        _tree_stats, _human_size, _human_age, _home_relative, save_config,
    )

    projects = config.get('projects') or {}
    unknown = [n for n in names if n not in projects]
    if unknown:
        dax_print('[!] not registered: {}'.format(', '.join(unknown)))
        sys.exit(1)
    if out_path is not None and (len(names) != 1 or remove):
        raise ValueError('a specific-file archive is one env, never removed')

    def _dir_for(name):
        return Path(projects[name].get('dir', '')).expanduser()

    def _has_dir(name):
        return _dir_for(name).is_dir()

    if not any(_has_dir(n) for n in names):
        dax_print('[!] nothing left to archive — every selected env is missing its directory')
        return

    # Checked for every candidate before any archive is written, so a bad
    # removal target isn't discovered only after the rest of the batch.
    if remove:
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
    if out_path is not None:
        out_paths = {names[0]: Path(out_path).expanduser().resolve()}
        header = 'Archiving {} to {}:'.format(names[0], out_paths[names[0]])
    else:
        out_paths = {name: _archive_path_for(backup_dir, name, today=today) for name in names}
        header = 'Archiving {} env(s) to {} ({}):'.format(len(names), backup_dir / 'daily', today)

    name_width = max(len(n) for n in names)
    print()
    print(header)
    written = []
    for name in names:
        padded = name.ljust(name_width)
        if not _has_dir(name):
            dax_print('  [!] {}   directory not found ({}), skipping'.format(
                padded, _dir_for(name)))
            continue
        _write_env_archive(config, name, out_paths[name])
        written.append(name)
        dax_print('  [+] {}   {}'.format(padded, _dir_for(name)))

    if not remove:
        return

    failed = [(n, _verify_archive(out_paths[n])) for n in written]
    failed = [(n, why) for n, why in failed if why]
    if failed:
        print()
        for name, why in failed:
            dax_print('[!] {}: archive {} did not read back: {}'.format(
                name, out_paths[name], why))
        dax_print('[-]   nothing removed — every archive must verify before any env is removed')
        return

    rows = []
    for name in written:
        project = projects[name]
        tenant = project.get('tenant')
        dir_cell = _home_relative(_dir_for(name))
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
        derived = _derived_creds_for(project, name, config)
        cred_cell = ', '.join(derived) if derived else '(none derived)'
        rows.append((name, dir_cell, state_cell, cred_cell))

    print()
    print('All archives above were written and read back — already backed up. About to remove:')
    _print_teardown_table(rows)

    try:
        confirmed = _q_confirm('Remove {} archived env(s) now?'.format(len(written)),
                               default=False)
    except KeyboardInterrupt:
        confirmed = False
    if not confirmed:
        dax_print('[-]   archived only — nothing removed')
        return

    for name in written:
        _teardown_process(config, name, projects[name])
        del config['projects'][name]
    save_config(config)
    for name in written:
        dax_print('[+] removed {}'.format(name))


# Sentinel for the picker's "every registered env" row. A plain object
# rather than a string, so it can never collide with a real env name.
_ARCHIVE_ALL = object()


def _run_env_archive(config, args):
    """`dax env archive [name ...]`: a wizard over `_archive_envs`.

    Merges the old `process archive` and `process export` (decision 3).
    Their flags (--all, --remove, --include-credential, --dry-run) became
    prompts or were removed (decisions 6, 29, 30). Flow: env(s), named or
    picked (with an "every env" row) → for a single env, dax's managed
    backups or a specific file → for managed, also remove afterwards? →
    summary → confirm.
    """
    from dax_creds.init import _q_checkbox, _q_confirm, _q_select, _q_path
    import questionary

    projects = config.get('projects') or {}
    if not projects:
        dax_print('[!] no registered envs to archive')
        return

    try:
        names = list(args.name or [])
        if not names:
            choices = [questionary.Choice('(every registered env)', value=_ARCHIVE_ALL)]
            for pname, project in sorted(projects.items()):
                tenant = project.get('tenant')
                label = '{}   [{}{}]'.format(
                    pname, project.get('dir', '?'),
                    ', tenant {}'.format(tenant) if tenant else ', no tenant')
                choices.append(questionary.Choice(label, value=pname))
            picked = _q_checkbox('Select envs to archive (nothing pre-checked):', choices)
            if not picked:
                dax_print('[-]   nothing selected')
                return
            names = sorted(projects) if any(p is _ARCHIVE_ALL for p in picked) else picked

        unknown = [n for n in names if n not in projects]
        if unknown:
            dax_print('[!] not registered: {}'.format(', '.join(unknown)))
            sys.exit(1)

        backup_dir = _default_backup_dir(config)
        out_path = None
        if len(names) == 1:
            where = _q_select(
                'Where should the archive go?',
                [questionary.Choice("dax's managed backups ({})".format(backup_dir / 'daily'),
                                    value='managed'),
                 questionary.Choice('A specific file', value='file')])
            if where == 'file':
                filename = '{}-{}.tar.gz'.format(names[0], datetime.date.today().isoformat())
                out_path = _q_path('Archive file:', default=str(Path.cwd() / filename))
                # Tab completion makes "a directory, then Enter" an easy
                # answer; put the archive inside it rather than crash trying
                # to open a directory as a file.
                if Path(out_path).expanduser().is_dir() or out_path.endswith(os.sep):
                    out_path = str(Path(out_path).expanduser() / filename)
                if Path(out_path).expanduser().exists() and not _q_confirm(
                        '{} already exists — overwrite it?'.format(out_path), default=False):
                    dax_print('[-]   cancelled — nothing archived')
                    return

        remove = False
        if out_path is None:
            remove = _q_confirm(
                'Also remove {} after {} archive is written and verified?'.format(
                    'it' if len(names) == 1 else 'them',
                    'its' if len(names) == 1 else 'each one\'s'),
                default=False)

        _print_table(('field', 'value'), [
            ('archive', ', '.join(names)),
            ('to', str(out_path or (backup_dir / 'daily'))),
            ('then', 'remove each one (asks again before removing)' if remove else 'nothing else'),
        ])
        if not _q_confirm('Proceed?', default=True):
            dax_print('[-]   cancelled — nothing archived')
            return
    except KeyboardInterrupt:
        print()
        dax_print('[-]   cancelled — nothing archived')
        return

    _archive_envs(config, names, remove=remove, out_path=out_path)


_ARCHIVE_FILENAME_RE = re.compile(r'^(?P<name>.+)-(?P<date>\d{4}-\d{2}-\d{2})\.tar\.gz$')


def _available_archives(backup_dir):
    """{name: [(archive_path, date_str), ...]} for every dated archive under
    backup_dir/daily/, newest first per name (decision 27: an older archive
    has to be reachable, since after "break it, then archive it" the newest
    one is the broken one). Filenames are <name>-YYYY-MM-DD.tar.gz, parsed
    back via a fixed-shape date suffix rather than a naive split, since
    `name` itself may contain hyphens."""
    daily_dir = backup_dir / 'daily'
    if not daily_dir.is_dir():
        return {}
    found = {}
    for f in daily_dir.glob('*.tar.gz'):
        m = _ARCHIVE_FILENAME_RE.match(f.name)
        if not m:
            continue
        found.setdefault(m.group('name'), []).append((f, m.group('date')))
    for archives in found.values():
        archives.sort(key=lambda pair: pair[1], reverse=True)
    return found


def _read_archive_manifest(archive):
    with tarfile.open(archive) as tar:
        member = _archive_manifest_member(tar)
        if member is None:
            dax_print('[!] {} is not a dax archive (no manifest found)'.format(archive))
            sys.exit(1)
        return json.loads(tar.extractfile(member).read())


def _rename_container_path(value, old_base, new_base):
    """`value` with its trailing `old_base` swapped for `new_base`, if (and
    only if) it is exactly a project's container path, `/home/<user>/<old>`.
    Anything else, a subdirectory or a lookalike substring included, is
    returned unchanged."""
    if not isinstance(value, str):
        return value
    path = PurePosixPath(value)
    if path.name == old_base and len(path.parts) == 4 and path.parts[:2] == ('/', 'home'):
        return str(path.parent / new_base)
    return value


def _relocate_claude_project_key(dest_state, old_dir, new_dir):
    """Re-key a restored state tree from the archived env's folder name to
    the new one, in every record Claude Code keys by the container path.

    The container path is `<container_home>/<basename-of-project-dir>`,
    exactly where `feature_workdir` mounts a project, so a restore under a
    different folder name leaves three records pointing at the old path:

    - `projects/<mangled-path>/`, the session index `/resume` reads. Found
      live, 2026-08-30: `fabric` restored as `denim` showed no `/resume`
      history, because Claude Code looked for `-home-<user>-denim`.
    - `.claude.json`'s `projects["/home/<user>/<old>"]`: trust accepted,
      `allowedTools`, MCP enablement (decision 28). Left alone, a renamed
      restore asks for trust again and loses its granted permissions.
    - every `history.jsonl` line's `project` field: prompt-history recall.

    Only exact container-path values are rewritten (`_rename_container_path`),
    never substrings. A no-op when the basename didn't change. Session
    transcripts also record `cwd` per line; they're deliberately left alone
    (see decision 28's open note).
    """
    old_base = Path(old_dir).name
    new_base = Path(new_dir).name
    if old_base == new_base:
        return

    projects_dir = dest_state / 'projects'
    if projects_dir.is_dir():
        # Claude Code mangles the path into the key; non-alphanumerics in the
        # folder name (`_`, `.`) may or may not survive that, so match either
        # form and write the new name in whichever form matched.
        def _mangle(base):
            return re.sub(r'[^A-Za-z0-9]', '-', base)
        suffix_pairs = [('-' + old_base, '-' + new_base),
                        ('-' + _mangle(old_base), '-' + _mangle(new_base))]
        for entry in list(projects_dir.iterdir()):
            if not entry.is_dir():
                continue
            for old_suffix, new_suffix in suffix_pairs:
                if entry.name.endswith(old_suffix):
                    target = projects_dir / (entry.name[:-len(old_suffix)] + new_suffix)
                    if not target.exists():
                        entry.rename(target)
                    break

    claude_json = dest_state / '.claude.json'
    if claude_json.is_file():
        try:
            data = json.loads(claude_json.read_text())
        except ValueError:
            data = None
        if isinstance(data, dict) and isinstance(data.get('projects'), dict):
            renamed = {_rename_container_path(k, old_base, new_base): v
                       for k, v in data['projects'].items()}
            if list(renamed) != list(data['projects']):
                data['projects'] = renamed
                claude_json.write_text(json.dumps(data, indent=2) + '\n')

    history = dest_state / 'history.jsonl'
    if history.is_file():
        changed = False
        out_lines = []
        for line in history.read_text().splitlines(keepends=True):
            try:
                record = json.loads(line)
            except ValueError:
                out_lines.append(line)
                continue
            if isinstance(record, dict) and 'project' in record:
                new_value = _rename_container_path(record['project'], old_base, new_base)
                if new_value != record['project']:
                    record['project'] = new_value
                    line = json.dumps(record, ensure_ascii=False) + '\n'
                    changed = True
            out_lines.append(line)
        if changed:
            history.write_text(''.join(out_lines))


def _is_effectively_empty(path):
    """True for a missing directory, or one holding nothing but `.DS_Store`
    (decision 26: Finder drops one just from opening a folder)."""
    if not path.exists():
        return True
    return all(child.name == '.DS_Store' for child in path.iterdir())


def _extract_env_archive(archive, manifest, env_dir, dest_state):
    """Extract one archive into `env_dir` (and its state tree into
    `dest_state`). Never deletes or overwrites anything (decision 26):
    refuses unless `env_dir` is effectively empty and `dest_state` doesn't
    exist, checked here in the core so no caller can reach a deletion.

    The project's *contents* move into `env_dir`, which normally already
    exists (the user created it and is standing in it). Any
    `.credentials.json` in the archived state tree is dropped before the tree
    is moved into place (decision 29), since only an old archive made with
    the removed --include-credential can carry one. Returns whether it did.
    """
    if not _is_effectively_empty(env_dir):
        raise RuntimeError('{} is not empty'.format(env_dir))
    if dest_state is not None and dest_state.exists():
        raise RuntimeError('{} already exists'.format(dest_state))

    stripped_credential = False
    with tarfile.open(archive) as tar, tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _extractall_permissive(tar, tmp)

        env_dir.mkdir(parents=True, exist_ok=True)
        project_src = tmp / 'project'
        if project_src.is_dir():
            for child in project_src.iterdir():
                if child.name == '.DS_Store' and (env_dir / child.name).exists():
                    continue
                shutil.move(str(child), str(env_dir / child.name))

        state_src = tmp / 'state'
        if manifest.get('had_state_tree') and state_src.is_dir() and dest_state is not None:
            credential = state_src / '.credentials.json'
            if credential.exists() or credential.is_symlink():
                credential.unlink()
                stripped_credential = True
            dest_state.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(state_src), str(dest_state))
            if manifest.get('dir'):
                _relocate_claude_project_key(dest_state, manifest['dir'], env_dir)

    return stripped_credential


def _restored_entry(config, manifest, env_dir, tenant):
    """The registry entry a restore writes, built in memory so it's saved
    once (decision 31), plus notes for the summary.

    - Features dax no longer has are written as commented-out `#- <name>`
      list entries, matching ~/.dax.yaml's own toggle convention, so the
      record of what the env used to have survives without being active.
    - Retired entries are dropped silently, not commented out: the archived
      `claude`/`claude_tenant_state` features and any Claude credential in
      `creds:` are what Claude being baseline with an implied credential
      replaced (decisions 35, 36), and `auggie` was removed (decision 39),
      so nothing is lost. This also covers a
      renamed restore: the copy's implied name follows its own folder name,
      so it never shares the original's grant (decision 33).
    """
    from dax_creds.config import RETIRED_FEATURES, is_retired_credential
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    from ruamel.yaml.error import CommentMark
    from ruamel.yaml.tokens import CommentToken

    known_features = _feature_names()
    features = [f for f in (manifest.get('features') or []) if f not in RETIRED_FEATURES]
    kept = [f for f in features if f in known_features]
    dropped = [f for f in features if f not in known_features]

    creds = []
    for cred in manifest.get('creds') or []:
        if not is_retired_credential(config, cred) and cred not in creds:
            creds.append(cred)

    entry = CommentedMap()
    entry['dir'] = str(env_dir)
    entry['image'] = manifest.get('image') or 'dax-base'
    entry['creds'] = creds
    if tenant:
        entry['tenant'] = tenant
    if kept or dropped:
        seq = CommentedSeq(kept)
        entry['features'] = seq
        if dropped:
            text = '\n' + ''.join('#- {}\n'.format(f) for f in dropped)
            token = CommentToken(text, CommentMark(0), None)
            if kept:
                seq.ca.items[len(kept) - 1] = [token, None, None, None]
            else:
                entry.ca.items['features'] = [None, None, token, None]
    if manifest.get('mounts'):
        entry['mounts'] = list(manifest['mounts'])
    # A substrate path in an old manifest is ignored: the one global
    # setting is used (decision 41).

    return entry, {'dropped_features': dropped}


def _print_restore_login_reminders(config, creds, tenant, name):
    from dax_creds.config import resolve_credential_names
    try:
        resolved_names = [n for n, _p in resolve_credential_names(
            {'creds': creds, 'tenant': tenant}, name, config)]
    except ValueError as e:
        dax_print('[!] {}'.format(e))
        resolved_names = creds
    for cred_name in resolved_names:
        dax_print('    dax creds login {}'.format(cred_name))


def _pick_managed_archive(config, wanted_name=None):
    """(archive path, date) from dax's managed backups: pick the env (unless
    `wanted_name` was given), then the date, newest first and pre-selected.
    The env row matching cwd's name is pre-selected too, which is the
    rollback case (decision 27). None when there's nothing to pick."""
    from dax_creds.init import _q_select
    import questionary

    backup_dir = _default_backup_dir(config)
    available = _available_archives(backup_dir)
    if not available:
        dax_print('[!] no archives found under {}'.format(backup_dir / 'daily'))
        return None

    name = wanted_name
    if name is None:
        width = max(len(n) for n in available)
        choices = [questionary.Choice(
            '{}   newest {}   ({} archive{})'.format(
                n.ljust(width), archives[0][1], len(archives), '' if len(archives) == 1 else 's'),
            value=n) for n, archives in sorted(available.items())]
        cwd_name = Path.cwd().name
        name = _q_select('Restore which env?', choices,
                         default=cwd_name if cwd_name in available else None)
    elif name not in available:
        dax_print('[!] no archive found for {!r} under {}'.format(name, backup_dir / 'daily'))
        sys.exit(1)

    archives = available[name]
    if len(archives) == 1:
        return archives[0]
    by_date = {date: (path, date) for path, date in archives}
    date = _q_select('Which archive of {}?'.format(name),
                     [questionary.Choice(date, value=date) for _path, date in archives],
                     default=archives[0][1])
    return by_date[date]


def _run_env_restore(config, args):
    """`dax env restore [name|path]`: recover one archived env into the
    current directory, which must be empty and unregistered (decisions 3,
    26). The env is named after the folder (decision 5). Merges the old
    `process restore` and `process import`.

    Restore never deletes or overwrites anything, so rolling back a live env
    is `dax env destroy` first, then this. Flow: source (dax's managed
    backups or a specific file, skipped when `name|path` is given) → for
    managed, env then date → summary → confirm → extract → register (one
    save).
    """
    from dax_creds.config import state_tree_path
    from dax_creds.init import _q_confirm, _q_path, _q_select, save_config
    import questionary

    env_dir = Path.cwd().resolve()
    name = env_dir.name
    projects = config.get('projects') or {}

    # The registration check comes first: "this is an env" says more than
    # "this isn't empty" when cwd is both, and comes with the rollback recipe.
    not_empty = not _is_effectively_empty(env_dir)
    problem = _bootstrap_dir_conflict(config, env_dir)
    if problem:
        dax_print('[!] {}{}'.format(problem, ' (and not empty)' if not_empty else ''))
        dax_print('    Restore never replaces an existing env. To roll one back, '
                  '`dax env destroy` it first, then restore into an empty folder.')
        sys.exit(1)
    if not_empty:
        dax_print('[!] {} is not empty — restore only goes into an empty folder.'.format(env_dir))
        dax_print('    Make one and run it from there: mkdir <dir> && cd <dir> && dax env restore')
        sys.exit(1)
    if name in projects:
        dax_print('[!] an env named {!r} is already registered (at {}).'.format(
            name, projects[name].get('dir', '?')))
        dax_print('    The restored env is named after this folder, so rename the folder '
                  'or restore from somewhere else.')
        sys.exit(1)

    try:
        source = getattr(args, 'source', None)
        if source and (os.sep in source or source.endswith('.tar.gz')
                       or Path(source).expanduser().is_file()):
            archive, archive_date = Path(source).expanduser().resolve(), None
        elif source:
            picked = _pick_managed_archive(config, wanted_name=source)
            if picked is None:
                return
            archive, archive_date = picked
        else:
            where = _q_select(
                'Restore from where?',
                [questionary.Choice("dax's managed backups ({})".format(
                    _default_backup_dir(config) / 'daily'), value='managed'),
                 questionary.Choice('A specific file', value='file')])
            if where == 'managed':
                picked = _pick_managed_archive(config)
                if picked is None:
                    return
                archive, archive_date = picked
            else:
                archive = Path(_q_path('Archive file:')).expanduser().resolve()
                archive_date = None

        if not archive.is_file():
            dax_print('[!] {} not found'.format(archive))
            sys.exit(1)
        manifest = _read_archive_manifest(archive)

        # Every env needs a tenant (decision 35); an archive from before that
        # rule may not record one, so ask, like the bootstrap wizard does.
        tenant = manifest.get('tenant')
        if not tenant:
            print()
            print('This archive records no tenant, and every env needs one now.')
            tenant = _prompt_tenant(env_dir, _known_tenant_names())
        dest_state = state_tree_path(tenant, name)
        if dest_state is not None and dest_state.exists():
            dax_print('[!] {} already exists — leftover Claude state with no env registered.'.format(
                dest_state))
            dax_print('    Remove it, or restore into a folder with a different name.')
            sys.exit(1)

        renamed = name != manifest.get('name')
        entry, notes = _restored_entry(config, manifest, env_dir, tenant)

        rows = [
            ('archive', '{}{}'.format(archive, '  ({})'.format(archive_date) if archive_date else '')),
            ('archived as', '{}  (from {})'.format(manifest.get('name', '?'), manifest.get('dir', '?'))),
            ('restore as', '{}{}'.format(name, '  (renamed)' if renamed else '')),
            ('into', str(env_dir)),
            ('tenant', tenant or '(none)'),
            ('state tree', str(dest_state) if manifest.get('had_state_tree') else '(none in archive)'),
            ('image', entry['image']),
            ('claude', 'claude-{}-{} (its own; logs in fresh)'.format(tenant, name)),
            ('creds', ', '.join(entry['creds']) or '(none besides Claude)'),
        ]
        if 'features' in entry:
            rows.append(('features', ', '.join(entry['features']) or '(none active)'))
        if notes['dropped_features']:
            rows.append(('', 'no longer in dax, kept commented out: {}'.format(
                ', '.join(notes['dropped_features']))))
        if 'mounts' in entry:
            rows.append(('mounts', ', '.join(entry['mounts']) + '   (paths only)'))
        if 'substrate' in (entry.get('features') or []):
            rows.append(('substrate', config.get('substrate') or
                         'NOT SET: add a top-level `substrate: <path>` (`dax config`) '
                         'before launching'))
        _print_table(('field', 'value'), rows)

        if not _q_confirm('Restore {} here?'.format(name), default=True):
            dax_print('[-]   cancelled — nothing restored')
            return
    except KeyboardInterrupt:
        print()
        dax_print('[-]   cancelled — nothing restored')
        return

    stripped = _extract_env_archive(archive, manifest, env_dir,
                                    dest_state if manifest.get('had_state_tree') else None)
    config.setdefault('projects', {})[name] = entry
    save_config(config)

    dax_print('[+] restored {} at {}'.format(name, env_dir))
    if stripped:
        dax_print('[!] this archive contained a Claude credential; it was not restored. '
                  'Consider deleting {}'.format(archive))
    _print_restore_login_reminders(config, list(entry['creds']), tenant, name)
    dax_print('    then `dax run` from {} to launch it.'.format(env_dir))


def _env_archive_explain_text(config):
    """`config` is None when ~/.dax.yaml doesn't exist — --explain must work
    with zero setup, same contract as `dax backup --explain`."""
    have_config = config is not None
    config = config or {}
    configured_backup_dir = config.get('backup_dir') or '(not set)'
    resolved_backup_dir = _default_backup_dir(config)
    registered = len(config.get('projects') or {})

    config_note = (
        '' if have_config else
        "\nNo ~/.dax.yaml found right now — the values below are what an "
        "empty config resolves to, not real settings, and there is nothing "
        "registered to archive yet.\n")

    return f"""\
dax env archive
{config_note}
The BCP/DR backup mechanism: a point-in-time snapshot of one or more
registered envs (their directory, Claude state tree, and registry
metadata), not an ongoing dotfile mirror. In most cases this replaces
GitHub as the real backup store for an env, since it captures the Claude
session state and registry entry GitHub never would.
    currently: {registered} env(s) registered in ~/.dax.yaml

If you're looking to back up dotfiles/config instead (.zshrc, .gitconfig,
CLAUDE.md, ...), you want `dax backup` instead — run `dax backup --explain`
to see what that does.

What one archive captures per env:
  - The env's own directory (dir: in ~/.dax.yaml), tarred whole, minus
    rebuildable junk directories.
  - Its Claude state tree, if it has one (transcripts, auto-memory).
  - A manifest: name, dir, tenant, image, creds, features, mounts,
    substrate, and whether a state tree existed — everything a restore
    needs to recreate the registry entry later.

What it never captures: credentials. The Claude credential
(.credentials.json) is always left out of the state tree, and every other
credential lives only in Keychain. A restored env logs in fresh.

How it runs — a short wizard, no flags:
  1. Which env(s): named on the command line, or picked from a checkbox
     list (nothing pre-checked; the first row selects every env). One env
     with a missing directory is skipped and reported, never fatal to the
     rest of the batch.
  2. Where (for a single env): dax's managed backups,
     backup_dir/daily/<name>-<date>.tar.gz, or a specific file you name.
     Several envs always go to the managed backups.
  3. For the managed backups: whether to also remove the env(s) afterwards.
     Removal happens only once every archive in the batch has been written
     *and read back* successfully, and asks once more, for the whole
     batch, before deleting each env's directory, state tree, derived
     credential, and ~/.dax.yaml entry. Refused up front if any target's
     container is running.
  4. A summary, then a confirm. Declining changes nothing.

Archiving alone is always non-destructive: nothing deleted, nothing
stopped, the registry untouched. Run it as often as you like.

Where it writes: backup_dir/daily/, the same backup_dir: setting
`dax backup` uses (defaults to backup/ inside this repo checkout; point it
at real off-site/BCP media if that's what you need).
    backup_dir: currently {configured_backup_dir}
    resolves right now to: {resolved_backup_dir}

The recovery half is restore — see
docs/design/2026-08-22-process-archive-restore.md for the full design.
"""


def _env_restore_explain_text(config):
    """`config` is None when ~/.dax.yaml doesn't exist — --explain must work
    with zero setup."""
    config = config or {}
    resolved_backup_dir = _default_backup_dir(config)
    available = _available_archives(resolved_backup_dir)
    count = sum(len(a) for a in available.values())

    return f"""\
dax env restore [name|path]

Recovers one archived env (made by `dax env archive`) into the folder you
run it from. The recovery half of the BCP/DR mechanism.
    archives available right now: {count} across {len(available)} env(s),
    under {resolved_backup_dir / 'daily'}

The rules:
  - Run it from an EMPTY folder (a lone .DS_Store is fine) that isn't a
    registered env. Restore never deletes or overwrites anything.
  - The restored env is named after that folder. Restoring `fabric` from
    ~/fabric-check registers `fabric-check`, next to the live `fabric`.
  - Credentials are never restored. The env logs in fresh on its first
    `dax run`. If an old archive still carries one, it is dropped and
    you're told so.
  - A name already registered, or leftover Claude state under that name,
    stops it before anything is touched.

How it runs — a short wizard:
  1. Source: dax's managed backups or a specific file. Giving an env name
     skips to that env's dates; giving a path skips to the summary.
  2. For the managed backups: pick the env (the one matching this folder's
     name is pre-selected), then the date (newest pre-selected; older
     archives are there too).
  3. A summary of exactly what gets registered, then a confirm. Declining
     changes nothing.

To roll a live env back: `dax env destroy <name>`, then
`mkdir <dir> && cd <dir> && dax env restore`. To look at an archive without
touching the live env, restore it under a different folder name instead.

A renamed restore re-keys the Claude state (session list, trust and
permissions, prompt history) to the new folder name, and replaces an
explicitly named Claude credential with `claude` so the copy gets its own.
"""


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
    own workdir, dotfiles, ~/.claude via the claude feature) -
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


def _fmt_explain_list(paths):
    return ', '.join(paths) if paths else '(none configured)'


def _backup_explain_text(config):
    """`config` is None when ~/.dax.yaml doesn't exist at all (--explain must
    work with zero setup) — every value below falls back to what an empty
    config would resolve to, with a note that it's hypothetical rather than
    silently showing real-looking values for a file that isn't there."""
    have_config = config is not None
    config = config or {}
    dotfiles = config.get('dotfiles') or {}
    ro = _fmt_explain_list(dotfiles.get('ro') or [])
    rw = _fmt_explain_list(dotfiles.get('rw') or [])
    backup_list = _fmt_explain_list(config.get('backup') or [])
    configured_backup_dir = config.get('backup_dir') or '(not set)'
    resolved_backup_dir = _default_backup_dir(config)

    config_note = (
        '' if have_config else
        "\nNo ~/.dax.yaml found right now — the values below are what an "
        "empty config resolves to, not real settings.\n")

    return f"""\
dax backup
{config_note}
If you're looking to back up a working process (a registered dax env's own
directory and state), you want `dax env archive` instead — run
`dax env archive --explain` to see what that does. This command only
copies dotfiles/config, described below.

Copies a fixed list of host files into backup/ inside this repo checkout
(or wherever `backup_dir:` in ~/.dax.yaml points, if you've set one) — a
plain file copy, not an archive and not off-site storage on its own.

What it copies, read fresh from ~/.dax.yaml each run:
  - dotfiles.ro   (read-only mounts, e.g. .zshrc, .vimrc)
      currently: {ro}
  - dotfiles.rw   (read-write mounts, e.g. .gitconfig, .claude.json)
      currently: {rw}
  - backup:       (anything else listed there, e.g. CLAUDE.md, settings.json)
      currently: {backup_list}

How it decides what to touch:
  - Byte-for-byte comparison against whatever's already in backup/. A file
    that hasn't changed is left alone and reported "unchanged", not
    re-copied.
  - A changed or new file is copied in (shutil.copy2 — content, mtime, and
    permissions all carried over).
  - A configured path that doesn't exist on disk right now is skipped and
    reported, never an error.
  - Nothing is ever deleted — not the source file, not a stale copy already
    sitting in backup/ for a path you've since removed from the config.

Where it writes, and why that's safe:
  - Defaults to backup/ inside this repo checkout. Deliberate: it's the one
    location guaranteed to survive a container rebuild (it's this project's
    own bind-mounted workdir), and backup/ is in .gitignore — so nothing
    copied here, including dotfiles that may carry secrets, can ever be
    `git add`ed or pushed, regardless of what lands in it.
      resolves right now to: {resolved_backup_dir}
  - A `backup_dir:` override in ~/.dax.yaml points this — and `dax env
    archive` and restore, which share the same base directory — somewhere
    else entirely: real off-site/BCP media. That trades away the
    survives-a-rebuild guarantee on purpose. Your call, never assumed.
      currently: {configured_backup_dir}

It also runs automatically, quietly, on every `dax run` (unchanged files
aren't reported then, a failure there is a warning and never blocks the
container launch) — running `dax backup` by hand just does the same pass
and shows every file's status, including the unchanged ones.

Not the same thing as `dax env archive` and restore: this is an ongoing,
incremental dotfile mirror; archive/restore is the deliberate, dated,
point-in-time snapshot-and-restore mechanism for an entire process
directory, meant to replace GitHub as the backup store for BCP/DR.
"""


def cmd_backup(args):
    try:
        with open(os.path.join(os.path.expanduser('~'), '.dax.yaml'), 'r') as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        config = None

    if getattr(args, 'explain', False):
        print(_backup_explain_text(config))
        return

    if config is None:
        _exit_missing_config()

    _run_backup(config, verbose=True)


def main():
    parser = argparse.ArgumentParser(description='DAX - Docker-based environment manager')
    parser.add_argument('--version', action='version', version='dax ' + _get_version())
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
    backup_p = subparsers.add_parser('backup', help='Back up dotfiles and config to backup/')
    backup_p.add_argument('--explain', action='store_true',
                          help='print what this command does and where it writes, then exit '
                               '(reads nothing, copies nothing)')

    creds_p = subparsers.add_parser('creds', help='Manage credentials')
    creds_sub = creds_p.add_subparsers(dest='creds_command', required=True)
    creds_sub.add_parser('add', help='Add or update a credential in the global store')
    creds_sub.add_parser('list', help='List registered credentials')
    remove_p = creds_sub.add_parser(
        'remove', help='Remove a credential from Keychain and ~/.dax.yaml (refuses if an env uses it)')
    remove_p.add_argument('name', help='Credential name to remove')
    update_p = creds_sub.add_parser('update', help='Update credential metadata (chrome profile, client_id, etc.)')
    update_p.add_argument('name', nargs='?', default=None, help='Credential name to update (omit to pick interactively)')
    login_p = creds_sub.add_parser('login', help='Authenticate a credential via its first-party auth flow')
    login_p.add_argument('name', help='Credential name to authenticate')

    # One noun, every verb: `dax env list` lists envs and the rest act on one
    # or more of them, the same shape as `dax creds` (decision 18).
    subparsers.add_parser(
        'config', help='Edit ~/.dax.yaml in $EDITOR, keeping the previous version, '
                       'then check it')

    env_p = subparsers.add_parser(
        'env', help='List, inspect, back up, restore, or remove environments',
        description='A new env is created by `dax run` in its folder; edit one with `dax config`.')
    env_sub = env_p.add_subparsers(dest='env_command', required=True)

    env_sub.add_parser('list', help='List registered environments, and Claude state '
                                    'trees no env claims')

    env_show_p = env_sub.add_parser('show', help="Show one env's configuration")
    env_show_p.add_argument('name', nargs='?', help='Env name (default: the env containing cwd)')

    env_destroy_p = env_sub.add_parser(
        'destroy',
        help='Permanently remove one or more envs: directory, state tree, '
             'derived credential, and registry entry')
    env_destroy_p.add_argument(
        'name', nargs='*',
        help='Env name(s) to destroy (omit for an interactive picker over every registered env)')

    env_archive_p = env_sub.add_parser(
        'archive',
        help="Snapshot one or more envs to dax's managed backups, or one env to a "
             'specific file (a short wizard; run with --explain for details)')
    env_archive_p.add_argument(
        'name', nargs='*',
        help='Env name(s) to archive (omit for an interactive picker)')
    env_archive_p.add_argument(
        '--explain', action='store_true',
        help='Describe what archive captures and where it writes, then exit')

    env_restore_p = env_sub.add_parser(
        'restore',
        help="Recover one env from dax's managed backups or a specific file, into "
             'the current (empty) folder (run with --explain for details)')
    env_restore_p.add_argument(
        'source', nargs='?',
        help="An archived env's name (skip to its dates) or an archive file's path "
             '(skip to the summary); omit for the full wizard')
    env_restore_p.add_argument(
        '--explain', action='store_true',
        help='Describe how restore works and what it never does, then exit')


    args = parser.parse_args()

    if args.command == 'build':
        cmd_build(args)
    elif args.command == 'run':
        cmd_run(args)
    elif args.command == 'features':
        cmd_features(args)
    elif args.command == 'backup':
        cmd_backup(args)
    elif args.command == 'creds':
        cmd_creds(args)
    elif args.command == 'env':
        cmd_env(args)
    elif args.command == 'config':
        cmd_config(args)


if __name__ == '__main__':
    main()
