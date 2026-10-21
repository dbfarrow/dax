import os
import sys
import time
from pathlib import Path

from dax_creds.config import (
    BARE_PROVIDER_CREDS, ENV_FIELDS, credential_names_for_provider,
    credential_users, derived_credentials,
    last_login_at, relogin_due, resolve_credential_names, state_tree_path,
    state_trees, unknown_env_keys, retired_entries,
    describe_unknown_env_keys, synthesized_credential, credential_settings_source,
)
from dax_creds.providers.ssh import SshProvider

try:
    import keyring as _keyring_module
except ImportError:
    _keyring_module = None

_KEYCHAIN_SERVICE = 'dax-creds'


def save_config(config):
    """Rewrite ~/.dax.yaml, preserving comments, key order, and quoting.

    Refuses rather than falling back to pyyaml when ruamel is missing: a
    pyyaml rewrite silently deletes every comment in the file and alphabetises
    the keys, which is worse than not saving at all. Reads still fall back —
    see `dax_creds.config.load_dax_config`.

    Serialises fully before opening the file so a dump that raises cannot
    leave a truncated config behind. Deliberately an in-place write rather
    than write-temp-plus-rename: ~/.dax.yaml is a single-file bind mount
    inside a dax container, and single-file grpcfuse mounts are exactly where
    atomic rename breaks (see docs/design/2026-07-27-tenant-isolation.md).
    """
    from io import StringIO
    from dax_creds.config import yaml_round_trip

    buf = StringIO()
    yaml_round_trip('save_config').dump(config, buf)
    rendered = buf.getvalue()

    config_path = Path.home() / '.dax.yaml'
    with open(config_path, 'w') as f:
        f.write(rendered)


def _flush_stdin():
    """Best-effort drain of any buffered terminal input before showing a
    new interactive prompt.

    Found live, 2026-08-30: in `dax process restore`'s interactive picker
    (which asks several questionary prompts in a row, looping back to the
    top after each pick), a leftover keystroke — most likely a habitual
    double Enter confirming the previous prompt — sat in the terminal's
    input queue and was silently consumed by the *next* prompt the moment
    it started reading, instantly resolving it to its first/default choice
    before the user ever saw it. Reported live: picking "Proceed? Yes" for
    one restore, then landing already past the next loop iteration's
    top-level picker (silently "choosing" whichever env sorted first) and
    straight into that env's mode-choice screen, never having consciously
    picked it.

    POSIX only (termios) and deliberately swallow-everything: this is a UX
    nicety protecting against a hazard of chaining interactive prompts in a
    loop, not something to fail loudly over when unavailable (non-TTY
    stdin, a platform without termios, a piped/redirected input stream
    feeding scripted answers on purpose - flushing there would eat input
    the caller actually wants).
    """
    try:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except Exception:
        pass


def _q_text(prompt, default=None):
    import questionary
    _flush_stdin()
    result = questionary.text(prompt, default=default or '').ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip() or default


def _q_select(prompt, choices, default=None):
    """`default` pre-highlights the choice with that value."""
    import questionary
    _flush_stdin()
    result = questionary.select(prompt, choices=choices, default=default).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _q_confirm(prompt, default=False):
    import questionary
    _flush_stdin()
    # questionary's own default (auto_enter=True) commits a y/n answer on the
    # keypress alone, with no Enter and no moment to reconsider, so a single
    # stray or too-fast keypress could advance a multi-step wizard with no
    # recovery (found live, 2026-10). Waiting for Enter complements the
    # stdin flush above: the flush discards keys typed *before* the prompt,
    # this stops one typed *at* it from committing on its own.
    result = questionary.confirm(prompt, default=default, auto_enter=False).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _q_path(prompt, default=None):
    """A path prompt with tab completion; ~ is expanded in the result."""
    import questionary
    _flush_stdin()
    result = questionary.path(prompt, default=default or '').ask()
    if result is None:
        raise KeyboardInterrupt
    result = result.strip() or default
    return os.path.expanduser(result) if result else result


def _q_checkbox(prompt, choices):
    import questionary
    _flush_stdin()
    result = questionary.checkbox(prompt, choices=choices).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _prompt(prompt, default=None):
    return _q_text(prompt, default=default)


def _pick_from_list(prompt, options, allow_none=False):
    choices = list(options)
    if allow_none:
        choices = choices + ['(done)']
    picked = _q_select(prompt, choices)
    if allow_none and picked == '(done)':
        return None
    return picked


def _pick_browser(browser_enumerator=None):
    from dax_creds.chrome import enumerate_browsers
    import questionary
    browsers = browser_enumerator() if browser_enumerator else enumerate_browsers()
    choices = [questionary.Choice(b['label'], value=b) for b in browsers]
    picked = _q_select('Browser for OAuth flow:', choices)
    return picked


