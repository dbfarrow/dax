import os
import sys
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import load_config


def _write_yaml(path, data):
    with open(path, 'w') as f:
        yaml.dump(data, f)


def test_load_config_reads_home_defaults(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'work'
    workdir.mkdir()

    defaults = {
        'image': 'test/dax:latest',
        'features': ['workdir'],
    }
    _write_yaml(home / '.dax.yaml', defaults)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert config['image'] == 'test/dax:latest'
    assert 'workdir' in config['features']


def test_load_config_merges_local_config(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'project'
    workdir.mkdir()

    defaults = {
        'image': 'test/dax:latest',
        'features': ['workdir'],
    }
    local = {
        'features': ['aws'],
        'awsdir': {'host': '/Users/test/.aws', 'container': '/home/test/.aws'},
    }
    _write_yaml(home / '.dax.yaml', defaults)
    _write_yaml(workdir / '.dax.yaml', local)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert 'workdir' in config['features']
    assert 'aws' in config['features']


def test_load_config_sets_envname(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'foo' / 'bar'
    workdir.mkdir(parents=True)

    _write_yaml(home / '.dax.yaml', {'image': 'x', 'features': []})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert config['envname'] == 'foo-bar'


def test_load_config_sets_workdir_name(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'foo' / 'bar'
    workdir.mkdir(parents=True)

    _write_yaml(home / '.dax.yaml', {'image': 'x', 'features': []})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    # envname is the full path relative to $HOME (used for --name); workdir_name
    # is just the final component (used for the mount destination) — they
    # diverge whenever cwd is more than one level deep.
    assert config['envname'] == 'foo-bar'
    assert config['workdir_name'] == 'bar'


def test_load_config_workdir_name_handles_dotted_and_spaced_dirs(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'my.project (copy)'
    workdir.mkdir(parents=True)

    _write_yaml(home / '.dax.yaml', {'image': 'x', 'features': []})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert config['workdir_name'] == 'my.project (copy)'


def test_load_config_exits_if_outside_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'other'
    outside.mkdir()

    _write_yaml(home / '.dax.yaml', {'image': 'x', 'features': []})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(outside)

    try:
        load_config()
        assert False, "should have exited"
    except SystemExit:
        pass
