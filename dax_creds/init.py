import sys
import yaml
from pathlib import Path

from dax_creds.config import daemon_socket_path
from dax_creds.providers.ssh import SshProvider

try:
    import keyring as _keyring_module
except ImportError:
    _keyring_module = None

_KEYCHAIN_SERVICE = 'dax-creds'


def register_project(config, name, project_dir, image, creds):
    existing = config.setdefault('projects', {}).get(name, {})
    existing.update({
        'dir': str(project_dir),
        'image': image,
        'creds': creds,
    })
    config['projects'][name] = existing


def save_config(config):
    config_path = Path.home() / '.dax.yaml'
    with open(config_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)


def _q_text(prompt, default=None):
    import questionary
    result = questionary.text(prompt, default=default or '').ask()
    if result is None:
        raise KeyboardInterrupt
    return result.strip() or default


def _q_select(prompt, choices):
    import questionary
    result = questionary.select(prompt, choices=choices).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _q_confirm(prompt, default=False):
    import questionary
    result = questionary.confirm(prompt, default=default).ask()
    if result is None:
        raise KeyboardInterrupt
    return result


def _q_checkbox(prompt, choices):
    import questionary
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
    elif provider_name in ('github', 'claude', 'gmail'):
        if provider_name == 'gmail':
            scope = _prompt('Gmail scope', default='readonly')
            cred_def['scope'] = scope
        picked = _pick_browser(browser_enumerator)
        cred_def['browser'] = picked['browser']
        if picked['chrome_profile']:
            cred_def['chrome_profile'] = picked['chrome_profile']
    return cred_def


def _setup_ssh_credential(cred_name, cred_def):
    provider = SshProvider()
    if provider.check(cred_def):
        print(f'  [{cred_name}] already loaded in SSH agent.')
        return
    print(f'  [{cred_name}] loading {cred_def["key"]} into macOS Keychain...')
    try:
        provider.setup(cred_def)
        print(f'  [{cred_name}] done.')
    except RuntimeError as e:
        print(f'  [{cred_name}] failed: {e}', file=sys.stderr)


def _setup_github_credential(cred_name, cred_def, config):
    from dax_creds.providers.github import GitHubProvider
    provider = GitHubProvider()

    if provider.check(cred_def, cred_name):
        print(f'  [{cred_name}] token already in Keychain.')
        return

    token = provider.import_from_disk(cred_def, credential_name=cred_name)
    if token:
        print(f'  [{cred_name}] importing existing token from ~/.config/gh/hosts.yml')
        provider.store(cred_name, token)
        print(f'  [{cred_name}] done. Consider removing the token from hosts.yml.')
        return

    if not cred_def.get('client_id'):
        client_id = _prompt('GitHub OAuth App client_id')
        if not client_id:
            print(f'  [{cred_name}] skipped — no client_id provided.')
            return
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
    except RuntimeError as e:
        print(f'  [{cred_name}] failed: {e}', file=sys.stderr)


def _setup_claude_credential(cred_name, cred_def):
    from dax_creds.providers.claude import ClaudeProvider
    provider = ClaudeProvider()

    if provider.check(cred_def, cred_name):
        print(f'  [{cred_name}] token already in Keychain.')
        return

    token = provider.import_from_disk(cred_def)
    if token:
        print(f'  [{cred_name}] importing existing token from ~/.claude/credentials.json')
        provider.store(cred_name, token)
        print(f'  [{cred_name}] done.')
        return

    print(f'  [{cred_name}] no token found — run `dax creds login {cred_name}` to authenticate.')


def run_init(config, cwd):
    print('\ndax init\n')
    try:
        return _run_init(config, cwd)
    except KeyboardInterrupt:
        print('\n\nInit cancelled. Nothing was saved.')
        sys.exit(0)


