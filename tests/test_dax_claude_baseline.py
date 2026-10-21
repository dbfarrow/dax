"""Claude is an always-on baseline feature with an implied credential
(env-lifecycle consolidation, decisions 35 and 36), as seen from `dax run`.

- An env whose `features:` and `creds:` mention nothing about Claude still
  gets its state tree mounted and its credential `claude-<tenant>-<env>`.
- The retired per-env names (`claude`, `claude_tenant_state`) and Claude
  entries in `creds:` are ignored wherever they come from (the env's own
  list, a cwd-local .dax.yaml, `-f`), so the baseline is never applied twice,
  and every `dax run` reports envs still carrying them.
- Every env needs a tenant, and an undefined credential is reported by name
  rather than ending in a misleading "needs a tenant".

Replaces test_dax_claude_mount_exclusivity.py: there is no second `claude`
feature left to collide with.
"""
import argparse

import pytest
import yaml

from dax import cmd_run


def _args(features=None):
    return argparse.Namespace(features=features, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch, project_extra=None, others=None, credentials=None):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    repo = home / 'fabric'
    repo.mkdir(exist_ok=True)

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    project.update(project_extra or {})
    projects = {'fabric': project}
    projects.update(others or {})

    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': projects,
                   'credentials': credentials or {}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    return home, repo


def _claude_mounts(out):
    return [l for l in out.split() if l.startswith('--volume=') and l.endswith('/.claude')]


def _fake_daemon(monkeypatch):
    import dax

    class _FakeDaemon:
        def terminate(self):
            pass

        def wait(self):
            pass

    monkeypatch.setattr(dax, '_keyring_importable', lambda: True)
    monkeypatch.setattr(dax, '_find_free_port', lambda: 54321)
    monkeypatch.setattr(dax, '_start_creds_daemon', lambda *a, **k: _FakeDaemon())
    monkeypatch.setattr(dax, '_wait_for_tcp', lambda host, port, timeout=5.0: True)
    monkeypatch.setattr(dax, '_sync_and_clear_claude_credential', lambda *a, **k: None)


def test_an_env_that_mentions_nothing_about_claude_still_gets_it(tmp_path, monkeypatch, capsys):
    home, _ = _setup(tmp_path, monkeypatch)
    _fake_daemon(monkeypatch)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert len(_claude_mounts(out)) == 1
    assert '.local/state/dax/tenants/personal/fabric:' in out
    assert 'CLAUDE_CONFIG_DIR=' in out
    assert 'DAX_CREDS_CLAUDE=claude-personal-fabric' in out


def test_a_real_launch_creates_and_seeds_the_state_tree(tmp_path, monkeypatch, launch_without_docker):
    home, _ = _setup(tmp_path, monkeypatch)
    (home / '.claude').mkdir()
    (home / '.claude' / 'CLAUDE.md').write_text('global')

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=False))

    tree = home / '.local/state/dax/tenants/personal/fabric'
    assert tree.is_dir()
    assert (tree / 'CLAUDE.md').read_text() == 'global'
    assert any(c[:2] == ['docker', 'run'] for c in launch_without_docker)


def test_dash_t_creates_and_seeds_nothing(tmp_path, monkeypatch, capsys):
    """-t is a pure preview: the command still names the tree, but nothing
    is made on disk."""
    home, _ = _setup(tmp_path, monkeypatch)
    (home / '.claude').mkdir()
    (home / '.claude' / 'CLAUDE.md').write_text('global')

    cmd_run(_args())

    assert '.local/state/dax/tenants/personal/fabric:' in capsys.readouterr().out
    assert not (home / '.local/state/dax/tenants').exists()


@pytest.mark.parametrize('listed', [['claude'], ['claude_tenant_state'],
                                    ['claude', 'claude_tenant_state', 'auto_claude']])
def test_old_feature_names_in_an_envs_own_list_apply_claude_once(
        tmp_path, monkeypatch, capsys, listed):
    _setup(tmp_path, monkeypatch, project_extra={'features': listed})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert len(_claude_mounts(out)) == 1
    assert 'adding claude_tenant_state' not in out
    assert 'unknown feature' not in out


def test_old_feature_names_from_a_cwd_local_file_or_dash_f_apply_once(
        tmp_path, monkeypatch, capsys):
    _, repo = _setup(tmp_path, monkeypatch)
    with open(repo / '.dax.yaml', 'w') as f:
        yaml.dump({'features': ['claude_tenant_state', 'claude']}, f)

    cmd_run(_args(features='claude_tenant_state,claude'))

    out = capsys.readouterr().out
    assert len(_claude_mounts(out)) == 1
    assert out.count('adding claude') == 1


def test_a_feature_from_two_sources_is_applied_once(tmp_path, monkeypatch, capsys):
    _, repo = _setup(tmp_path, monkeypatch, project_extra={'features': ['auto_claude']})
    with open(repo / '.dax.yaml', 'w') as f:
        yaml.dump({'features': ['auto_claude']}, f)

    cmd_run(_args(features='auto_claude'))

    assert capsys.readouterr().out.count('adding auto_claude') == 1


def test_envs_still_listing_claude_entries_are_reported(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch,
           project_extra={'features': ['claude_tenant_state'], 'creds': ['claude']},
           others={'ot': {'dir': str(tmp_path / 'ot'), 'tenant': 'ysec',
                          'creds': ['claude-ysec-ot', 'gh']}},
           credentials={'claude-ysec-ot': {'provider': 'claude'}, 'gh': {'provider': 'github'}})

    cmd_run(_args())

    out = capsys.readouterr().out
    assert ("env 'fabric' still lists claude_tenant_state (features), claude (creds) "
            "— no longer needed (Claude is automatic, `mounts:` switches mounts on by "
            "itself, auggie was removed); remove them") in out
    assert "env 'ot' still lists claude-ysec-ot (creds)" in out
    assert 'gh (creds)' not in out


def test_no_report_once_the_entries_are_gone(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'features': ['auto_claude']})

    cmd_run(_args())

    assert 'still lists' not in capsys.readouterr().out


def test_an_env_without_a_tenant_is_refused_before_launch(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'tenant': None})

    with pytest.raises(SystemExit) as excinfo:
        cmd_run(_args())

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "env 'fabric' has no tenant" in out
    assert 'Add `tenant: <tenant>`' in out
    assert 'docker run' not in out


def test_an_undefined_credential_is_named_not_blamed_on_the_tenant(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, project_extra={'creds': ['github-nobody']})

    with pytest.raises(SystemExit) as excinfo:
        cmd_run(_args())

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "lists credential 'github-nobody', which is not defined" in out
    assert 'needs a tenant' not in out


def test_dax_features_marks_claude_always_on(capsys):
    from dax import _print_features
    _print_features()
    out = capsys.readouterr().out
    assert 'claude   (always on)' in out
    assert 'claude_tenant_state' not in out
