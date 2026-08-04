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

    _write_yaml(home / '.dax.yaml', {'image': 'test/dax:latest'})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert config['image'] == 'test/dax:latest'


def test_load_config_merges_local_config(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'project'
    workdir.mkdir()

    local = {
        'features': ['aws'],
        'awsdir': {'host': '/Users/test/.aws', 'container': '/home/test/.aws'},
    }
    _write_yaml(home / '.dax.yaml', {'image': 'test/dax:latest'})
    _write_yaml(workdir / '.dax.yaml', local)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert 'workdir' in config['features']
    assert 'aws' in config['features']


# --- always-on baseline features --------------------------------------------
#
# ~/.dax.yaml's top-level `features:` key was removed 2026-08 — it was the
# actual cause of the additive-only, no-opt-out problem (an env could never
# decline something every other env also got by default). workdir/dotfiles/
# webpreview are wanted unconditionally by every env, so they're a baseline in
# code now rather than something read from that list; anything else optional
# is per-env (project features:, cwd-local .dax.yaml, or -f) only.

def test_load_config_baseline_features_present_with_no_top_level_features_key(
        tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = home / 'work'
    workdir.mkdir()

    _write_yaml(home / '.dax.yaml', {'image': 'test/dax:latest'})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert set(config['features']) == {'workdir', 'dotfiles', 'webpreview'}


def test_load_config_ignores_a_legacy_top_level_features_list(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = home / 'work'
    workdir.mkdir()

    _write_yaml(home / '.dax.yaml', {'image': 'test/dax:latest', 'features': ['claude', 'ssh']})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert 'claude' not in config['features']
    assert 'ssh' not in config['features']
    assert {'workdir', 'dotfiles', 'webpreview'} <= set(config['features'])


def test_load_config_warns_about_a_legacy_top_level_features_list(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = home / 'work'
    workdir.mkdir()

    _write_yaml(home / '.dax.yaml', {'image': 'test/dax:latest', 'features': ['claude']})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    load_config()

    out = capsys.readouterr().out
    assert 'no longer read' in out
    assert 'claude' in out


def test_load_config_says_nothing_when_no_legacy_features_list_is_present(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = home / 'work'
    workdir.mkdir()

    _write_yaml(home / '.dax.yaml', {'image': 'test/dax:latest'})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    load_config()

    assert 'no longer read' not in capsys.readouterr().out


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
