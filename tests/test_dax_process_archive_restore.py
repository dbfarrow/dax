"""Tests for `dax process archive` (docs/design/2026-08-22-process-archive-restore.md).

The primary purpose is BCP/DR, not tidiness: `archive` snapshots one or more
registered processes into a permanent backup and, unlike `destroy`, never
touches the env unless `--remove` is explicitly passed. Because it's meant
to protect *everything on the machine in one call*, a single bad registry
entry (a stale/moved directory) must never take the rest of the batch down
with it — see test_missing_directory_is_skipped_not_fatal below, which
locks in a fix for exactly that failure mode.
"""
import argparse
import tarfile

import pytest

from dax import _run_process_archive
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
    """Defaults the --remove confirmation to a decline — tests that care
    about a real removal override this explicitly."""
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: False)


def _register(home, name, tenant=None, creds=None, make_dir=True):
    config = load_dax_config()
    process_dir = home / name
    if make_dir:
        process_dir.mkdir(exist_ok=True)
    config.setdefault('projects', {})[name] = {
        'dir': str(process_dir),
        'tenant': tenant,
        'creds': creds if creds is not None else [],
        'image': 'dax-base',
    }
    from dax_creds.init import save_config
    save_config(config)
    return process_dir, load_dax_config()


def _args(name=None, all=False, remove=False, include_credential=False, dry_run=False):
    return argparse.Namespace(
        name=name or [], all=all, remove=remove,
        include_credential=include_credential, dry_run=dry_run)


def _archive_file(backup_dir, name):
    matches = list((backup_dir / 'daily').glob('{}-*.tar.gz'.format(name)))
    assert len(matches) == 1, matches
    return matches[0]


def test_plain_archive_leaves_the_env_untouched(home, tmp_path):
    process_dir, config = _register(home, 'demo')
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(['demo']))

    assert process_dir.is_dir()
    assert 'demo' in load_dax_config()['projects']
    archive = _archive_file(backup_dir, 'demo')
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert 'dax-export-manifest.json' in names
    assert 'project' in names


def test_all_archives_every_registered_env_with_no_picker(home, tmp_path, monkeypatch):
    _register(home, 'proc-a')
    _register(home, 'proc-b')
    config = load_dax_config()
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    def _boom(*a, **k):
        raise AssertionError('picker should not run when --all is passed')
    monkeypatch.setattr('dax_creds.init._q_checkbox', _boom)

    _run_process_archive(config, _args(all=True))

    _archive_file(backup_dir, 'proc-a')
    _archive_file(backup_dir, 'proc-b')


def test_multiple_explicit_names_in_one_call(home, tmp_path):
    _register(home, 'proc-a')
    _register(home, 'proc-b')
    config = load_dax_config()
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(['proc-a', 'proc-b']))

    _archive_file(backup_dir, 'proc-a')
    _archive_file(backup_dir, 'proc-b')


def test_missing_directory_is_skipped_not_fatal(home, tmp_path, capsys):
    """The core regression this session's fix covers: `_write_process_archive`
    used to hard sys.exit(1) the instant one target's directory was
    missing, aborting every other target in the same batch with it. A
    stale registry entry (exactly what `dax envs list`'s `!` marker flags)
    must not be able to sink an otherwise-healthy backup run."""
    _register(home, 'good')
    _register(home, 'stale', make_dir=False)
    config = load_dax_config()
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(all=True))

    _archive_file(backup_dir, 'good')
    assert not list((backup_dir / 'daily').glob('stale-*.tar.gz'))
    assert 'skipping' in capsys.readouterr().out


def test_dry_run_reports_the_same_skip_the_real_run_would_hit(home, tmp_path, capsys):
    """--dry-run must predict the real run's outcome, including which
    targets get skipped — the previous version's directory-existence check
    lived entirely inside the real (non-dry-run) code path, so a clean dry
    run gave no warning that the real run would later abort."""
    _register(home, 'good')
    _register(home, 'stale', make_dir=False)
    config = load_dax_config()
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(all=True, dry_run=True))

    out = capsys.readouterr().out
    assert 'skipping' in out
    assert 'stale' in out
    assert not backup_dir.exists()


def test_all_targets_missing_reports_and_returns(home, tmp_path, capsys):
    _register(home, 'stale', make_dir=False)
    config = load_dax_config()
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(all=True))

    assert not backup_dir.exists()
    assert 'nothing left to archive' in capsys.readouterr().out


def test_remove_tears_down_only_after_confirmation(home, tmp_path, monkeypatch):
    process_dir, config = _register(home, 'demo', tenant='acme', creds=['claude'])
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    state = state_tree_path('acme', 'demo')
    state.mkdir(parents=True, exist_ok=True)

    removed = []
    monkeypatch.setattr('dax_creds.init.run_creds_remove',
                        lambda cfg, cred_name: removed.append(cred_name))
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    _run_process_archive(config, _args(['demo'], remove=True))

    _archive_file(backup_dir, 'demo')
    assert not process_dir.exists()
    assert not state.exists()
    assert 'demo' not in load_dax_config()['projects']
    assert removed == ['claude-acme-demo']


def test_declining_the_remove_confirmation_keeps_the_env(home, tmp_path):
    process_dir, config = _register(home, 'demo')
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(['demo'], remove=True))  # decline is the fixture default

    _archive_file(backup_dir, 'demo')
    assert process_dir.is_dir()
    assert 'demo' in load_dax_config()['projects']


def test_remove_refuses_the_whole_batch_if_any_target_is_running(home, tmp_path, monkeypatch):
    process_dir, config = _register(home, 'demo')
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: True)

    with pytest.raises(SystemExit):
        _run_process_archive(config, _args(['demo'], remove=True))

    assert not backup_dir.exists()
    assert process_dir.is_dir()


def test_dry_run_writes_nothing(home, tmp_path):
    process_dir, config = _register(home, 'demo')
    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)

    _run_process_archive(config, _args(['demo'], dry_run=True))

    assert not backup_dir.exists()
    assert process_dir.is_dir()


def test_unknown_name_exits(home, tmp_path):
    config = load_dax_config()
    config['backup_dir'] = str(tmp_path / 'backup')

    with pytest.raises(SystemExit):
        _run_process_archive(config, _args(['ghost']))
