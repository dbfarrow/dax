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


def get_project_credentials(config, project):
    registry = config.get('credentials', {})
    return {name: registry[name] for name in project.get('creds', [])}


def daemon_socket_path(workdir):
    digest = hashlib.sha256(str(workdir).encode()).hexdigest()[:12]
    return Path.home() / '.dax' / f'creds-{digest}.sock'


def dir_basename(path):
    """Final path component, robust to a trailing slash.

    `os.path.basename` is not: `os.path.basename('/a/b/')` is `''`, not `'b'`.
    """
    return Path(str(path).rstrip('/')).name
