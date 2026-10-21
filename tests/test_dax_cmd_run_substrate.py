"""The substrate is one global top-level `substrate:` path, which an env opts
into with `features: [substrate]` (env-lifecycle consolidation, decision 41).
Exercised through `cmd_run`, since the wiring between the registry and
feature_substrate is where these bugs live.

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


def _setup(tmp_path, monkeypatch, project_extra=None, global_substrate='~/virgil'):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'a-priest-and-an-imam'
    repo.mkdir(exist_ok=True)
    # feature_substrate refuses a mount with no shared/wire.sh (2026-08-31),
    # so a real fixture needs one to still count as "available."
    wire_sh = home / 'virgil' / 'shared' / 'wire.sh'
    wire_sh.parent.mkdir(parents=True, exist_ok=True)
    wire_sh.write_text('#!/bin/sh\n')
    wire_sh.chmod(0o755)

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    project.update(project_extra or {})

    config = {'image': 'test/dax:latest', 'projects': {'a-priest-and-an-imam': project}}
    if global_substrate:
        config['substrate'] = global_substrate
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump(config, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    return home, repo


def test_an_env_that_opts_in_mounts_the_global_substrate(tmp_path, monkeypatch, capsys):
    home, _ = _setup(tmp_path, monkeypatch, project_extra={'features': ['substrate']})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert f'--volume={home}/virgil:/home/' in out
    assert 'SUBSTRATE_ROOT=' in out


def test_an_env_that_does_not_opt_in_gets_no_substrate(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'adding substrate' not in out
    assert 'SUBSTRATE_ROOT' not in out


def test_opting_in_with_no_global_substrate_is_refused(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'features': ['substrate']},
           global_substrate=None)

    with pytest.raises(SystemExit) as excinfo:
        cmd_run(_args())

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert 'no substrate is set' in out and 'top-level `substrate: <path>`' in out


def test_a_per_env_substrate_is_a_retired_key_and_not_used(tmp_path, monkeypatch, capsys):
    """The old per-env path doesn't stand in for the global one."""
    home, _ = _setup(tmp_path, monkeypatch,
                     project_extra={'features': ['substrate'], 'substrate': '~/elsewhere'})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert "retired key substrate — ignored; move its path to a top-level `substrate:`" in out
    assert f'--volume={home}/virgil:/home/' in out
    assert 'elsewhere' not in out.split('retired key')[0]


def test_a_refusal_names_the_per_env_path_to_move(tmp_path, monkeypatch, capsys):
    """Today's live config: every process env carries the path per-env and
    there is no top-level one. The refusal must say what to move, not just
    "add one", and the warning must not just say "remove it"."""
    _setup(tmp_path, monkeypatch,
           project_extra={'features': ['substrate'], 'substrate': '/Users/me/discernment'},
           global_substrate=None)

    with pytest.raises(SystemExit):
        cmd_run(_args())

    out = capsys.readouterr().out
    assert 'per-env `substrate: /Users/me/discernment`' in out
    assert 'Move that path to a top-level `substrate:`' in out


def test_a_global_substrate_saved_by_the_wizard_is_used_on_the_same_launch(
        tmp_path, monkeypatch, capsys, launch_without_docker):
    """The wizard saves the first global path after cmd_run already read its
    config; the launch right after must still see it."""
    import dax
    from dax_creds.init import save_config
    home, repo = _setup(tmp_path, monkeypatch, global_substrate=None)
    unregistered = home / 'new-proc'
    unregistered.mkdir()
    monkeypatch.chdir(unregistered)

    def fake_bootstrap(dax_config, cwd):
        entry = {'dir': str(cwd), 'tenant': 'personal', 'creds': [], 'features': ['substrate']}
        dax_config['projects']['new-proc'] = entry
        dax_config['substrate'] = str(home / 'virgil')
        save_config(dax_config)
        return 'new-proc', entry
    monkeypatch.setattr(dax, '_bootstrap_env', fake_bootstrap)

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=False))

    out = capsys.readouterr().out
    assert 'no substrate is set' not in out
    assert f'--volume={home}/virgil:/home/' in out
