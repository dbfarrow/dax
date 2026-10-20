import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import yaml


# Every key dax reads or writes on a project (env) entry in ~/.dax.yaml. Any
# other key is unknown, almost always a typo, and `unknown_env_keys` below
# reports it (from `dax env show` and, quietly, from every `dax run`). The
# retired multi-tenant `multi_tenant`/`tenant_subdir` keys are deliberately
# absent, so an entry still carrying them is flagged too. A new per-env field
# must be added here, or it starts being flagged on its own legitimate use.
#
# Lives here rather than in init.py so building the argument parser, which
# needs the field names, does not pull in init.py's keyring/provider imports on
# every single `dax` invocation.
ENV_FIELDS = {
    'tenant': 'grouping label used for state-tree paths and credential names',
    'dir': 'absolute path to the project directory on the host',
    'image': 'docker image to run for this env',
    'creds': 'comma-separated credential names (replaces the whole list)',
    'features': 'features for this env, on top of the always-on baseline',
    'mounts': 'comma-separated host paths, each mounted at $HOME/<name> in the '
              'container (needs "mounts" in features too). A path may be '
              '<host>:<container_name> to mount under a name other than its own '
              'basename, e.g. ~/.claude:host-claude, and/or end in :ro to mount '
              'read-only instead of the read-write default, e.g. '
              '~/discernment::ro (empty middle field keeps the default name) '
              'or ~/.claude:host-claude:ro',
    'substrate': 'host path to a virgil-style substrate repo, mounted read-write and '
                 'exposed as $SUBSTRATE_ROOT (needs "substrate" in features too)',
}


def state_root():
    return Path.home() / '.local' / 'state' / 'dax' / 'tenants'


def state_tree_path(tenant, project):
    return state_root() / tenant / project


def state_trees():
    """Every (tenant, project) Claude state tree that exists on disk.

    Deliberately filesystem-driven, unlike the registry-driven listing it
    replaces: a tree whose project was deregistered or whose tenant was renamed
    is exactly what needs surfacing, and no amount of reading ~/.dax.yaml will
    ever reveal one.
    """
    root = state_root()
    if not root.is_dir():
        return {}
    return {
        (tenant_dir.name, project_dir.name): project_dir
        for tenant_dir in sorted(root.iterdir()) if tenant_dir.is_dir()
        for project_dir in sorted(tenant_dir.iterdir()) if project_dir.is_dir()
    }


DEFAULT_RELOGIN_COOLDOWN_HOURS = 24


def login_state_root():
    return Path.home() / '.local' / 'state' / 'dax' / 'creds-login'


def login_state_path(cred_name):
    return login_state_root() / '{}.json'.format(cred_name)


