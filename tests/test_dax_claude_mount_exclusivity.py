"""`claude` and `claude_tenant_state` both mount ~/.claude, so `dax run` refuses.

Decision B makes the state tree the *replacement* for `feature_claude`'s wholesale
mount, not an addition to it. Left to Docker the collision is still caught — it
rejects a duplicate mount point — but the message names neither feature.

`~/.dax.yaml`'s top-level `features:` key was removed 2026-08 (workdir/dotfiles/
webpreview are now an always-on baseline in code); `claude`/`claude_tenant_state`
are opt-in only per env now, via the project's own `features:` list or `-f` — so
every fixture here puts them there instead of a global list that no longer
carries anything optional.
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
    repo = home / 'fabric'
    repo.mkdir(exist_ok=True)

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    project.update(project_extra or {})

    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest',
                   'claudedir': {'mount': '~/.claude'},
                   'projects': {'fabric': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    return home, repo


def test_both_features_together_is_refused(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'features': ['claude', 'claude_tenant_state']})

    with pytest.raises(SystemExit) as excinfo:
        cmd_run(_args())

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert 'both mount ~/.claude' in out
    assert "this env's own" in out


def test_the_tenant_state_tree_alone_is_fine(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'features': ['claude_tenant_state']})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'both mount' not in out


def test_the_shared_mount_alone_is_still_fine(tmp_path, monkeypatch, capsys):
    """Every env works this way today; opting in is per env and reversible."""
    _setup(tmp_path, monkeypatch, project_extra={'features': ['claude']})

    cmd_run(_args())

    assert 'both mount' not in capsys.readouterr().out


def test_a_command_line_feature_can_collide_too(tmp_path, monkeypatch, capsys):
    """`-f claude_tenant_state` on an env whose own features: has `claude` is the
    most likely way to hit this, so the check must run after argv is merged."""
    _setup(tmp_path, monkeypatch, project_extra={'features': ['claude']})
    args = _args()
    args.features = 'claude_tenant_state'

    with pytest.raises(SystemExit):
        cmd_run(args)

    assert 'both mount ~/.claude' in capsys.readouterr().out


# --- per-env feature opt-in -------------------------------------------------
#
# This was never wired up: features came only from the global `features:` list, a
# `.dax.yaml` in cwd, and `-f`. An env's own `features:` list — which the design
# doc, CLAUDE.md, and the feature's docstring all name as the way to switch
# claude_tenant_state on for one project — was read by nothing, so opting in per
# env was impossible.

def test_an_envs_own_features_list_is_honoured(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'features': ['claude_tenant_state']})

    cmd_run(_args())

    assert 'adding claude_tenant_state' in capsys.readouterr().out


def test_opting_in_per_env_collides_with_a_command_line_feature(
        tmp_path, monkeypatch, capsys):
    """The realistic collision now that there's no global list to inherit
    `claude` from: an env's own features: opts into claude_tenant_state, and
    `-f claude` collides with it at launch time."""
    _setup(tmp_path, monkeypatch, project_extra={'features': ['claude_tenant_state']})
    args = _args()
    args.features = 'claude'

    with pytest.raises(SystemExit):
        cmd_run(args)

    assert 'both mount ~/.claude' in capsys.readouterr().out


def test_a_feature_from_two_sources_is_not_added_twice(tmp_path, monkeypatch, capsys):
    """A duplicate would emit the same mount twice and Docker would reject it.

    A cwd-local .dax.yaml and the project's own features: are two independent
    sources that both land in config['features'] — the realistic way to get a
    duplicate now that there's no global list to double up against."""
    home, repo = _setup(tmp_path, monkeypatch,
                         project_extra={'features': ['claude_tenant_state']})
    with open(repo / '.dax.yaml', 'w') as f:
        yaml.dump({'features': ['claude_tenant_state']}, f)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert out.count('adding claude_tenant_state') == 1


def test_an_env_without_a_tenant_is_refused_before_launch(tmp_path, monkeypatch, capsys):
    """The refusal moved here from the container-side wrapper: with Gate B gone
    there is nothing left to resolve at launch, so this is the only place it can
    be caught — and catching it before the container starts is better anyway."""
    _setup(tmp_path, monkeypatch,
           project_extra={'features': ['claude_tenant_state'], 'tenant': None})

    with pytest.raises(SystemExit) as excinfo:
        cmd_run(_args())

    assert excinfo.value.code != 0
    assert 'dax env set fabric tenant' in capsys.readouterr().out