def _run_init(config, cwd):
    import questionary

    existing_project_name = next(
        (name for name, p in config.get('projects', {}).items()
         if Path(p['dir']) == cwd),
        None
    )
    existing_project = config.get('projects', {}).get(existing_project_name, {})

    if existing_project_name:
        print(f'  {cwd.name} is already registered as "{existing_project_name}".')
        if not _q_confirm('Update it?', default=False):
            print('Nothing changed.')
            return config

    default_image = existing_project.get('image') or config.get('defaults', {}).get('image', 'dax-base')
    image = _prompt('Image', default=default_image)

    existing_creds = list(config.get('credentials', {}).keys())
    already_selected = set(existing_project.get('creds', []))
    new_creds = {}

    # Single checkbox — existing creds pre-checked if already attached
    if existing_creds:
        choices = [
            questionary.Choice(name, checked=(name in already_selected))
            for name in existing_creds
        ]
        selected_creds = _q_checkbox('Select credentials for this project:', choices)
    else:
        selected_creds = []

    # Define new credentials
    while _q_confirm('Define a new credential?', default=False):
        cred_name = _prompt('Credential name (e.g. ssh-github, github-dfarrow)')
        if not cred_name:
            continue
        provider_name = _q_select('Provider:', ['ssh', 'github', 'claude', 'gmail'])
        cred_def = _define_credential(cred_name, provider_name)
        new_creds[cred_name] = cred_def
        selected_creds.append(cred_name)

    config.setdefault('credentials', {}).update(new_creds)

    for cred_name in selected_creds:
        cred_def = config['credentials'].get(cred_name) or new_creds.get(cred_name)
        if not cred_def:
            continue
        provider_name = cred_def.get('provider')
        if provider_name == 'ssh':
            _setup_ssh_credential(cred_name, cred_def)
        elif provider_name == 'github':
            _setup_github_credential(cred_name, cred_def, config)
        elif provider_name == 'claude':
            _setup_claude_credential(cred_name, cred_def)

    project_name = existing_project_name or cwd.name
    register_project(config, name=project_name, project_dir=cwd,
                     image=image, creds=selected_creds)
    save_config(config)
    print(f'\nRegistered {project_name}. Run `dax run` to launch.')
    return config


def run_creds_add(config):
    print('\ndax creds add\n')
    try:
        return _run_creds_add(config)
    except KeyboardInterrupt:
        print('\n\nCancelled. Nothing was saved.')
        sys.exit(0)


def _run_creds_add(config):
    cred_name = _prompt('Credential name (e.g. ssh-github, github-dfarrow)')
    if not cred_name:
        print('No name provided. Nothing saved.')
        return config

    existing = config.get('credentials', {}).get(cred_name)
    if existing:
        print(f'  "{cred_name}" already exists (provider: {existing.get("provider")}).')
        if not _q_confirm('Overwrite it?', default=False):
            print('Nothing changed.')
            return config

    provider_name = _q_select('Provider:', ['ssh', 'github', 'claude', 'gmail'])
    cred_def = _define_credential(cred_name, provider_name)
    config.setdefault('credentials', {})[cred_name] = cred_def

    if provider_name == 'ssh':
        _setup_ssh_credential(cred_name, cred_def)
    elif provider_name == 'github':
        _setup_github_credential(cred_name, cred_def, config)
    elif provider_name == 'claude':
        _setup_claude_credential(cred_name, cred_def)

    save_config(config)
    print(f'\nCredential "{cred_name}" saved.')
    return config


def _cred_status(config, name, cred_def):
    """Return (stored, envs_list, warnings_list) for a credential."""
    stored = bool(_keyring_module and _keyring_module.get_password(_KEYCHAIN_SERVICE, name))

    envs = []
    for proj_name, proj in config.get('projects', {}).items():
        if name in proj.get('creds', []):
            envs.append(proj_name)

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
            if ClaudeProvider().has_disk_copy(cred_def):
                warnings.append('[!] key on disk')
        except ImportError:
            pass

    return stored, envs, warnings


