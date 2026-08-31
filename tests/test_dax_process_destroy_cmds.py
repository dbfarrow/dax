"""Tests for `dax process destroy` (docs/design/VALIDATION.md's `destroy-process`,
item 6): tear down a registered process's directory, Claude state tree, any
derived credential, and its ~/.dax.yaml entry.

Generalized past substrate processes specifically — plain projects (no
`substrate`/`claude_tenant_state`) are valid destroy targets too, since the
cleanup is the same either way.
"""
import argparse
import subprocess

import pytest

from dax import _run_process_destroy
from dax_creds.config import load_dax_config, state_tree_path, _shared_files_manifest_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text('projects: {}\n')
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.fixture(autouse=True)
def _no_running_containers(monkeypatch):
    """Real `docker ps` isn't available in the test sandbox; default every
    candidate to "not running" unless a test overrides this explicitly."""
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: False)


@pytest.fixture(autouse=True)
def _confirm_destroy(monkeypatch):
    """Types DESTROY at the confirmation prompt by default — tests that care
    about declining override this explicitly."""
    monkeypatch.setattr('builtins.input', lambda *a, **k: 'DESTROY')


def _register(home, name, tenant='acme', creds=None, make_dir=True, make_state=True):
    config = load_dax_config()
    process_dir = home / name
    if make_dir:
        process_dir.mkdir(exist_ok=True)
    config.setdefault('projects', {})[name] = {
        'dir': str(process_dir),
        'tenant': tenant,
        'creds': creds if creds is not None else ['claude'],
        'image': 'dax-base',
    }
    from dax_creds.init import save_config
    save_config(config)

    if tenant and make_state:
        state = state_tree_path(tenant, name)
        state.mkdir(parents=True, exist_ok=True)
        (state / '.credentials.json').write_text('{}')

    return process_dir, load_dax_config()


def _args(name=None, dry_run=False):
    return argparse.Namespace(name=name or [], dry_run=dry_run)


def test_destroy_single_name_removes_everything(home, monkeypatch):
    process_dir, config = _register(home, 'demo')
    removed = []
    monkeypatch.setattr('dax_creds.init.run_creds_remove',
                        lambda cfg, cred_name: removed.append(cred_name))

    state = state_tree_path('acme', 'demo')
    manifest = _shared_files_manifest_path('acme', 'demo')
    manifest.write_text('{}')

    _run_process_destroy(config, _args(['demo']))

    assert not process_dir.exists()
    assert not state.exists()
    assert not manifest.exists()
    assert 'demo' not in load_dax_config().get('projects', {})
    assert removed == ['claude-acme-demo']


def test_destroy_multiple_names_in_one_call(home):
    dir_a, config = _register(home, 'proc-a')
    dir_b, config = _register(home, 'proc-b')

    _run_process_destroy(config, _args(['proc-a', 'proc-b']))

    assert not dir_a.exists()
    assert not dir_b.exists()
    projects = load_dax_config().get('projects', {})
    assert 'proc-a' not in projects
    assert 'proc-b' not in projects


def test_no_args_uses_checkbox_picker(home, monkeypatch):
    dir_a, config = _register(home, 'proc-a')
    dir_b, config = _register(home, 'proc-b')

    monkeypatch.setattr('dax_creds.init._q_checkbox', lambda prompt, choices: ['proc-a'])

    _run_process_destroy(config, _args())

    assert not dir_a.exists()
    assert dir_b.exists()
    projects = load_dax_config().get('projects', {})
    assert 'proc-a' not in projects
    assert 'proc-b' in projects


def test_no_args_nothing_selected_changes_nothing(home, monkeypatch):
    process_dir, config = _register(home, 'demo')
    monkeypatch.setattr('dax_creds.init._q_checkbox', lambda prompt, choices: [])

    _run_process_destroy(config, _args())

    assert process_dir.exists()
    assert 'demo' in load_dax_config().get('projects', {})


def test_no_registered_envs_at_all(home, capsys):
    config = load_dax_config()
    _run_process_destroy(config, _args())
    assert 'no registered envs' in capsys.readouterr().out


def test_dry_run_touches_nothing(home, capsys):
    process_dir, config = _register(home, 'demo')

    _run_process_destroy(config, _args(['demo'], dry_run=True))

    out = capsys.readouterr().out
    assert process_dir.name in out  # table shows ~/-relative paths, not the raw absolute one
    assert 'dry run' in out
    assert process_dir.exists()
    assert state_tree_path('acme', 'demo').exists()
    assert 'demo' in load_dax_config().get('projects', {})


def test_declining_confirmation_changes_nothing(home, monkeypatch, capsys):
    process_dir, config = _register(home, 'demo')
    monkeypatch.setattr('builtins.input', lambda *a, **k: 'not destroy')

    _run_process_destroy(config, _args(['demo']))

    assert 'aborted' in capsys.readouterr().out
    assert process_dir.exists()
    assert 'demo' in load_dax_config().get('projects', {})


def test_refuses_if_container_is_running(home, monkeypatch, capsys):
    process_dir, config = _register(home, 'demo')
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: True)

    with pytest.raises(SystemExit):
        _run_process_destroy(config, _args(['demo']))

    out = capsys.readouterr().out
    assert 'stop it first' in out
    assert process_dir.exists()
    assert 'demo' in load_dax_config().get('projects', {})


def test_unknown_name_aborts_before_anything(home, capsys):
    process_dir, config = _register(home, 'demo')

    with pytest.raises(SystemExit):
        _run_process_destroy(config, _args(['demo', 'not-a-real-env']))

    assert 'not registered' in capsys.readouterr().out
    assert process_dir.exists()
    assert 'demo' in load_dax_config().get('projects', {})


def test_uncommitted_git_changes_are_warned_not_blocked(home, capsys):
    process_dir, config = _register(home, 'demo')
    subprocess.run(['git', 'init', '-q'], cwd=process_dir, check=True)
    (process_dir / 'dirty.txt').write_text('uncommitted')

    _run_process_destroy(config, _args(['demo']))

    out = capsys.readouterr().out
    assert 'uncommitted git change' in out
    assert not process_dir.exists()
    assert 'demo' not in load_dax_config().get('projects', {})


def test_already_deleted_directory_still_cleans_up_rest(home):
    process_dir, config = _register(home, 'demo')
    process_dir.rmdir()

    _run_process_destroy(config, _args(['demo']))

    assert not state_tree_path('acme', 'demo').exists()
    assert 'demo' not in load_dax_config().get('projects', {})


def test_no_tenant_skips_state_tree_cleanly(home, capsys):
    process_dir, config = _register(home, 'demo', tenant=None, creds=[], make_state=False)

    _run_process_destroy(config, _args(['demo']))

    assert not process_dir.exists()
    assert 'demo' not in load_dax_config().get('projects', {})


def test_explicit_credential_is_never_auto_removed(home, monkeypatch):
    process_dir, config = _register(home, 'demo', creds=['github-dfarrow'])
    config.setdefault('credentials', {})['github-dfarrow'] = {'provider': 'github'}
    from dax_creds.init import save_config
    save_config(config)
    config = load_dax_config()

    removed = []
    monkeypatch.setattr('dax_creds.init.run_creds_remove',
                        lambda cfg, cred_name: removed.append(cred_name))

    _run_process_destroy(config, _args(['demo']))

    assert removed == []
    assert 'github-dfarrow' in load_dax_config().get('credentials', {})