def _define_credential(cred_name, provider_name, browser_enumerator=None):
    cred_def = {'provider': provider_name}
    if provider_name == 'ssh':
        key_path = _prompt('Key file path', default='~/.ssh/id_ed25519')
        cred_def['key'] = key_path
    elif provider_name in ('gmail', 'drive'):
        client_id = _prompt('OAuth client_id')
        cred_def['client_id'] = client_id
        client_secret = _prompt('OAuth client_secret')
        cred_def['client_secret'] = client_secret
        default_scopes = provider_name  # 'gmail' or 'drive'
        scopes_raw = _prompt('Scopes (comma-separated)', default=default_scopes)
        cred_def['scopes'] = [s.strip() for s in scopes_raw.split(',') if s.strip()]
        picked = _pick_browser(browser_enumerator)
        cred_def['browser'] = picked['browser']
        if picked['chrome_profile']:
            cred_def['chrome_profile'] = picked['chrome_profile']
    elif provider_name in ('github', 'claude'):
        picked = _pick_browser(browser_enumerator)
        cred_def['browser'] = picked['browser']
        if picked['chrome_profile']:
            cred_def['chrome_profile'] = picked['chrome_profile']
    return cred_def


# Every _setup_* below returns True when the credential ends up with a usable
# secret and False when it does not, so the caller can stop announcing "saved"
# for a credential nothing was stored for. The YAML write really did happen in
# that case — but "saved" answers a different question than the one the user is
# asking, which is whether the credential works now.
#
# Every _setup_* below takes `replace`. It is False for ordinary setup, where
# an existing secret means there is nothing to do, and True only when the user
# has explicitly confirmed "Overwrite it?" in `dax creds add`. Without it the
# early-exits made that confirmation a lie: the YAML metadata was rewritten
# while the Keychain secret was left untouched, so a rotated credential could
# not be re-imported without `dax creds remove` first.
def _setup_ssh_credential(cred_name, cred_def, replace=False):
    provider = SshProvider()
    if not replace and provider.check(cred_def):
        print(f'  [{cred_name}] already loaded in SSH agent.')
        return True
    print(f'  [{cred_name}] loading {cred_def["key"]} into macOS Keychain...')
    try:
        provider.setup(cred_def)
        print(f'  [{cred_name}] done.')
        return True
    except RuntimeError as e:
        print(f'  [{cred_name}] failed: {e}', file=sys.stderr)
        return False


def _setup_github_credential(cred_name, cred_def, config, replace=False):
    from dax_creds.providers.github import GitHubProvider
    provider = GitHubProvider()

    if not replace and provider.check(cred_def, cred_name):
        print(f'  [{cred_name}] token already in Keychain.')
        return True

    token = provider.import_from_disk(cred_def, credential_name=cred_name)
    if token:
        print(f'  [{cred_name}] importing existing token from ~/.config/gh/hosts.yml')
        provider.store(cred_name, token)
        print(f'  [{cred_name}] done. Consider removing the token from hosts.yml.')
        return True

    if not cred_def.get('client_id'):
        client_id = _prompt('GitHub OAuth App client_id')
        if not client_id:
            print(f'  [{cred_name}] skipped — no client_id provided.')
            return False
        cred_def['client_id'] = client_id
        config.setdefault('credentials', {})[cred_name] = cred_def

    opener = None
    browser = cred_def.get('browser', 'default')
    chrome_profile = cred_def.get('chrome_profile')
    if browser == 'chrome' and chrome_profile:
        from dax_creds.chrome import open_url_in_profile
        opener = lambda url: open_url_in_profile(url, chrome_profile)
    elif browser and browser != 'default':
        import subprocess
        _app = {'firefox': 'Firefox', 'safari': 'Safari', 'arc': 'Arc',
                'brave': 'Brave Browser'}.get(browser, browser.title())
        opener = lambda url: subprocess.Popen(['open', '-a', _app, url])

    try:
        provider.acquire(cred_def, cred_name, opener=opener)
        return True
    except RuntimeError as e:
        print(f'  [{cred_name}] failed: {e}', file=sys.stderr)
        return False


def _setup_google_credential(cred_name, cred_def, replace=False):
    from dax_creds.providers.google import GoogleProvider
    provider = GoogleProvider(cred_def['provider'])

    if not replace and provider.check(cred_def, cred_name):
        print(f'  [{cred_name}] refresh token already in Keychain.')
        return True

    _report_no_token(cred_name, provider, cred_def, replace)
    return False