def run_creds_list(config):
    credentials = config.get('credentials', {})
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
        for c in proj.get('creds', []):
            if c in cred_envs:
                cred_envs[c].append(proj_name)

    _ENVS_MAX = 30
    _DETAIL_MAX = 28
    _FLAGS_MAX = 24

    col = max(len(n) for n in credentials) + 2
    print()
    print(f"  {'name':<{col}}  {'provider':<8}  {'stored':<6}  {'detail':<{_DETAIL_MAX}}  {'used by':<{_ENVS_MAX}}  warnings")
    print(f"  {'-'*col}  {'-'*8}  {'-'*6}  {'-'*_DETAIL_MAX}  {'-'*_ENVS_MAX}  {'-'*_FLAGS_MAX}")
    for name, cred_def in credentials.items():
        provider = cred_def.get('provider', '?')
        stored_bool, envs, warnings = _cred_status(config, name, cred_def)
        stored = 'yes' if stored_bool else 'no'
        if provider == 'ssh':
            detail = cred_def.get('key', '')
        elif provider in ('github', 'claude', 'gmail'):
            from dax_creds.chrome import browser_label
            detail = browser_label(cred_def.get('browser', 'default'), cred_def.get('chrome_profile'))
        else:
            detail = ''
        flags = '  '.join(warnings)
        if len(detail) > _DETAIL_MAX:
            detail = detail[:_DETAIL_MAX - 3] + '...'
        if len(flags) > _FLAGS_MAX:
            flags = flags[:_FLAGS_MAX - 3] + '...'
        envs_str = ', '.join(envs)
        if len(envs_str) > _ENVS_MAX:
            envs_str = envs_str[:_ENVS_MAX - 3] + '...'
        print(f'  {name:<{col}}  {provider:<8}  {stored:<6}  {detail:<{_DETAIL_MAX}}  {envs_str:<{_ENVS_MAX}}  {flags}')
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


def run_envs_list(config):
    projects = config.get('projects', {})
    if not projects:
        print('No environments registered.')
        return

    rows = []
    for name, proj in projects.items():
        dir_path = proj.get('dir', '')
        image = proj.get('image', '?')
        creds_str = ', '.join(proj.get('creds', [])) or '(none)'
        dir_exists = Path(dir_path).exists() if dir_path else False
        running = _is_container_running(_container_name_for(dir_path)) if dir_exists else False
        status = '*' if running else (' ' if dir_exists else '!')
        rows.append((status, name, image, dir_path, creds_str))

    nc = max(len(r[1]) for r in rows)
    ic = max(len(r[2]) for r in rows)
    dc = max(len(r[3]) for r in rows)

    print()
    print(f"  {'':3}  {'name':<{nc}}  {'image':<{ic}}  {'dir':<{dc}}  creds")
    print(f"  {'':3}  {'-'*nc}  {'-'*ic}  {'-'*dc}  {'-'*_CREDS_MAX}")
    for status, name, image, dir_path, creds_str in rows:
        if len(creds_str) > _CREDS_MAX:
            creds_str = creds_str[:_CREDS_MAX - 3] + '...'
        print(f"  [{status}]  {name:<{nc}}  {image:<{ic}}  {dir_path:<{dc}}  {creds_str}")
    print()


def run_creds_remove(config, name, keyring=None):
    if name not in config.get('credentials', {}):
        raise KeyError(name)
    kr = keyring or _keyring_module
    if kr is None:
        raise ImportError('keyring package required: pip install keyring')
    try:
        kr.delete_password(_KEYCHAIN_SERVICE, name)
        print(f'  [{name}] removed from Keychain.')
    except Exception:
        print(f'  [{name}] not found in Keychain (nothing to remove).')


def ensure_project_credentials(config, project_creds, keyring=None):
    """Return list of credential names not found in Keychain."""
    kr = keyring or _keyring_module
    missing = []

    for cred_name, cred_def in project_creds.items():
        provider = cred_def.get('provider')
        if provider == 'github':
            token = kr.get_password(_KEYCHAIN_SERVICE, cred_name) if kr else None
            if token is None:
                missing.append(cred_name)

    return missing


# Fields editable per provider (excludes 'provider' itself and Keychain-stored secrets)
_PROVIDER_FIELDS = {
    'ssh':    ['key'],
    'github': ['browser'],
    'claude': ['browser'],
    'gmail':  ['scope', 'browser'],
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
                for c in proj.get('creds', []):
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
