"""Tests for `dax env archive` (docs/design/2026-08-22-process-archive-restore.md,
merged with the old `export` by the env-lifecycle consolidation's decision 3).

The primary purpose is BCP/DR, not tidiness: archive snapshots one or more
registered envs into a permanent backup and never touches the env unless
removal is explicitly chosen. Because it's meant to protect *everything on
the machine in one call*, a single bad registry entry (a stale/moved
directory) must never take the rest of the batch down with it — see
test_missing_directory_is_skipped_not_fatal below.

Two layers: `_archive_envs` (the non-interactive core) and
`_run_env_archive` (the wizard that gathers its arguments).
"""
import argparse
import json
import tarfile

import pytest

from dax import (
    ARCHIVE_MANIFEST, _ARCHIVE_ALL, _archive_envs, _run_env_archive,
    _verify_archive, _write_env_archive,
)
from dax_creds.config import load_dax_config, state_tree_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text('projects: {}\n')
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.fixture(autouse=True)
def _no_running_containers(monkeypatch):
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: False)


@pytest.fixture(autouse=True)
def _decline_remove_confirm(monkeypatch):
    """Defaults every confirm to a decline — tests that care about a real
    removal (or a wizard run) override this explicitly."""
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: False)


def _register(home, name, tenant=None, creds=None, make_dir=True):
    config = load_dax_config()
    process_dir = home / name
    if make_dir:
        process_dir.mkdir(exist_ok=True)
        (process_dir / 'notes.txt').write_text('hello\n')
    config.setdefault('projects', {})[name] = {
        'dir': str(process_dir),
        'tenant': tenant,
        'creds': creds if creds is not None else [],
        'image': 'dax-base',
    }
    from dax_creds.init import save_config
    save_config(config)
    return process_dir, load_dax_config()


def _with_backup_dir(config, tmp_path):
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)
    return backup_dir


def _archive_file(backup_dir, name):
    matches = list((backup_dir / 'daily').glob('{}-*.tar.gz'.format(name)))
    assert len(matches) == 1, matches
    return matches[0]


def _script(monkeypatch, confirms=(), select=None, path=None, checkbox=None):
    """Scripted answers for the wizard's prompts, in order."""
    confirms = list(confirms)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: confirms.pop(0))
    if select is not None:
        monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: select)
    if path is not None:
        monkeypatch.setattr('dax_creds.init._q_path', lambda *a, **k: path)
    if checkbox is not None:
        monkeypatch.setattr('dax_creds.init._q_checkbox', lambda prompt, choices: checkbox)
    return confirms


# --- the core: _archive_envs -------------------------------------------------

def test_plain_archive_leaves_the_env_untouched(home, tmp_path):
    process_dir, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)

    _archive_envs(config, ['demo'])

    assert process_dir.is_dir()
    assert 'demo' in load_dax_config()['projects']
    with tarfile.open(_archive_file(backup_dir, 'demo')) as tar:
        names = tar.getnames()
    assert ARCHIVE_MANIFEST in names
    assert 'project' in names


def test_writes_the_new_manifest_name_only(home, tmp_path):
    """Decision 23: new archives carry dax-env-manifest.json, never the
    legacy dax-export-manifest.json."""
    _, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)

    _archive_envs(config, ['demo'])

    with tarfile.open(_archive_file(backup_dir, 'demo')) as tar:
        names = tar.getnames()
    assert 'dax-env-manifest.json' in names
    assert 'dax-export-manifest.json' not in names


def test_credentials_are_never_archived(home, tmp_path):
    """Decision 29: no include-credential option survives. A
    .credentials.json in the state tree never reaches the tarball."""
    _, config = _register(home, 'demo', tenant='acme')
    backup_dir = _with_backup_dir(config, tmp_path)
    state = state_tree_path('acme', 'demo')
    state.mkdir(parents=True)
    (state / '.credentials.json').write_text('{"claudeAiOauth": {}}')
    (state / 'history.jsonl').write_text('{}\n')

    _archive_envs(config, ['demo'])

    with tarfile.open(_archive_file(backup_dir, 'demo')) as tar:
        names = tar.getnames()
    assert 'state/history.jsonl' in names
    assert not any(n.endswith('.credentials.json') for n in names)