def _report_no_token(cred_name, provider, cred_def, replace):
    """Nothing was found to store. Say plainly whether the old secret survived.

    On a confirmed overwrite this matters: the user asked for the secret to be
    replaced and it was not, so silently printing the usual "no token found"
    would leave them believing the old one is gone.
    """
    print(f'  [{cred_name}] no token found — run `dax creds login {cred_name}` to authenticate.')
    if replace and provider.check(cred_def, cred_name):
        print(f'  [{cred_name}] the existing Keychain secret was left in place — '
              f'nothing new was found to replace it.')
        print(f'  [{cred_name}] to clear it anyway: dax creds remove {cred_name}')


def run_creds_add(config, cwd=None):
    """Returns (config, pending_login). See `_run_creds_add`."""
    print('\ndax creds add\n')
    try:
        return _run_creds_add(config, cwd=cwd)
    except KeyboardInterrupt:
        print('\n\nCancelled. Nothing was saved.')
        sys.exit(0)


_CRED_PROVIDERS = ['ssh', 'github', 'claude', 'gmail', 'drive']


def _run_creds_add(config, cwd=None):
    """Returns (config, pending_login) — a credential name to log in, or None.

    Provider is asked *first*, because the answer decides whether a name is even
    a question. For a per-env provider (`BARE_PROVIDER_CREDS` — claude) dax
    builds the name from the env, so prompting for one would invite exactly the
    typo decision C2 exists to eliminate: a misspelled `claud-fre` sat unused in
    ~/.dax.yaml for weeks, silently, because a name nothing matches is simply
    never used.
    """
    provider_name = _q_select('Provider:', _CRED_PROVIDERS)

    if provider_name in BARE_PROVIDER_CREDS:
        return _add_per_env_credential(config, provider_name, cwd=cwd)

    cred_name = _prompt('Credential name (e.g. ssh-github, github-dfarrow)')
    if not cred_name:
        print('No name provided. Nothing saved.')
        return config, None

    existing = config.get('credentials', {}).get(cred_name)
    replace = False
    if existing:
        print(f'  "{cred_name}" already exists (provider: {existing.get("provider")}).')
        if not _q_confirm('Overwrite it?', default=False):
            print('Nothing changed.')
            return config, None
        # Carried into setup so the provider actually re-stores the secret.
        replace = True

    cred_def = _define_credential(cred_name, provider_name)
    config.setdefault('credentials', {})[cred_name] = cred_def

    if provider_name == 'ssh':
        stored = _setup_ssh_credential(cred_name, cred_def, replace=replace)
    elif provider_name == 'github':
        stored = _setup_github_credential(cred_name, cred_def, config, replace=replace)
    else:
        stored = _setup_google_credential(cred_name, cred_def, replace=replace)

    save_config(config)
    if stored:
        print(f'\nCredential "{cred_name}" saved.')
    else:
        print(f'\nCredential "{cred_name}" was written to ~/.dax.yaml, but no secret '
              f'is stored for it yet.')
        print(f'Run `dax creds login {cred_name}` to authenticate.')
    return config, None


def _env_choices(config, cwd=None):
    """Env names, with the one containing cwd first so it is the default."""
    from dax_creds.config import find_enclosing_project, find_named_project_by_dir

    names = list((config.get('projects') or {}))
    if cwd is None:
        cwd = Path.cwd()
    here = None
    try:
        here = find_named_project_by_dir(config, cwd)[0]
    except KeyError:
        enclosing = find_enclosing_project(config, cwd)
        if enclosing:
            here = enclosing[0]
    if here in names:
        names.remove(here)
        names.insert(0, here)
    return names


