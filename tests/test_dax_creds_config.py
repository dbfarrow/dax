import pytest
import yaml
from dax_creds.config import (
    load_dax_config, find_project_by_dir, find_enclosing_project,
    get_project_credentials, dir_basename,
)


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


def test_find_enclosing_project_finds_ancestor_for_a_nested_subdir(tmp_path):
    root = tmp_path / 'discernment'
    subdir = root / 'processes' / 'ysecurity-onboard'
    config = {'projects': {'discernment': {'dir': str(root), 'multi_tenant': True}}}

    result = find_enclosing_project(config, subdir)

    assert result is not None
    name, project = result
    assert name == 'discernment'
    assert project['dir'] == str(root)


def test_find_enclosing_project_returns_none_for_the_root_itself(tmp_path):
    root = tmp_path / 'discernment'
    config = {'projects': {'discernment': {'dir': str(root)}}}

    assert find_enclosing_project(config, root) is None


def test_find_enclosing_project_returns_none_when_unrelated(tmp_path):
    root = tmp_path / 'discernment'
    unrelated = tmp_path / 'some-other-dir'
    config = {'projects': {'discernment': {'dir': str(root)}}}

    assert find_enclosing_project(config, unrelated) is None


def test_find_enclosing_project_returns_none_when_no_projects_registered(tmp_path):
    assert find_enclosing_project({}, tmp_path / 'anything') is None


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


@pytest.mark.parametrize('path, expected', [
    ('/Users/dave/my-project', 'my-project'),
    ('/Users/dave/my-project/', 'my-project'),
    ('/Users/dave/my-project//', 'my-project'),
    ('/Users/dave/my.project', 'my.project'),
    ('/Users/dave/my project', 'my project'),
    ('/Users/dave/.hidden-project', '.hidden-project'),
])
def test_dir_basename(path, expected):
    assert dir_basename(path) == expected


def test_dir_basename_accepts_path_object(tmp_path):
    assert dir_basename(tmp_path / 'my-project') == 'my-project'


def test_dir_basename_not_fooled_by_trailing_slash_unlike_os_path_basename():
    import os
    # The bug class this helper exists to avoid.
    assert os.path.basename('/Users/dave/my-project/') == ''
    assert dir_basename('/Users/dave/my-project/') == 'my-project'
