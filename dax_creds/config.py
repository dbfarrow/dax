import hashlib
import json
from pathlib import Path
import yaml


# Settable through `dax env set`. Deliberately not the full set of keys that
# may appear on a registry entry: `multi_tenant`/`tenant_subdir` are slated for
# deletion with the multi-tenant model (see the design doc's 2026-07-29
# redesign) and are not worth teaching anyone to set. `features` is not here
# either — it lives at the top level of ~/.dax.yaml or in a project-local
# .dax.yaml, not on the registry entry.
#
# Lives here rather than in init.py so building the argument parser, which
# needs the field names, does not pull in init.py's keyring/provider imports on
# every single `dax` invocation.
ENV_FIELDS = {
    'tenant': 'grouping label used for state-tree paths and credential names',
    'dir': 'absolute path to the project directory on the host',
    'image': 'docker image to run for this env',
    'creds': 'comma-separated credential names (replaces the whole list)',
    'features': 'comma-separated features for this env, on top of the global list',
    'mounts': 'comma-separated host paths, each mounted read-write at $HOME/<name> '
              'in the container (needs "mounts" in features too). A path may be '
              '<host>:<container_name> to mount under a name other than its own '
              'basename, e.g. ~/.claude:host-claude',
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


# User-level files a `claude_tenant_state` tree mount replaces wholesale, taking
# your global CLAUDE.md/settings with it. A nested read-only *file* bind mount
# was tried first (2026-07-31) and abandoned within hours: unlike a nested
# directory mount, it didn't deliver the host's content at all — the container
# saw an empty file. This copies them in instead, at `dax run` time.
CLAUDE_SHARED_FILES = ('CLAUDE.md', 'settings.json', 'settings.local.json')


def _shared_files_manifest_path(tenant, project):
    # A sibling of the tree, not inside it — anything under state_tree_path()
    # is mounted straight into the container's ~/.claude, and this bookkeeping
    # has no business showing up there.
    return state_root() / tenant / '{}.shared-files.json'.format(project)


def _load_shared_files_manifest(tenant, project):
    path = _shared_files_manifest_path(tenant, project)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_shared_files_manifest(tenant, project, manifest):
    path = _shared_files_manifest_path(tenant, project)
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + '\n')


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sync_claude_shared_files(tenant, project, force=False):
    """Copy the host's CLAUDE_SHARED_FILES into <tenant>/<project>'s state tree.

    Three-way comparison against a small manifest (filename -> sha256 of what
    was last synced in), because a plain host-vs-tree diff can't tell "host
    moved on since last sync" (safe to overwrite) apart from "something changed
    the tree's copy independently" (a container editing it directly, now that
    it's a plain file and not a mount — exactly what should not be clobbered
    silently). force=True (the `accept-shared-files` escape hatch) skips that
    distinction and always takes the host's version.

    Returns the list of filenames left alone because they'd drifted and
    force was False — the caller's cue to warn.
    """
    host_claude = Path.home() / '.claude'
    tree = state_tree_path(tenant, project)
    manifest = _load_shared_files_manifest(tenant, project)
    warnings = []
    changed = False

    for filename in CLAUDE_SHARED_FILES:
        host_file = host_claude / filename
        if not host_file.is_file():
            continue
        host_bytes = host_file.read_bytes()
        host_hash = _sha256_bytes(host_bytes)
        tree_file = tree / filename

        if not tree_file.is_file():
            tree.mkdir(parents=True, exist_ok=True)
            tree_file.write_bytes(host_bytes)
            manifest[filename] = host_hash
            changed = True
            continue

        tree_bytes = tree_file.read_bytes()
        tree_hash = _sha256_bytes(tree_bytes)
        if tree_hash == host_hash:
            if manifest.get(filename) != tree_hash:
                manifest[filename] = tree_hash
                changed = True
            continue

        if force or manifest.get(filename) == tree_hash:
            tree_file.write_bytes(host_bytes)
            manifest[filename] = host_hash
            changed = True
        else:
            warnings.append(filename)

    if changed:
        _save_shared_files_manifest(tenant, project, manifest)

    return warnings


def env_field_help():
    width = max(len(f) for f in ENV_FIELDS)
    return 'fields:\n' + '\n'.join(
        f'  {f:<{width}}  {desc}' for f, desc in ENV_FIELDS.items())


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
        raise FileNotFoundError('~/.dax.yaml not found — run `dax init` to create it')


def find_named_project_by_dir(config, cwd):
    """(name, project) for the env registered at exactly this directory.

    The name matters now that credential names are derived from it, so callers
    that need to resolve `creds:` want this rather than `find_project_by_dir`.
    """
    cwd = Path(cwd)
    for name, project in config.get('projects', {}).items():
        if Path(project['dir']).expanduser() == cwd:
            return name, project
    raise KeyError(f'no project configured for {cwd} — run `dax init` first')


def find_project_by_dir(config, cwd):
    return find_named_project_by_dir(config, cwd)[1]


def find_enclosing_project(config, cwd):
    """Find a registered project whose `dir` is a strict ancestor of cwd.

    Distinct from `find_project_by_dir`'s exact-match lookup: this catches
    the case where `dax run` is invoked from *inside* an already-registered
    project (e.g. a multi-tenant repo's tenant_subdir child) rather than
    from its root. Returns `(name, project)` or `None`.
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


def resolve_credential_names(project, project_name):
    """[(name, derived_provider)] for each entry in an env's `creds:` list.

    `derived_provider` is the provider string when dax built the name, None
    when the user named the credential explicitly.

    An explicitly named credential always wins over the convention, which keeps
    existing configs working and leaves an escape hatch for deliberately
    sharing one credential across two envs.
    """
    resolved = []
    for entry in project.get('creds', []) or []:
        if entry not in BARE_PROVIDER_CREDS:
            resolved.append((entry, None))
            continue
        tenant = project.get('tenant')
        if not tenant:
            raise ValueError(
                f"env '{project_name}' asks for a '{entry}' credential by convention, "
                f"but has no tenant — the name is derived as {entry}-<tenant>-<project>. "
                f"Set one with `dax env set {project_name} tenant <tenant>`.")
        resolved.append((derived_credential_name(entry, tenant, project_name), entry))
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
            resolved = resolve_credential_names(project, project_name)
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
            resolved = resolve_credential_names(project, project_name)
        except ValueError:
            continue
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
    for name, provider in resolve_credential_names(project, project_name):
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


def daemon_socket_path(workdir):
    digest = hashlib.sha256(str(workdir).encode()).hexdigest()[:12]
    return Path.home() / '.dax' / f'creds-{digest}.sock'


def dir_basename(path):
    """Final path component, robust to a trailing slash.

    `os.path.basename` is not: `os.path.basename('/a/b/')` is `''`, not `'b'`.
    """
    return Path(str(path).rstrip('/')).name