def test_multiple_names_in_one_call(home, tmp_path):
    _register(home, 'proc-a')
    _register(home, 'proc-b')
    config = load_dax_config()
    backup_dir = _with_backup_dir(config, tmp_path)

    _archive_envs(config, ['proc-a', 'proc-b'])

    _archive_file(backup_dir, 'proc-a')
    _archive_file(backup_dir, 'proc-b')


def test_specific_file_destination(home, tmp_path):
    _, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    out = tmp_path / 'elsewhere' / 'demo.tar.gz'

    _archive_envs(config, ['demo'], out_path=str(out))

    assert out.is_file()
    assert not backup_dir.exists()


def test_specific_file_rejects_several_envs_or_removal(home, tmp_path):
    _register(home, 'proc-a')
    _register(home, 'proc-b')
    config = load_dax_config()
    out = str(tmp_path / 'x.tar.gz')

    with pytest.raises(ValueError):
        _archive_envs(config, ['proc-a', 'proc-b'], out_path=out)
    with pytest.raises(ValueError):
        _archive_envs(config, ['proc-a'], out_path=out, remove=True)


def test_missing_directory_is_skipped_not_fatal(home, tmp_path, capsys):
    """A stale registry entry (exactly what `dax envs list`'s `!` marker
    flags) must not be able to sink an otherwise-healthy backup run."""
    _register(home, 'good')
    _register(home, 'stale', make_dir=False)
    config = load_dax_config()
    backup_dir = _with_backup_dir(config, tmp_path)

    _archive_envs(config, ['good', 'stale'])

    _archive_file(backup_dir, 'good')
    assert not list((backup_dir / 'daily').glob('stale-*.tar.gz'))
    assert 'skipping' in capsys.readouterr().out


def test_all_targets_missing_reports_and_returns(home, tmp_path, capsys):
    _register(home, 'stale', make_dir=False)
    config = load_dax_config()
    backup_dir = _with_backup_dir(config, tmp_path)

    _archive_envs(config, ['stale'])

    assert not backup_dir.exists()
    assert 'nothing left to archive' in capsys.readouterr().out


def test_remove_tears_down_only_after_confirmation(home, tmp_path, monkeypatch):
    process_dir, config = _register(home, 'demo', tenant='acme', creds=['claude'])
    backup_dir = _with_backup_dir(config, tmp_path)
    state = state_tree_path('acme', 'demo')
    state.mkdir(parents=True, exist_ok=True)

    removed = []
    monkeypatch.setattr('dax_creds.init.run_creds_remove',
                        lambda cfg, cred_name: removed.append(cred_name))
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    _archive_envs(config, ['demo'], remove=True)

    _archive_file(backup_dir, 'demo')
    assert not process_dir.exists()
    assert not state.exists()
    assert 'demo' not in load_dax_config()['projects']
    assert removed == ['claude-acme-demo']


def test_declining_the_remove_confirmation_keeps_the_env(home, tmp_path):
    process_dir, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)

    _archive_envs(config, ['demo'], remove=True)  # decline is the fixture default

    _archive_file(backup_dir, 'demo')
    assert process_dir.is_dir()
    assert 'demo' in load_dax_config()['projects']


def test_remove_refuses_the_whole_batch_if_any_target_is_running(home, tmp_path, monkeypatch):
    process_dir, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: True)

    with pytest.raises(SystemExit):
        _archive_envs(config, ['demo'], remove=True)

    assert not backup_dir.exists()
    assert process_dir.is_dir()


def test_an_archive_that_fails_read_back_blocks_every_removal(home, tmp_path, monkeypatch, capsys):
    """Decision 34: removal deletes the only other copy, so one archive that
    doesn't read back cancels removal for the whole batch."""
    dir_a, _ = _register(home, 'proc-a')
    dir_b, _ = _register(home, 'proc-b')
    config = load_dax_config()
    _with_backup_dir(config, tmp_path)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)
    monkeypatch.setattr('dax._verify_archive',
                        lambda path: 'truncated' if 'proc-b' in str(path) else None)

    _archive_envs(config, ['proc-a', 'proc-b'], remove=True)

    out = capsys.readouterr().out
    assert 'did not read back: truncated' in out
    assert 'nothing removed' in out
    assert dir_a.is_dir() and dir_b.is_dir()
    projects = load_dax_config()['projects']
    assert 'proc-a' in projects and 'proc-b' in projects