def _add_per_env_credential(config, provider, cwd=None):
    """Set up a per-env credential: dax derives the name, a login mints the secret.

    Deliberately never imports a token from disk. That path copies whatever is
    already in the provider's on-disk location — under a shared `~/.claude` mount,
    some *other* env's credential — putting two names on one OAuth grant, which
    is the isolation failure decision C exists to prevent. Grant collisions are
    refused at store time too (decision C6), but not offering the copy at all is
    better than refusing it after the fact.
    """
    from dax_creds.config import derived_credential_name

    projects = config.get('projects') or {}
    if not projects:
        print(f'  no envs registered, so there is nothing to scope a {provider} '
              f'credential to.\n  Run `dax run` from the env\'s folder first to register it.')
        return config, None

    print(f'\n  {provider} credentials are per env, and dax builds the name from the '
          f'env\'s\n  tenant and name rather than asking you to type it.\n')
    env_name = _q_select('Env:', _env_choices(config, cwd=cwd))
    project = projects[env_name]

    tenant = project.get('tenant')
    if not tenant:
        # The one point where tenant genuinely is not known yet: the derived name
        # cannot be built without it, and decision C2 refuses rather than falling
        # back to a shared credential. Asked here rather than earlier because
        # tenant belongs to the env, not to the credential — everywhere else the
        # registry already knows the answer.
        print(f'  env {env_name!r} has no tenant, and the credential name is built '
              f'from it.')
        tenant = _prompt('Tenant to group this env under')
        if not tenant:
            print('  no tenant given. Nothing saved.')
            return config, None
        project['tenant'] = tenant

    cred_name = derived_credential_name(provider, tenant, env_name)
    print(f'\n  -> {cred_name}')

    # No creds-list edit: every env uses its implied Claude credential
    # automatically (decision 35), so there is nothing to opt into.

    # Browser and profile belong to the human authenticating, not to the env, so
    # they live in one `credential_defaults` block covering every derived
    # credential of this provider (decision C4) rather than being re-answered per
    # env.
    defaults = (config.get('credential_defaults') or {}).get(provider) or {}
    if not defaults:
        print(f'\n  no credential_defaults for {provider} yet — this applies to every '
              f'derived\n  {provider} credential, except where a tenant sets its own '
              f'under `tenants:`.')
        picked = _pick_browser()
        entry = {'browser': picked['browser']}
        if picked['chrome_profile']:
            entry['chrome_profile'] = picked['chrome_profile']
        if not isinstance(config.get('credential_defaults'), dict):
            config['credential_defaults'] = {}  # an emptied key loads as None
        config['credential_defaults'][provider] = entry

    save_config(config)

    stored = bool(_keyring_module
                  and _keyring_module.get_password(_KEYCHAIN_SERVICE, cred_name))
    if stored:
        print(f'\n  [{cred_name}] already has a secret in Keychain — nothing to mint.')
        print(f'  [{cred_name}] to replace it: dax creds login {cred_name}')
        return config, None

    print(f'\n  [{cred_name}] no secret yet — it is minted by its own login,')
    print(f'                so that each name is an independent OAuth grant.')
    if _q_confirm(f'Run `dax creds login {cred_name}` now?', default=True):
        return config, cred_name
    print(f'  [{cred_name}] run it when ready: dax creds login {cred_name}')
    return config, None


def _cred_status(config, name, cred_def):
    """Return (stored, envs_list, warnings_list) for a credential."""
    stored = bool(_keyring_module and _keyring_module.get_password(_KEYCHAIN_SERVICE, name))

    # Resolved rather than matched literally: an env listing a bare `claude`
    # never matches the derived name it actually uses.
    envs = credential_users(config, name)

    warnings = []
    provider = cred_def.get('provider', '?')
    if provider == 'ssh':
        from dax_creds.providers.ssh import SshProvider
        protected = SshProvider().has_passphrase(cred_def)
        if protected is False:
            warnings.append('[!] no passphrase')
    elif provider == 'github':
        try:
            from dax_creds.providers.github import GitHubProvider
            if GitHubProvider().has_disk_copy(cred_def, credential_name=name):
                warnings.append('[!] token on disk')
        except ImportError:
            pass
    elif provider == 'claude':
        try:
            from dax_creds.providers.claude import ClaudeProvider
            claude_provider = ClaudeProvider()
            # Checked per env, in each env's own state tree: that's where its
            # live credential sits while a container runs (every env has one
            # since Claude became baseline, decision 35).
            found = False
            for env_name in envs:
                proj = config.get('projects', {}).get(env_name, {})
                tenant = proj.get('tenant')
                if tenant:
                    tree_file = state_tree_path(tenant, env_name) / '.credentials.json'
                    if claude_provider.has_disk_copy(cred_def, disk_locations=[tree_file]):
                        found = True
            if found:
                warnings.append('[!] key on disk')
        except ImportError:
            pass

    return stored, envs, warnings


