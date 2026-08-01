"""cmd_run must promote an env's `mounts:` list into the flat config
feature_mounts reads — the same promotion `tenant` and `features` already get.

Found 2026-07-31: `dax env show` displayed a configured `mounts` entry
correctly (it reads straight from the project dict), but `dax run` printed
"no mounts defined for this env" and built no --volume for it. feature_mounts
itself was fine in isolation (see test_features.py) — cmd_run just never
copied project['mounts'] into config['mounts'] the way it does for
project['tenant'], so the field was invisible at launch despite `env show`
and `env set` both working. Only an integration test through cmd_run itself,
not a unit test of feature_mounts alone, catches this class of bug.
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
    (home / 'discernment').mkdir(exist_ok=True)

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    project.update(project_extra or {})

    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'features': [],
                   'projects': {'a-priest-and-an-imam': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    return home, repo


def test_an_envs_mounts_list_reaches_feature_mounts(tmp_path, monkeypatch, capsys):
    home, _ = _setup(tmp_path, monkeypatch, project_extra={
        'mounts': ['~/discernment'],
        'features': ['mounts'],
    })

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'no mounts defined' not in out
    assert f'--volume={home}/discernment:/home/' in out


def test_mounts_without_the_feature_is_silently_inactive(tmp_path, monkeypatch, capsys):
    """Data present but the opt-in feature missing must not mount anything —
    matches feature_ports' existing shape, not a regression."""
    _setup(tmp_path, monkeypatch, project_extra={'mounts': ['~/discernment']})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'adding mounts' not in out
    assert 'discernment' not in out