def test_unknown_name_exits(home, tmp_path):
    config = load_dax_config()
    _with_backup_dir(config, tmp_path)

    with pytest.raises(SystemExit):
        _archive_envs(config, ['ghost'])


# --- _verify_archive ---------------------------------------------------------

def test_verify_accepts_a_real_archive(home, tmp_path):
    _, config = _register(home, 'demo')
    out = tmp_path / 'demo.tar.gz'
    _write_env_archive(config, 'demo', out)

    assert _verify_archive(out) is None


def test_verify_accepts_a_legacy_manifest_name(tmp_path):
    out = tmp_path / 'old.tar.gz'
    project = tmp_path / 'project'
    project.mkdir()
    manifest = tmp_path / 'dax-export-manifest.json'
    manifest.write_text(json.dumps({'name': 'old'}))
    with tarfile.open(out, 'w:gz') as tar:
        tar.add(manifest, arcname='dax-export-manifest.json')
        tar.add(project, arcname='project')

    assert _verify_archive(out) is None


def test_verify_rejects_a_truncated_file(home, tmp_path):
    _, config = _register(home, 'demo')
    out = tmp_path / 'demo.tar.gz'
    _write_env_archive(config, 'demo', out)
    out.write_bytes(out.read_bytes()[:40])

    assert _verify_archive(out)


def test_verify_rejects_an_archive_without_a_manifest(tmp_path):
    out = tmp_path / 'bad.tar.gz'
    stray = tmp_path / 'stray.txt'
    stray.write_text('x')
    with tarfile.open(out, 'w:gz') as tar:
        tar.add(stray, arcname='stray.txt')

    assert _verify_archive(out) == 'no manifest found'


# --- the wizard: _run_env_archive --------------------------------------------

def _args(name=None):
    return argparse.Namespace(name=name or [], explain=False)


def test_wizard_single_env_to_managed_backups(home, tmp_path, monkeypatch):
    process_dir, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    _script(monkeypatch, confirms=[False, True], select='managed')  # no removal; proceed

    _run_env_archive(config, _args(['demo']))

    _archive_file(backup_dir, 'demo')
    assert process_dir.is_dir()


def test_wizard_summary_is_a_table(home, tmp_path, monkeypatch, capsys):
    _, config = _register(home, 'demo')
    _with_backup_dir(config, tmp_path)
    _script(monkeypatch, confirms=[False, True], select='managed')

    _run_env_archive(config, _args(['demo']))

    lines = capsys.readouterr().out.splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith('field'))
    assert set(lines[header + 1].replace(' ', '')) == {'-'}
    assert any(l.startswith('archive') and 'demo' in l for l in lines[header:])
    assert any(l.startswith('then') and 'nothing else' in l for l in lines[header:])


def test_wizard_single_env_to_a_specific_file(home, tmp_path, monkeypatch):
    _, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    out = tmp_path / 'picked.tar.gz'
    left = _script(monkeypatch, confirms=[True], select='file', path=str(out))  # proceed only

    _run_env_archive(config, _args(['demo']))

    assert out.is_file()
    assert not backup_dir.exists()
    assert left == []  # no removal question for a specific file


def test_wizard_specific_file_given_a_directory_writes_inside_it(home, tmp_path, monkeypatch):
    """Tab completion makes "a directory, then Enter" an easy answer."""
    _, config = _register(home, 'demo')
    target_dir = tmp_path / 'outdir'
    target_dir.mkdir()
    _script(monkeypatch, confirms=[True], select='file', path=str(target_dir) + '/')

    _run_env_archive(config, _args(['demo']))

    written = list(target_dir.glob('demo-*.tar.gz'))
    assert len(written) == 1


def test_wizard_specific_file_declining_an_overwrite_writes_nothing(home, tmp_path, monkeypatch, capsys):
    _, config = _register(home, 'demo')
    out = tmp_path / 'existing.tar.gz'
    out.write_text('an earlier archive')
    _script(monkeypatch, confirms=[False], select='file', path=str(out))  # don't overwrite

    _run_env_archive(config, _args(['demo']))

    assert out.read_text() == 'an earlier archive'
    assert 'nothing archived' in capsys.readouterr().out