def run_creds_list(config):
    # Derived credentials have no entry in ~/.dax.yaml but do hold a Keychain
    # secret once logged in. Omitting them made `dax creds list` on the host
    # disagree with `dax-creds list` inside the container, which lists what the
    # daemon was actually given.
    derived = derived_credentials(config)
    credentials = dict(config.get('credentials', {}))
    credentials.update(derived)
    if not credentials:
        print('No credentials registered.')
        return

    try:
        from dax_creds.providers.github import GitHubProvider
        _github = GitHubProvider()
    except ImportError:
        _github = None

    from dax_creds.providers.ssh import SshProvider
    _ssh = SshProvider()

    # build reverse map: cred_name -> [project_name, ...]
    cred_envs = {name: [] for name in credentials}
    for proj_name, proj in config.get('projects', {}).items():
        for c in proj.get('creds') or []:
            if c in cred_envs:
                cred_envs[c].append(proj_name)

    _ENVS_MAX = 30
    _DETAIL_MAX = 28
    _FLAGS_MAX = 24
    _RELOGIN_MAX = 10

    col = max(len(n) for n in credentials) + 2
    print()
    print(f"  {'name':<{col}}  {'provider':<8}  {'stored':<6}  {'detail':<{_DETAIL_MAX}}  "
          f"{'used by':<{_ENVS_MAX}}  {'relogin':<{_RELOGIN_MAX}}  warnings")
    print(f"  {'-'*col}  {'-'*8}  {'-'*6}  {'-'*_DETAIL_MAX}  {'-'*_ENVS_MAX}  "
          f"{'-'*_RELOGIN_MAX}  {'-'*_FLAGS_MAX}")
    for name, cred_def in credentials.items():
        provider = cred_def.get('provider', '?')
        stored_bool, envs, warnings = _cred_status(config, name, cred_def)
        stored = 'yes' if stored_bool else 'no'
        if provider == 'ssh':
            detail = cred_def.get('key', '')
        elif provider in ('github', 'claude', 'gmail', 'drive'):
            from dax_creds.chrome import browser_label
            detail = browser_label(cred_def.get('browser', 'default'), cred_def.get('chrome_profile'))
        else:
            detail = ''
        # Only meaningful for a claude credential some env actually uses —
        # that's the only case `dax run` consults
        # the cooldown for at all (see CLAUDE.md's "found live, 2026-10"
        # entry). "stored: yes" above says nothing about this: it is true
        # for a well-formed Keychain blob whose refresh token Anthropic has
        # already stopped honoring, which is exactly the gap this column
        # closes — not a shape check, a "when did the flow last actually run"
        # one, since no local shape check can tell the two apart.
        relogin_col = '-'
        if provider == 'claude' and envs:
            last = last_login_at(name)
            if last is None:
                relogin_col = 'never'
            else:
                age = _human_age(last.timestamp())
                relogin_col = f'{age} due' if relogin_due(name, config) else age
        flags = '  '.join(warnings)
        if len(detail) > _DETAIL_MAX:
            detail = detail[:_DETAIL_MAX - 3] + '...'
        if len(flags) > _FLAGS_MAX:
            flags = flags[:_FLAGS_MAX - 3] + '...'
        if len(relogin_col) > _RELOGIN_MAX:
            relogin_col = relogin_col[:_RELOGIN_MAX - 3] + '...'
        envs_str = ', '.join(envs)
        if len(envs_str) > _ENVS_MAX:
            envs_str = envs_str[:_ENVS_MAX - 3] + '...'
        print(f'  {name:<{col}}  {provider:<8}  {stored:<6}  {detail:<{_DETAIL_MAX}}  '
              f'{envs_str:<{_ENVS_MAX}}  {relogin_col:<{_RELOGIN_MAX}}  {flags}')
    print()


def _container_name_for(dir_path):
    home = str(Path.home())
    return str(dir_path).replace(home, '').lstrip('/').replace('/', '-')


def _is_container_running(container_name):
    import subprocess
    try:
        result = subprocess.run(
            ['docker', 'ps', '--filter', f'name={container_name}',
             '--format', '{{.Names}}'],
            capture_output=True, text=True, timeout=3,
        )
        return container_name in result.stdout.strip().split('\n')
    except Exception:
        return False


_CREDS_MAX = 38


def _tree_stats(path):
    """(total bytes, newest mtime) for a state tree, in a single walk.

    Size and recency answer different halves of "is this worth keeping": a
    large tree nobody has opened in a year is a better deletion candidate than
    a small one from this morning.

    Newest-file mtime rather than the directory's own: a directory's mtime only
    moves when entries are added or removed, so editing a transcript in place
    would not register.
    """
    total = 0
    newest = 0.0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue  # vanished mid-walk or unreadable — not worth failing a listing over
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return total, newest


def _human_size(n):
    for unit in ('B', 'K', 'M'):
        if n < 1024:
            return f'{int(n)}{unit}'
        n /= 1024
    return f'{n:.1f}G'


def _human_age(mtime, now=None):
    """Compact relative age — the useful question is staleness, not the date."""
    if not mtime:
        return '-'
    seconds = max(0.0, (time.time() if now is None else now) - mtime)
    if seconds < 60:
        return 'now'
    for div, unit, limit in ((60, 'm', 3600), (3600, 'h', 86400),
                             (86400, 'd', 7 * 86400), (7 * 86400, 'w', 365 * 86400)):
        if seconds < limit:
            return f'{int(seconds // div)}{unit}'
    return f'{int(seconds // (365 * 86400))}y'


