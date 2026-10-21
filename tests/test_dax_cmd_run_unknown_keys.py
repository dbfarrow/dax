"""`dax run` warns about unknown keys on *any* registered env's entry, not
just the one launching — quietly, and without blocking the launch.

The failure this guards against was real: a misspelled credential name
(`claud-fre`) sat unused in ~/.dax.yaml for weeks with nothing to say so. A
misspelled *key* is worse, since dax ignores it outright; checking every env
on every launch means a typo in an env nobody has opened lately still
surfaces on the very next `dax run` of anything.
"""
import argparse

import yaml

import dax
from dax import cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch, extra_projects=None, myenv_extra=None):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'myenv'
    repo.mkdir(exist_ok=True)

    myenv = {'dir': str(repo), 'tenant': 'personal', 'creds': [], 'features': []}
    myenv.update(myenv_extra or {})
    projects = {'myenv': myenv}
    projects.update(extra_projects or {})
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': projects}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(dax, '_sync_and_clear_claude_credential', lambda *a, **k: None)
    return home


def test_warns_about_an_unknown_key_on_another_env(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, extra_projects={
        'other': {'dir': str(tmp_path / 'other'), 'tenant': 'personal', 'feautres': ['mounts']},
    })

    cmd_run(_args())

    out = capsys.readouterr().out
    assert "env 'other' has unknown key(s) feautres — ignored (typo?)" in out


def test_warns_about_an_unknown_key_on_the_launching_env(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, myenv_extra={'tennant': 'x'})

    cmd_run(_args())

    assert "env 'myenv' has unknown key(s) tennant" in capsys.readouterr().out


def test_no_warning_when_every_key_is_known(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    cmd_run(_args())

    assert 'unknown key' not in capsys.readouterr().out


def test_the_launch_still_proceeds(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, myenv_extra={'bogus': 1})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'unknown key' in out
    assert 'docker' in out  # test_only prints the assembled docker command


def test_retired_multi_tenant_keys_are_named_as_retired(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, extra_projects={
        'old': {'dir': str(tmp_path / 'old'), 'multi_tenant': True},
    })

    cmd_run(_args())

    out = capsys.readouterr().out
    assert "env 'old' has retired key(s) multi_tenant — ignored; remove them" in out
