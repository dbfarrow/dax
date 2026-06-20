import sys
import yaml
from pathlib import Path

from dax_creds.config import daemon_socket_path
from dax_creds.providers.ssh import SshProvider


def register_project(config, name, project_dir, image, creds):
    config.setdefault('projects', {})[name] = {
        'dir': str(project_dir),
        'image': image,
        'creds': creds,
    }


def save_config(config):
    config_path = Path.home() / '.dax.yaml'
    with open(config_path, 'w') as f:
        yaml.dump(config, f, default_flow_style=False)


def _prompt(prompt, default=None):
    suffix = f' [{default}]' if default else ''
    value = input(f'{prompt}{suffix}: ').strip()
    return value or default


def _pick_from_list(prompt, options, allow_none=False):
    print(prompt)
    for i, opt in enumerate(options, 1):
        print(f'  {i}) {opt}')
    if allow_none:
        print('  0) none / done')
    while True:
        raw = input('> ').strip()
        if allow_none and raw == '0':
            return None
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            pass
        print('Invalid selection.')


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


def run_init(config, cwd):
    print('\ndax init\n')
    try:
        return _run_init(config, cwd)
    except KeyboardInterrupt:
        print('\n\nInit cancelled. Nothing was saved.')
        sys.exit(0)


def _run_init(config, cwd):
    existing = next(
        (name for name, p in config.get('projects', {}).items()
         if Path(p['dir']) == cwd),
        None
    )
    if existing:
        print(f'  {cwd.name} is already registered as "{existing}".')
        answer = _prompt('Re-run init to update it? [y/N]', default='N')
        if answer.lower() != 'y':
            print('Nothing changed.')
            return config

    default_image = config.get('defaults', {}).get('image', 'dax-base')
    image = _prompt('Image', default=default_image)

    existing_creds = list(config.get('credentials', {}).keys())
    selected_creds = []
    new_creds = {}

    print('\nCredentials (select existing or define new; 0 when done):')
    while True:
        options = existing_creds + ['[ new credential ]']
        choice = _pick_from_list('Add a credential:', options, allow_none=True)
        if choice is None:
            break
        if choice == '[ new credential ]':
            cred_name = _prompt('Credential name (e.g. ssh-github, github-dfarrow)')
            if not cred_name:
                continue
            provider = _pick_from_list('Provider:', ['ssh', 'github', 'claude', 'gmail'])
            cred_def = {'provider': provider}
            if provider == 'ssh':
                key_path = _prompt('Key file path', default='~/.ssh/id_ed25519')
                cred_def['key'] = key_path
            elif provider in ('claude', 'gmail'):
                from dax_creds.chrome import enumerate_profiles
                if provider == 'gmail':
                    scope = _prompt('Gmail scope', default='readonly')
                    cred_def['scope'] = scope
                profiles = enumerate_profiles()
                if profiles:
                    print('\nWhich Chrome profile should handle the OAuth flow?')
                    profile_labels = [
                        f'{p["name"]}  ({p["email"]})  [{p["directory"]}]'
                        for p in profiles
                    ]
                    choice = _pick_from_list('Chrome profile:', profile_labels)
                    idx = profile_labels.index(choice)
                    cred_def['chrome_profile'] = profiles[idx]['directory']
                else:
                    print('  Chrome profile list unavailable — OAuth will use default browser.')
            new_creds[cred_name] = cred_def
            existing_creds.append(cred_name)
            selected_creds.append(cred_name)
        else:
            if choice not in selected_creds:
                selected_creds.append(choice)

    for cred_name in selected_creds:
        cred_def = config.get('credentials', {}).get(cred_name) or new_creds.get(cred_name)
        if cred_def and cred_def.get('provider') == 'ssh':
            _setup_ssh_credential(cred_name, cred_def)

    config.setdefault('credentials', {}).update(new_creds)

    project_name = cwd.name
    register_project(config, name=project_name, project_dir=cwd,
                     image=image, creds=selected_creds)
    save_config(config)
    print(f'\nRegistered {project_name}. Run `dax run` to launch.')
    return config