def _home_relative(path):
    """Shorten host paths for display. Returns the path unchanged when it isn't
    under $HOME — which is the normal case inside a container, where registry
    entries hold host paths like /Users/... that don't exist locally."""
    try:
        return '~/' + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)

def _login_browser_note(config, tenant, provider):
    """Which browser a derived credential's login opens, and why (decision
    43): the tenant's own setting, credential_defaults, or nothing set."""
    from dax_creds.chrome import browser_label
    cred = synthesized_credential(config, provider, tenant)
    label = browser_label(cred.get('browser', 'default'), cred.get('chrome_profile'))
    source = credential_settings_source(config, tenant, provider)
    if source == 'tenant':
        return f'{label}   (tenant {tenant})'
    if source == 'default':
        return f'{label}   (credential_defaults)'
    return f'{label}   (nothing set)'


def run_env_show(config, name):
    """Print one env's full registry entry plus what it resolves to."""
    projects = config.get('projects', {})
    if name not in projects:
        known = ', '.join(sorted(projects)) or '(none registered)'
        raise KeyError(f'no env named {name!r}. Known envs: {known}')

    proj = projects[name]
    dir_path = proj.get('dir', '')
    tenant = proj.get('tenant')
    creds = proj.get('creds', []) or []

    print()
    print(f'  env      {name}')
    print(f'  tenant   {tenant or "(unset)"}')
    print(f'  dir      {dir_path}{"" if Path(dir_path).exists() else "   [missing]"}')
    print(f'  image    {proj.get("image", "?")}')
    print(f'  creds    {", ".join(creds) or "(none)"}')
    # Every env's Claude credential is implied (decision 35), so show the
    # resolved names rather than leave the convention invisible, and surface
    # the "no tenant" error here, where it is cheap, rather than at launch.
    try:
        for resolved, derived_provider in resolve_credential_names(proj, name, config):
            if derived_provider:
                print(f'           -> {resolved}   [implied]' if derived_provider == 'claude'
                      else f'           -> {resolved}')
                if resolved not in (config.get('credentials') or {}):
                    print(f'              login opens {_login_browser_note(config, tenant, derived_provider)}')
    except ValueError as e:
        print(f'           -> unresolved: {e.args[0]}')

    # Shown even when empty, so it's clear nothing is opted in beyond the
    # always-on baseline (workdir, dotfiles, webpreview, claude).
    env_features = proj.get('features') or []
    print(f'  features {", ".join(env_features) or "(none beyond the always-on baseline)"}')

    # A non-empty `mounts:` switches the feature on by itself (decision 40).
    env_mounts = proj.get('mounts') or []
    if env_mounts:
        print(f'  mounts   {", ".join(env_mounts)}')

    # One global substrate, opted into per env (decision 41).
    if 'substrate' in env_features:
        global_substrate = config.get('substrate')
        print(f'  substrate {global_substrate or "NOT SET: add a top-level `substrate: <path>` (dax config)"}'
              f'{"   (global)" if global_substrate else ""}')


    container = _container_name_for(dir_path)
    if container:
        running = _is_container_running(container)
        print(f'  container {container}   [{"running" if running else "stopped"}]')

    if tenant:
        state = state_tree_path(tenant, name)
        if state.exists():
            size, used = _tree_stats(state)
            age = _human_age(used)
            if not used:
                when = 'never used'
            elif age == 'now':
                when = 'last used just now'
            else:
                when = f'last used {age} ago'
            suffix = f'   [{_human_size(size)}, {when}]'
        else:
            suffix = '   [not created yet]'
        print(f'  state    {state}{suffix}')

    # Anything outside ENV_FIELDS is a typo (ignored) or a retired
    # multi-tenant key; say which, rather than let it look like configuration.
    unknown = unknown_env_keys({'projects': {name: proj}}).get(name)
    if unknown:
        print()
        print(f'  note: {describe_unknown_env_keys(unknown)}')
    legacy = retired_entries(
        {'projects': {name: proj}, 'credentials': config.get('credentials')}).get(name)
    if legacy:
        print()
        print(f'  note: still lists {", ".join(legacy)} — no longer needed (Claude is '
              'automatic, `mounts:` switches mounts on by itself, auggie was removed); '
              'remove them')
    print()


