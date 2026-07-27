import hashlib
from pathlib import Path
import yaml


def load_dax_config():
    config_path = Path.home() / '.dax.yaml'
    try:
        with open(config_path) as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError('~/.dax.yaml not found — run `dax init` to create it')


def find_project_by_dir(config, cwd):
    cwd = Path(cwd)
    for name, project in config.get('projects', {}).items():
        if Path(project['dir']).expanduser() == cwd:
            return project
    raise KeyError(f'no project configured for {cwd} — run `dax init` first')


def get_project_credentials(config, project):
    registry = config.get('credentials', {})
    return {name: registry[name] for name in project.get('creds', [])}


def daemon_socket_path(workdir):
    digest = hashlib.sha256(str(workdir).encode()).hexdigest()[:12]
    return Path.home() / '.dax' / f'creds-{digest}.sock'
