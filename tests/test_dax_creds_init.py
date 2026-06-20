import pytest
import yaml
from pathlib import Path
from dax_creds.init import register_project, save_config


def _load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def test_register_project_adds_project_entry(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config_path = tmp_path / '.dax.yaml'
    config_path.write_text(yaml.dump({
        'defaults': {'image': 'dax-base'},
        'credentials': {},
        'projects': {},
    }))

    project_dir = tmp_path / 'my-project'
    config = _load_yaml(config_path)
    register_project(config, name='my-project', project_dir=project_dir,
                     image='dax-base', creds=['github-dfarrow'])

    assert config['projects']['my-project']['dir'] == str(project_dir)
    assert config['projects']['my-project']['creds'] == ['github-dfarrow']
    assert config['projects']['my-project']['image'] == 'dax-base'


def test_save_config_writes_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {
        'projects': {'my-project': {'dir': str(tmp_path), 'creds': []}},
    }

    save_config(config)

    saved = _load_yaml(tmp_path / '.dax.yaml')
    assert saved['projects']['my-project']['dir'] == str(tmp_path)
