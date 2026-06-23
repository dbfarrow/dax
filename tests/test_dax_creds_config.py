import pytest
import yaml
from dax_creds.config import load_dax_config, find_project_by_dir, get_project_credentials


def _write_yaml(path, data):
    with open(path, 'w') as f:
        yaml.dump(data, f)


def test_load_credentials_returns_named_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_yaml(tmp_path / '.dax.yaml', {
        'credentials': {
            'github-dfarrow': {'provider': 'github'},
            'claude-work': {'provider': 'claude'},
        }
    })

    config = load_dax_config()

    assert config['credentials']['github-dfarrow']['provider'] == 'github'
    assert config['credentials']['claude-work']['provider'] == 'claude'


def test_find_project_by_dir_returns_matching_project(tmp_path):
    config = {
        'projects': {
            'my-tool': {
                'dir': str(tmp_path / 'my-tool'),
                'creds': ['github-dfarrow'],
            }
        }
    }

    project = find_project_by_dir(config, tmp_path / 'my-tool')

    assert project['creds'] == ['github-dfarrow']


def test_find_project_by_dir_raises_when_no_match(tmp_path):
    config = {'projects': {'my-tool': {'dir': str(tmp_path / 'my-tool'), 'creds': []}}}

    with pytest.raises(KeyError, match='no project configured for'):
        find_project_by_dir(config, tmp_path / 'other-dir')


def test_load_dax_config_raises_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))

    with pytest.raises(FileNotFoundError, match='~/.dax.yaml'):
        load_dax_config()


def test_get_project_credentials_resolves_named_refs(tmp_path):
    config = {
        'credentials': {
            'github-dfarrow': {'provider': 'github'},
            'claude-work': {'provider': 'claude'},
        },
        'projects': {
            'my-tool': {
                'dir': str(tmp_path),
                'creds': ['github-dfarrow'],
            }
        }
    }
    project = config['projects']['my-tool']

    creds = get_project_credentials(config, project)

    assert creds == {'github-dfarrow': {'provider': 'github'}}