def record_login(cred_name, when=None):
    """Marks the login flow as having just run for `cred_name`.

    Deliberately records "the flow ran", not "a fresh grant was minted" —
    the browser round trip is itself the expensive part this exists to
    avoid repeating, and `_post_login_import`'s own before/after guard
    already refuses to import an unchanged token from an abandoned attempt.
    A sibling of the credential itself, never inside anything a container
    mounts — this is host-side bookkeeping only.
    """
    path = login_state_path(cred_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    when = when or datetime.now(timezone.utc)
    path.write_text(json.dumps({'last_login_at': when.isoformat()}))


def last_login_at(cred_name):
    path = login_state_path(cred_name)
    if not path.is_file():
        return None
    try:
        return datetime.fromisoformat(json.loads(path.read_text())['last_login_at'])
    except (json.JSONDecodeError, OSError, KeyError, ValueError):
        return None


def relogin_due(cred_name, config, now=None):
    """Whether `dax run` should proactively run the login flow for
    `cred_name` before launching, rather than trust whatever is already in
    Keychain.

    There is no reliable local signal for "is this refresh token still
    good" — `expiresAt` is the short-lived access token and says nothing,
    and `refreshTokenExpiresAt` was found not to correlate with actual mint
    date (see the 2026-07-30 decision C notes), so it cannot be trusted
    either. Rather than guess, just relogin — gated on a cooldown so
    restarting the same env several times in a day (e.g. after `dax build`)
    doesn't pop a browser window on every single restart.
    """
    last = last_login_at(cred_name)
    if last is None:
        return True
    cooldown_hours = (config.get('defaults') or {}).get(
        'relogin_cooldown_hours', DEFAULT_RELOGIN_COOLDOWN_HOURS)
    now = now or datetime.now(timezone.utc)
    return (now - last) > timedelta(hours=cooldown_hours)


# User-level files a `claude_tenant_state` tree mount replaces wholesale, taking
# your global CLAUDE.md/settings with it. A nested read-only *file* bind mount
# was tried first (2026-07-31) and abandoned within hours: unlike a nested
# directory mount, it didn't deliver the host's content at all — the container
# saw an empty file. This copies them in instead, at `dax run` time.
CLAUDE_SHARED_FILES = ('CLAUDE.md', 'settings.json', 'settings.local.json')


def shared_files_manifest_path(tenant, project):
    """Where the retired drift-tracking sync kept its per-env manifest: a
    sibling of the state tree. Nothing writes it any more; teardown still
    deletes a leftover one, since real envs have them on disk."""
    return state_root() / tenant / '{}.shared-files.json'.format(project)


def sync_claude_shared_files(tenant, project):
    """Seed the host's CLAUDE_SHARED_FILES into <tenant>/<project>'s state
    tree, once: each file is copied only if the tree doesn't have it yet, and
    never touched again (decision 13). A tree's copy is its own state from
    then on, like everything else in the tree.

    Replaced a three-way drift check (host vs tree vs a last-synced
    manifest) whose only trigger, a session under the retired shared
    `claude` mount writing the real host file, no longer exists. Known gap,
    deferred on purpose: a later edit to the host's global CLAUDE.md doesn't
    reach an already-seeded tree (see decision 13's note).
    """
    host_claude = Path.home() / '.claude'
    tree = state_tree_path(tenant, project)
    for filename in CLAUDE_SHARED_FILES:
        host_file = host_claude / filename
        tree_file = tree / filename
        # An empty file counts as missing: a 0-byte copy is never a real
        # one (a failed nested-mount experiment left exactly that in a real
        # tree), and with no drift tracking left, re-seeding it is the only
        # repair. To refresh any other copy, delete it; the next launch
        # re-seeds it.
        if host_file.is_file() and (not tree_file.exists() or tree_file.stat().st_size == 0):
            tree.mkdir(parents=True, exist_ok=True)
            tree_file.write_bytes(host_file.read_bytes())


# Per-env entries made redundant by Claude becoming a baseline feature with
# an implied credential (decisions 35, 36): filtered out wherever an env's
# `features:`/`creds:` are read, and reported until hand-removed.
LEGACY_CLAUDE_FEATURES = ('claude', 'claude_tenant_state')


def is_claude_credential(config, cred_name):
    """A bare `claude`, a name registered with provider claude, or an
    unregistered `claude-...` name (e.g. from an archive made on another
    machine, where it was registered). Registered names go by their
    provider, never by spelling."""
    if cred_name == 'claude':
        return True
    credentials = (config or {}).get('credentials') or {}
    if cred_name in credentials:
        return (credentials[cred_name] or {}).get('provider') == 'claude'
    return cred_name.startswith('claude-')


def legacy_claude_entries(config):
    """{env name: ['<entry> (features)' / '<entry> (creds)', ...]} for every
    env still listing a Claude feature or credential, which Claude being
    automatic has made redundant. Envs with none are omitted."""
    found = {}
    for name, proj in (config.get('projects') or {}).items():
        proj = proj or {}
        entries = ['{} (features)'.format(f) for f in (proj.get('features') or [])
                   if f in LEGACY_CLAUDE_FEATURES]
        entries += ['{} (creds)'.format(c) for c in (proj.get('creds') or [])
                    if is_claude_credential(config, c)]
        if entries:
            found[name] = entries
    return found


# Retired multi-tenant keys (the model was abandoned 2026-07-29 and its code
# deleted in the env-lifecycle consolidation's step 8). Outside ENV_FIELDS,
# and named separately in the warning so they don't read as typos.
RETIRED_ENV_KEYS = ('multi_tenant', 'tenant_subdir')


def describe_unknown_env_keys(keys):
    """One phrase for a list of unknown keys, telling retired ones apart from
    plain unknowns (almost always typos), which dax genuinely ignores."""
    retired = [k for k in keys if k in RETIRED_ENV_KEYS]
    other = [k for k in keys if k not in RETIRED_ENV_KEYS]
    parts = []
    if other:
        parts.append('unknown key(s) {} — ignored (typo?)'.format(', '.join(other)))
    if retired:
        parts.append('retired key(s) {} — ignored; remove them'.format(', '.join(retired)))
    return '; '.join(parts)


def unknown_env_keys(config):
    """{env name: [unknown key, ...]} for every registered env whose entry
    carries a key outside ENV_FIELDS, in registry order; envs with none are
    omitted. Typos are the point: a misspelled key is otherwise silently
    ignored, the same way a misspelled credential name (`claud-fre`) once sat
    unused in ~/.dax.yaml for weeks."""
    found = {}
    for name, proj in (config.get('projects') or {}).items():
        unknown = [k for k in (proj or {}) if k not in ENV_FIELDS]
        if unknown:
            found[name] = unknown
    return found


def yaml_round_trip(required_by):
    """A ruamel YAML handler that preserves comments, key order, and quoting.

    pyyaml cannot do this: `safe_load` never sees comments, so `dump` cannot
    put them back, and its default `sort_keys=True` alphabetises everything on
    the way out. Rewriting ~/.dax.yaml through pyyaml therefore deletes every
    comment in it — including commented-out feature lines like
    `#- claude_tenant_state`, which are configuration, not decoration.

    `required_by` names the caller in the error, since a missing dependency
    here surfaces at `dax creds add` time rather than at import time.
    """
    try:
        from ruamel.yaml import YAML
    except ImportError:
        raise ImportError(
            f'{required_by} needs ruamel.yaml to rewrite ~/.dax.yaml without '
            'destroying its comments and key order — `pip install ruamel.yaml` '
            '(or reinstall dax, which now declares it)'
        )
    handler = YAML()  # typ='rt' (round-trip) is the default
    handler.preserve_quotes = True
    # ruamel wraps at 80 columns by default, which would reflow long values —
    # absolute paths in `dir:` entries are routinely longer than that.
    handler.width = 4096
    return handler


def load_dax_config():
    """Load ~/.dax.yaml, preserving comments when ruamel is available.

    Falls back to pyyaml if it is not: every caller here only reads, so a
    missing dependency must not break `dax run`. The write path
    (`dax_creds.init.save_config`) refuses instead of falling back, since
    saving a pyyaml-loaded config is exactly what destroys the file.
    """
    config_path = Path.home() / '.dax.yaml'
    try:
        with open(config_path) as f:
            try:
                return yaml_round_trip('load_dax_config').load(f)
            except ImportError:
                f.seek(0)
                return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(missing_config_message())


def missing_config_message():
    """What to do about a missing ~/.dax.yaml (decision 37). dax never
    creates one itself: `dax init`, which used to, is gone, and a silently
    invented config is worse than a clear instruction."""
    return """\
dax needs a ~/.dax.yaml and never creates one itself. A minimal one:

    defaults:
      image: dax-base
    # Where `dax env archive` writes and `dax env restore` reads (optional;
    # defaults to backup/ inside the dax repo checkout):
    # backup_dir: ~/backups/dax
    projects: {}

Create it, then run the command again. .dax.yaml.example in the dax repo
shows every setting."""


def find_named_project_by_dir(config, cwd):
    """(name, project) for the env registered at exactly this directory.

    The name matters now that credential names are derived from it, so callers
    that need to resolve `creds:` want this rather than `find_project_by_dir`.
    """
    cwd = Path(cwd)
    for name, project in config.get('projects', {}).items():
        if Path(project['dir']).expanduser() == cwd:
            return name, project
    raise KeyError(f'no project configured for {cwd} — run `dax run` there to register it')


def find_project_by_dir(config, cwd):
    return find_named_project_by_dir(config, cwd)[1]


def find_enclosing_project(config, cwd):
    """Find a registered project whose `dir` is a strict ancestor of cwd.

    Distinct from `find_project_by_dir`'s exact-match lookup: this catches
    the case where `dax run` is invoked from *inside* an already-registered
    project (any subdirectory of it) rather than from its root. Returns
    `(name, project)` or `None`.
    """
    cwd = Path(cwd)
    for name, project in config.get('projects', {}).items():
        project_dir = Path(project['dir']).expanduser()
        if project_dir != cwd and project_dir in cwd.parents:
            return name, project
    return None


# Providers whose credential name dax derives rather than the user naming it.
# Listing a bare provider name in an env's `creds:` means "a credential of this
# kind, scoped to this env" — dax builds `<provider>-<tenant>-<project>` from
# the registry. Only claude is in here: its credentials are per tenant/project
# by design (redesign decision C), while github/gmail/ssh are genuinely
# user-level and shared, so naming those explicitly is correct.
#
# This exists because a hand-maintained convention is a convention users
# typo — a real `claud-fre` entry sat unused in ~/.dax.yaml for weeks.
BARE_PROVIDER_CREDS = ('claude',)


def derived_credential_name(provider, tenant, project):
    return f'{provider}-{tenant}-{project}'


def resolve_credential_names(project, project_name, config=None):
    """[(name, derived_provider)] for an env's credentials: always its
    implied Claude credential, `claude-<tenant>-<env>` (decision 35), then
    each non-Claude entry in its `creds:` list.

    `derived_provider` is the provider string when dax built the name, None
    when the user named the credential explicitly.

    Claude entries in `creds:` (a bare `claude`, or an explicitly named
    Claude credential) are redundant now and skipped (decision 36); `config`
    is what identifies an explicit name as a Claude one. An env with no
    tenant raises ValueError: there's no name to derive and no state tree to
    mount, and pooling into a shared default is the isolation failure the
    per-env naming exists to prevent.
    """
    tenant = project.get('tenant')
    if not tenant:
        raise ValueError(
            f"env '{project_name}' has no tenant. Every env needs one: its Claude "
            f"credential is named claude-<tenant>-{project_name} and its Claude state "
            f"lives under ~/.local/state/dax/tenants/<tenant>/. "
            f"Add `tenant: <tenant>` to its entry in ~/.dax.yaml.")
    resolved = [(derived_credential_name('claude', tenant, project_name), 'claude')]
    for entry in project.get('creds', []) or []:
        if not is_claude_credential(config, entry):
            resolved.append((entry, None))
    return resolved


def synthesized_credential(config, provider):
    """The definition dax uses for a derived credential.

    Per-provider settings come from a top-level `credential_defaults:` block,
    because a derived credential has no entry of its own to hold them:

        credential_defaults:
          claude:
            browser: chrome
            chrome_profile: Profile 2

    Browser and profile are properties of the human doing the authenticating,
    not of the env, so one setting covering every derived Claude credential is
    the right shape — and without it every per-env login would silently open
    the default browser instead of the profile the account lives in.
    """
    defaults = (config.get('credential_defaults') or {}).get(provider) or {}
    return dict(defaults, provider=provider)


def derived_credentials(config):
    """{name: definition} for every credential an env's creds list derives.

    These are real — they hold a Keychain secret once logged in — but they have
    no entry in ~/.dax.yaml, so anything enumerating credentials has to rebuild
    them the same way resolution does or they simply vanish from view.
    """
    registry = config.get('credentials', {})
    found = {}
    for project_name, project in (config.get('projects') or {}).items():
        try:
            resolved = resolve_credential_names(project, project_name, config)
        except ValueError:
            continue  # unresolvable; reported by `dax env show` and `dax run`
        for name, provider in resolved:
            if provider and name not in registry:
                found[name] = synthesized_credential(config, provider)
    return found


def credential_names_for_provider(config, provider):
    """Every credential name using this provider — registered and derived.

    Used to enforce one-grant-per-name at import time: the check needs the full
    set of names whose Keychain secrets could collide, and a derived name holds a
    secret just as a registered one does.
    """
    names = {name for name, cred in (config.get('credentials') or {}).items()
             if (cred or {}).get('provider') == provider}
    names |= {name for name, cred in derived_credentials(config).items()
              if cred.get('provider') == provider}
    return names


def credential_users(config, name):
    """Envs whose creds list resolves to this credential — derived or not."""
    users = []
    for project_name, project in (config.get('projects') or {}).items():
        try:
            resolved = resolve_credential_names(project, project_name, config)
        except ValueError:
            # No tenant, so no implied name, but the explicit non-Claude
            # names it lists are still in use: without this, `dax creds
            # remove` would delete a credential a tenantless env still names.
            resolved = [(c, None) for c in (project.get('creds') or [])
                        if not is_claude_credential(config, c)]
        if any(resolved_name == name for resolved_name, _ in resolved):
            users.append(project_name)
    return users


def get_project_credentials(config, project, project_name=None):
    """Map an env's `creds:` list to credential definitions.

    `project_name` is required to expand bare provider tokens; without it the
    list is taken literally, which is what the older call sites did.
    """
    registry = config.get('credentials', {})
    if project_name is None:
        return {name: registry[name] for name in project.get('creds', []) or []}

    creds = {}
    for name, provider in resolve_credential_names(project, project_name, config):
        if name in registry:
            creds[name] = registry[name]
        elif provider:
            # No entry of its own — synthesized rather than written to
            # ~/.dax.yaml so this stays a pure read. Per-provider defaults are
            # what carry browser/chrome_profile onto it.
            creds[name] = synthesized_credential(config, provider)
        else:
            raise KeyError(name)
    return creds


def dir_basename(path):
    """Final path component, robust to a trailing slash.

    `os.path.basename` is not: `os.path.basename('/a/b/')` is `''`, not `'b'`.
    """
    return Path(str(path).rstrip('/')).name