def run_env_list(config):
    # No early return on an empty registry: deregistering every project is
    # precisely when every state tree becomes an orphan, and bailing here would
    # hide them all.
    projects = config.get('projects', {})
    trees = state_trees()

    rows = []
    for name, proj in projects.items():
        dir_path = proj.get('dir', '')
        tenant = proj.get('tenant')
        creds_str = ', '.join(proj.get('creds') or []) or '(none)'
        dir_exists = Path(dir_path).exists() if dir_path else False
        running = _is_container_running(_container_name_for(dir_path)) if dir_exists else False

        # Claiming the tree here is what makes whatever survives an orphan.
        # Only a declared tenant can claim one: without it there is no path to
        # look under, so any tree bearing this project's name stays unclaimed
        # and shows up below — which is the honest reading, since the registry
        # no longer says which tenant it belongs to.
        tree = trees.pop((tenant, name), None) if tenant else None

        size, used = _tree_stats(tree) if tree else (None, None)
        status = '*' if running else (' ' if dir_exists else '!')
        rows.append((status, tenant or '-', name, proj.get('image', '?'),
                     _home_relative(dir_path) if dir_path else '-',
                     creds_str,
                     _human_size(size) if tree else '-',
                     _human_age(used) if tree else '-'))

    # Anything still on disk has no registry entry pointing at it: the project
    # was deregistered or its tenant renamed, and its transcripts and memory
    # are still there. Invisible to every other command.
    for (tenant, name), tree in sorted(trees.items()):
        size, used = _tree_stats(tree)
        rows.append(('?', tenant, name, '-', '-', '-',
                     _human_size(size), _human_age(used)))

    if not rows:
        print('No environments registered, and no state trees on disk.')
        return

    headers = ('', 'tenant', 'name', 'image', 'dir', 'creds', 'state', 'used')
    ncols = len(headers)
    widths = [max(len(headers[i]), max(len(r[i]) for r in rows)) for i in range(ncols)]
    widths[5] = min(widths[5], _CREDS_MAX)

    def _line(cells):
        return '  [{}]  {}'.format(cells[0], '  '.join(
            str(c)[:widths[i]].ljust(widths[i]) for i, c in enumerate(cells[1:], start=1)))

    print()
    print(_line((' ',) + headers[1:]).replace('[ ]', '   ', 1))
    print(_line((' ',) + tuple('-' * widths[i] for i in range(1, ncols))).replace('[ ]', '   ', 1))
    for row in rows:
        creds_str = row[5]
        if len(creds_str) > _CREDS_MAX:
            creds_str = creds_str[:_CREDS_MAX - 3] + '...'
        print(_line(row[:5] + (creds_str,) + row[6:]))

    markers = {r[0] for r in rows}
    legend = [(m, t) for m, t in (('*', 'running'),
                                  ('!', 'directory missing'),
                                  ('?', 'orphaned state tree — no registry entry'))
              if m in markers]
    if legend:
        print()
        for marker, text in legend:
            print(f'  [{marker}] {text}')
    print()


def run_creds_remove(config, name, keyring=None, ignore_users=()):
    """Remove a credential from both Keychain and ~/.dax.yaml.

    Used to clear only the Keychain secret, leaving the definition in the
    config — so the credential kept appearing in `dax creds list` and a
    fat-fingered name could never actually be got rid of.
    """
    registered = name in config.get('credentials', {})
    # A derived credential holds a Keychain secret but has no config entry, so
    # without this it could not be removed at all — `dax creds remove` reported
    # it as unknown. Clearing one is a legitimate "re-authenticate this env"
    # operation: the next `dax run` finds it missing and offers a fresh login.
    is_derived = not registered and name in derived_credentials(config)
    if not registered and not is_derived:
        raise KeyError(name)

    if registered:
        # Checked before anything is deleted, so a refusal leaves both the
        # Keychain and the config untouched. Removing a referenced credential
        # would strand the project pointing at it with no error until its next
        # launch. Derived names are exempt — the reference is the *convention*,
        # which survives removal and simply re-mints.
        referenced = sorted(u for u in credential_users(config, name) if u not in ignore_users)
        if referenced:
            raise ValueError(
                f'{name} is still used by: {", ".join(referenced)}. Detach it first '
                f'by removing it from `creds:` in ~/.dax.yaml '
                f'(e.g. under {referenced[0]}).')

    kr = keyring or _keyring_module
    if kr is None:
        raise ImportError('keyring package required: pip install keyring')
    try:
        kr.delete_password(_KEYCHAIN_SERVICE, name)
        print(f'  [{name}] removed from Keychain.')
    except Exception:
        print(f'  [{name}] not found in Keychain (nothing to remove).')

    if is_derived:
        users = ', '.join(credential_users(config, name)) or 'no env'
        print(f'  [{name}] derived credential — nothing to remove from ~/.dax.yaml.')
        print(f'  [{name}] {users} will be offered a fresh login on the next `dax run`.')
        return

    del config['credentials'][name]
    save_config(config)
    print(f'  [{name}] removed from ~/.dax.yaml.')