def test_wizard_specific_file_confirmed_overwrite_replaces_it(home, tmp_path, monkeypatch):
    _, config = _register(home, 'demo')
    out = tmp_path / 'existing.tar.gz'
    out.write_text('an earlier archive')
    _script(monkeypatch, confirms=[True, True], select='file', path=str(out))  # overwrite; proceed

    _run_env_archive(config, _args(['demo']))

    assert _verify_archive(out) is None


def test_wizard_several_envs_skip_the_destination_question(home, tmp_path, monkeypatch):
    _register(home, 'proc-a')
    _register(home, 'proc-b')
    config = load_dax_config()
    backup_dir = _with_backup_dir(config, tmp_path)

    def _no_select(*a, **k):
        raise AssertionError('several envs always go to the managed backups')
    monkeypatch.setattr('dax_creds.init._q_select', _no_select)
    _script(monkeypatch, confirms=[False, True])

    _run_env_archive(config, _args(['proc-a', 'proc-b']))

    _archive_file(backup_dir, 'proc-a')
    _archive_file(backup_dir, 'proc-b')


def test_wizard_picker_every_env_row_selects_all(home, tmp_path, monkeypatch):
    _register(home, 'proc-a')
    _register(home, 'proc-b')
    config = load_dax_config()
    backup_dir = _with_backup_dir(config, tmp_path)
    _script(monkeypatch, confirms=[False, True], checkbox=[_ARCHIVE_ALL])

    _run_env_archive(config, _args())

    _archive_file(backup_dir, 'proc-a')
    _archive_file(backup_dir, 'proc-b')


def test_wizard_picker_nothing_selected_writes_nothing(home, tmp_path, monkeypatch, capsys):
    _, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    _script(monkeypatch, checkbox=[])

    _run_env_archive(config, _args())

    assert not backup_dir.exists()
    assert 'nothing selected' in capsys.readouterr().out


def test_wizard_declining_the_summary_writes_nothing(home, tmp_path, monkeypatch, capsys):
    _, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    _script(monkeypatch, confirms=[False, False], select='managed')  # no removal; don't proceed

    _run_env_archive(config, _args(['demo']))

    assert not backup_dir.exists()
    assert 'nothing archived' in capsys.readouterr().out


def test_wizard_ctrl_c_writes_nothing(home, tmp_path, monkeypatch, capsys):
    _, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)

    def _cancel(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr('dax_creds.init._q_select', _cancel)

    _run_env_archive(config, _args(['demo']))

    assert not backup_dir.exists()
    assert 'cancelled' in capsys.readouterr().out


def test_wizard_removal_then_confirm_removes(home, tmp_path, monkeypatch):
    process_dir, config = _register(home, 'demo')
    backup_dir = _with_backup_dir(config, tmp_path)
    _script(monkeypatch, confirms=[True, True, True], select='managed')  # remove; proceed; confirm removal

    _run_env_archive(config, _args(['demo']))

    _archive_file(backup_dir, 'demo')
    assert not process_dir.exists()
    assert 'demo' not in load_dax_config()['projects']


def test_wizard_no_registered_envs(home, capsys):
    _run_env_archive(load_dax_config(), _args())
    assert 'no registered envs to archive' in capsys.readouterr().out


# --- the CLI surface ---------------------------------------------------------

def _cli(home, *argv):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(repo / 'dax.py'), *argv],
                          capture_output=True, text=True, env=env, cwd=str(home))


def test_explain_works_through_the_cli_with_no_config(tmp_path):
    home = tmp_path / 'empty-home'
    home.mkdir()

    result = _cli(home, 'env', 'archive', '--explain')

    assert result.returncode == 0
    assert result.stdout.startswith('dax env archive')
    assert not (home / '.dax.yaml').exists()


@pytest.mark.parametrize('flag', ['--all', '--remove', '--include-credential', '--dry-run'])
def test_the_old_archive_flags_are_gone(home, flag):
    result = _cli(home, 'env', 'archive', 'demo', flag)
    assert result.returncode == 2
    assert 'unrecognized arguments: {}'.format(flag) in result.stderr


@pytest.mark.parametrize('sub', ['archive', 'export'])
def test_process_archive_and_export_are_gone(home, sub):
    result = _cli(home, 'process', sub, 'demo')
    assert result.returncode == 2
    assert 'invalid choice' in result.stderr  # `process` itself is gone since step 4
