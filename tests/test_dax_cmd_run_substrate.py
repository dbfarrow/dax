"""cmd_run must promote an env's `substrate` field into the flat config
feature_substrate reads — the same promotion `tenant` and `mounts` already get.

Same bug class as tests/test_dax_cmd_run_mounts.py caught for `mounts`: a
unit test of feature_substrate alone (see test_features.py) cannot catch a
wiring gap between the project registry and cmd_run's flat config, since it
never exercises cmd_run's own promotion step.
"""
import argparse

import pytest
import yaml

from dax import cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch, project_extra=None):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'a-priest-and-an-imam'
    repo.mkdir(exist_ok=True)
    (home / 'virgil').mkdir(exist_ok=True)

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    project.update(project_extra or {})

    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'features': [],
                   'projects': {'a-priest-and-an-imam': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    return home, repo


def test_an_envs_substrate_reaches_feature_substrate(tmp_path, monkeypatch, capsys):
    home, _ = _setup(tmp_path, monkeypatch, project_extra={
        'substrate': '~/virgil',
        'features': ['substrate'],
    })

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'no substrate configured' not in out
    assert f'--volume={home}/virgil:/home/' in out
    assert 'SUBSTRATE_ROOT=' in out


def test_substrate_without_the_feature_is_silently_inactive(tmp_path, monkeypatch, capsys):
    """Data present but the opt-in feature missing must not mount anything —
    matches feature_mounts'/feature_ports' existing shape, not a regression."""
    _setup(tmp_path, monkeypatch, project_extra={'substrate': '~/virgil'})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'adding substrate' not in out
    assert 'SUBSTRATE_ROOT' not in out