def ensure_project_credentials(config, project_creds, keyring=None):
    """Return list of credential names not found in Keychain."""
    kr = keyring or _keyring_module
    missing = []

    for cred_name, cred_def in project_creds.items():
        provider = cred_def.get('provider')
        if provider in ('github', 'claude', 'gmail', 'drive'):
            token = kr.get_password(_KEYCHAIN_SERVICE, cred_name) if kr else None
            if token is None:
                missing.append(cred_name)

    return missing


# Fields editable per provider (excludes 'provider' itself and Keychain-stored secrets)
_PROVIDER_FIELDS = {
    'ssh':    ['key'],
    'github': ['browser'],
    'claude': ['browser'],
    'gmail':  ['client_id', 'client_secret', 'scopes', 'browser'],
    'drive':  ['client_id', 'client_secret', 'scopes', 'browser'],
}


def run_creds_update(config, name=None, _prompter=None, _browser_enumerator=None, _picker=None):
    import questionary

    credentials = config.get('credentials', {})

    if name is None:
        names = list(credentials.keys())
        if not names:
            print('No credentials registered.')
            return
        if _picker:
            labels = [f'{n}  ({credentials[n].get("provider", "?")})' for n in names]
            picked_label = _picker('Select credential to update:', labels)
            name = names[labels.index(picked_label)]
        else:
            stored_bool_map = {
                n: bool(_keyring_module and _keyring_module.get_password(_KEYCHAIN_SERVICE, n))
                for n in names
            }
            envs_map = {n: [] for n in names}
            for proj_name, proj in config.get('projects', {}).items():
                for c in proj.get('creds') or []:
                    if c in envs_map:
                        envs_map[c].append(proj_name)
            choices = [
                questionary.Choice(
                    f'{n}  {credentials[n].get("provider", "?")}'
                    f'  {"●" if stored_bool_map[n] else "○"}'
                    f'  {", ".join(envs_map[n]) or "—"}',
                    value=n,
                )
                for n in names
            ]
            name = _q_select('Select credential to update:', choices)

    if name not in credentials:
        raise KeyError(name)

    prompter = _prompter or (lambda prompt, default=None: _prompt(prompt, default=default))

    cred_def = credentials[name]
    provider = cred_def.get('provider', '?')
    fields = _PROVIDER_FIELDS.get(provider, [])

    # Current configuration summary
    stored_bool, envs, warnings = _cred_status(config, name, cred_def)
    print(f'\n  {name} ({provider})')
    for field in fields:
        if field == 'browser':
            from dax_creds.chrome import browser_label
            val = browser_label(cred_def.get('browser', 'default'), cred_def.get('chrome_profile'))
        else:
            val = cred_def.get(field, '(not set)')
        print(f'    {field}:    {val}')
    print(f'    stored:   {"yes" if stored_bool else "no"}')
    print(f'    used by:  {", ".join(envs) or "(none)"}')
    if warnings:
        print(f'    warnings: {", ".join(warnings)}')
    print()

    for field in fields:
        if field == 'browser':
            from dax_creds.chrome import enumerate_browsers, browser_label
            browsers = _browser_enumerator() if _browser_enumerator else enumerate_browsers()
            if _prompter:
                # Legacy path for tests: numbered list via prompter
                current_browser = cred_def.get('browser', 'default')
                current_profile = cred_def.get('chrome_profile')
                current_display = browser_label(current_browser, current_profile)
                print(f'  browser: {current_display}')
                for i, b in enumerate(browsers, 1):
                    print(f'    {i}) {b["label"]}')
                choice = prompter('  Select number (blank to keep)', default='')
                if choice:
                    try:
                        idx = int(choice) - 1
                        picked = browsers[idx] if 0 <= idx < len(browsers) else None
                    except ValueError:
                        picked = None
                    if picked:
                        cred_def['browser'] = picked['browser']
                        if picked['chrome_profile']:
                            cred_def['chrome_profile'] = picked['chrome_profile']
                        else:
                            cred_def.pop('chrome_profile', None)
            else:
                choices = [questionary.Choice(b['label'], value=b) for b in browsers]
                picked = _q_select('Browser:', choices)
                cred_def['browser'] = picked['browser']
                if picked['chrome_profile']:
                    cred_def['chrome_profile'] = picked['chrome_profile']
                else:
                    cred_def.pop('chrome_profile', None)
        else:
            current = cred_def.get(field, '')
            new_val = prompter(f'  {field}', default=current)
            if new_val is not None:
                cred_def[field] = new_val

    config['credentials'][name] = cred_def
    save_config(config)
    print(f'\n  [{name}] updated.')
