"""Tests for `dax process restore` (docs/design/2026-08-22-process-archive-restore.md)
— the recovery half of the BCP/DR mechanism, restoring from `dax process
archive`'s backups.

Explicit name(s)/--all restore non-interactively and in place. Omitting
both drops into an interactive picker — pick one archive, choose in-place
or move restore, done — that additionally offers a "move restore" to a new
name/location per pick. This is the new behavior this session added on top
of the original design doc, so it gets the deepest coverage here (in-place
via the picker, move restore with fresh answers, reprompt-on-collision for
both the new name and the new directory, and one-shot-not-looping).

The picker was originally a loop ("handle several in one sitting without
re-invoking the command"), removed after a real live incident: chaining
several interactive prompts together let a leftover keystroke bleed into
the next prompt and silently resolve it before the user ever saw it. It's
one-shot now — restore the one pick, return to the shell.
"""
import argparse

import pytest

import tarfile

from dax import (
    _RESTORE_CANCEL, _extractall_permissive, _relocate_claude_project_key,
    _run_process_archive, _run_process_restore,
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


def _register(home, name, tenant=None, creds=None, make_dir=True, content='original'):
    config = load_dax_config()
    process_dir = home / name
    if make_dir:
        process_dir.mkdir(exist_ok=True)
        (process_dir / 'f.txt').write_text(content)
    config.setdefault('projects', {})[name] = {
        'dir': str(process_dir),
        'tenant': tenant,
        'creds': creds if creds is not None else [],
        'image': 'dax-base',
    }
    from dax_creds.init import save_config
    save_config(config)
    return process_dir, load_dax_config()


def _archive(home, backup_dir, name, **register_kwargs):
    """Registers `name`, archives it, then deregisters it (so the archive
    is real but the caller starts from a clean/unregistered slate for
    whatever restore scenario it's testing)."""
    process_dir, config = _register(home, name, **register_kwargs)
    config['backup_dir'] = str(backup_dir)
    _run_process_archive(config, argparse.Namespace(
        name=[name], all=False, remove=False, include_credential=False, dry_run=False))
    return process_dir


def _deregister(name):
    config = load_dax_config()
    config.get('projects', {}).pop(name, None)
    from dax_creds.init import save_config
    save_config(config)


def _args(name=None, all=False, dir=None, dry_run=False):
    return argparse.Namespace(name=name or [], all=all, dir=dir, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Non-interactive: explicit name(s) / --all
# ---------------------------------------------------------------------------

def test_restore_unregistered_name_lands_at_the_archived_dir(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')
    _deregister('demo')
    import shutil
    shutil.rmtree(process_dir)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(['demo']))

    assert (process_dir / 'f.txt').read_text() == 'original'
    assert load_dax_config()['projects']['demo']['dir'] == str(process_dir)


def test_dry_run_unregistered_writes_nothing(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')
    _deregister('demo')
    import shutil
    shutil.rmtree(process_dir)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(['demo'], dry_run=True))

    assert not process_dir.exists()
    assert 'demo' not in load_dax_config().get('projects', {})


def test_dry_run_already_registered_does_not_prompt_or_hang(home, tmp_path, monkeypatch, capsys):
    """The core regression this session's fix covers: --dry-run against an
    already-registered name used to call _q_confirm before ever checking
    args.dry_run, which blocks on stdin outside a test (and in any
    non-interactive/scripted invocation)."""
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'demo')  # still registered — never deregistered here

    def _boom(*a, **k):
        raise AssertionError('_q_confirm must not be called during --dry-run')
    monkeypatch.setattr('dax_creds.init._q_confirm', _boom)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(['demo'], dry_run=True))

    assert 'would prompt' in capsys.readouterr().out


def test_restore_over_already_registered_replaces_content_after_confirm(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo', content='archived-version')
    (process_dir / 'f.txt').write_text('changed-after-archiving')
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(['demo']))

    assert (process_dir / 'f.txt').read_text() == 'archived-version'


def test_declining_restore_over_leaves_the_registered_env_untouched(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo', content='archived-version')
    (process_dir / 'f.txt').write_text('still-here')
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: False)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(['demo']))

    assert (process_dir / 'f.txt').read_text() == 'still-here'


def test_all_restores_every_available_archive(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    dir_a = _archive(home, backup_dir, 'proc-a')
    dir_b = _archive(home, backup_dir, 'proc-b')
    for d in (dir_a, dir_b):
        import shutil
        shutil.rmtree(d)
    _deregister('proc-a')
    _deregister('proc-b')

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(all=True))

    assert (dir_a / 'f.txt').exists()
    assert (dir_b / 'f.txt').exists()


def test_unknown_name_exits(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'demo')

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    with pytest.raises(SystemExit):
        _run_process_restore(config, _args(['ghost']))


def test_no_archives_found_returns_cleanly(home, tmp_path, capsys):
    config = load_dax_config()
    config['backup_dir'] = str(tmp_path / 'backup')
    _run_process_restore(config, _args(['anything']))
    assert 'no archives found' in capsys.readouterr().out


def test_dir_relocates_a_single_unregistered_restore(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')
    _deregister('demo')
    import shutil
    shutil.rmtree(process_dir)
    new_dir = home / 'relocated'

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args(['demo'], dir=str(new_dir)))

    assert (new_dir / 'f.txt').read_text() == 'original'
    assert not process_dir.exists()
    assert load_dax_config()['projects']['demo']['dir'] == str(new_dir)


def test_dir_rejected_with_all(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'demo')
    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    with pytest.raises(SystemExit):
        _run_process_restore(config, _args(all=True, dir=str(home / 'x')))


def test_dir_rejected_with_multiple_names(home, tmp_path):
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'proc-a')
    _archive(home, backup_dir, 'proc-b')
    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    with pytest.raises(SystemExit):
        _run_process_restore(config, _args(['proc-a', 'proc-b'], dir=str(home / 'x')))


# ---------------------------------------------------------------------------
# Interactive picker (no name, no --all)
# ---------------------------------------------------------------------------

def test_interactive_cancel_at_top_level_changes_nothing(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'demo')  # still registered, untouched below
    before = load_dax_config()['projects']
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: _RESTORE_CANCEL)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    assert load_dax_config()['projects'] == before


def test_interactive_pick_then_in_place(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')
    _deregister('demo')
    import shutil
    shutil.rmtree(process_dir)

    # Exactly 2 answers, no trailing sentinel: a third _q_select call (i.e.
    # a regression back to looping for another pick) would raise
    # StopIteration and fail this test loudly.
    picks = iter(['demo', 'in_place'])
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: next(picks))

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    assert (process_dir / 'f.txt').read_text() == 'original'
    assert load_dax_config()['projects']['demo']['dir'] == str(process_dir)


def test_interactive_restore_is_one_shot_not_looping(home, tmp_path, monkeypatch):
    """Regression test for the interactive picker no longer looping back to
    the top-level list after completing a restore. Found live, 2026-08-30:
    the picker used to loop ("handle several in one sitting"), and chaining
    several interactive prompts together let a leftover keystroke bleed
    into the next prompt and silently resolve it before the user ever saw
    it. It's one-shot now: exactly 2 `_q_select` calls total (pick, then
    mode), never a third."""
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')
    _deregister('demo')
    import shutil
    shutil.rmtree(process_dir)

    calls = []
    picks = iter(['demo', 'in_place'])

    def _tracked(*a, **k):
        calls.append(a[0] if a else None)
        return next(picks)
    monkeypatch.setattr('dax_creds.init._q_select', _tracked)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    assert len(calls) == 2


def test_interactive_move_restore_to_fresh_name_and_dir(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')  # still registered

    picks = iter(['demo', 'move'])
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: next(picks))
    prompts = iter(['demo-copy', str(home / 'demo-copy')])
    monkeypatch.setattr('dax_creds.init._prompt', lambda *a, **k: next(prompts))
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    projects = load_dax_config()['projects']
    assert 'demo-copy' in projects
    assert projects['demo-copy']['dir'] == str(home / 'demo-copy')
    assert (home / 'demo-copy' / 'f.txt').read_text() == 'original'
    # the original is completely untouched
    assert projects['demo']['dir'] == str(process_dir)
    assert process_dir.is_dir()


def test_interactive_move_restore_reprompts_on_name_collision(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'demo')  # still registered
    _register(home, 'taken')  # a second, unrelated registered env

    picks = iter(['demo', 'move'])
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: next(picks))
    prompts = iter(['taken', 'demo-copy', str(home / 'demo-copy')])
    monkeypatch.setattr('dax_creds.init._prompt', lambda *a, **k: next(prompts))
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    assert 'demo-copy' in load_dax_config()['projects']


def test_interactive_move_restore_cancelled_with_blank_name(home, tmp_path, monkeypatch):
    backup_dir = tmp_path / 'backup'
    _archive(home, backup_dir, 'demo')

    picks = iter(['demo', 'move'])
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: next(picks))
    monkeypatch.setattr('dax_creds.init._prompt', lambda *a, **k: None)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    assert set(load_dax_config()['projects']) == {'demo'}


def test_interactive_cancelling_the_mode_choice_does_nothing(home, tmp_path, monkeypatch):
    """Regression test for a real bug found live on the host:
    `questionary.Choice('Cancel', value=None)` doesn't actually store
    `None` as the value — questionary treats an explicit `None` the same
    as an omitted one and substitutes the choice's title string instead.
    `_q_select`'s mock in the other tests here returns `None` directly
    (bypassing questionary entirely), which is exactly why they never
    caught this: they encoded the same wrong assumption the code did. This
    test drives the real sentinel (`_RESTORE_CANCEL`), which is what a real
    questionary.Choice roundtrips correctly, and confirms cancelling the
    mode choice does nothing at all rather than silently falling into
    move-restore (the actual bug — worse than a crash, since it looked like
    a normal move-restore prompt sequence starting up unbidden)."""
    backup_dir = tmp_path / 'backup'
    process_dir = _archive(home, backup_dir, 'demo')  # still registered

    # Exactly 2 answers: a third _q_select call (a regression back to
    # looping) would raise StopIteration and fail this test loudly.
    picks = iter(['demo', _RESTORE_CANCEL])
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: next(picks))

    def _boom(*a, **k):
        raise AssertionError('_prompt must not be called — cancel should do nothing')
    monkeypatch.setattr('dax_creds.init._prompt', _boom)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    projects = load_dax_config()['projects']
    assert set(projects) == {'demo'}
    assert projects['demo']['dir'] == str(process_dir)


def test_choice_cancel_sentinel_survives_a_real_questionary_choice():
    """Confirms the actual mechanism the fix depends on: a real
    `questionary.Choice(title, value=<sentinel>)` must return that exact
    sentinel, not the title string, for the `is _RESTORE_CANCEL` checks in
    `_run_process_restore_interactive` to ever fire. `value=None` silently
    fails this (see the bug this session found live) — this guards against
    reintroducing it."""
    import questionary
    from dax import _RESTORE_CANCEL

    assert questionary.Choice('Cancel', value=_RESTORE_CANCEL).value is _RESTORE_CANCEL


# ---------------------------------------------------------------------------
# _extractall_permissive — shared by restore and `process import`
# ---------------------------------------------------------------------------

def test_extractall_permissive_falls_back_on_pre_312_python(tmp_path, monkeypatch):
    """Regression test for a real bug found live on the host: `filter='tar'`
    didn't exist as an `extractall()` keyword until Python 3.12 (PEP 706).
    The host runs its own Python 3.9 (this repo's pyproject.toml declares
    `requires-python = ">=3.7"`, so 3.9 is a real supported target, not an
    edge case), while this dev/test sandbox runs a newer Python — every
    prior test run exercised only the 3.12+ path and could never have
    caught this. Forces the fallback by removing `tarfile.tar_filter` (the
    documented feature-detection marker for the whole PEP 706 mechanism)
    rather than trusting `hasattr` alone — this proves the `else` branch
    genuinely extracts, not just that the code parses."""
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'f.txt').write_text('hello')
    tar_path = tmp_path / 'test.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        tar.add(src / 'f.txt', arcname='f.txt')

    monkeypatch.delattr(tarfile, 'tar_filter', raising=True)

    dest = tmp_path / 'dest'
    with tarfile.open(tar_path) as tar:
        _extractall_permissive(tar, dest)

    assert (dest / 'f.txt').read_text() == 'hello'


# ---------------------------------------------------------------------------
# _relocate_claude_project_key — Claude Code's own projects/<key>/ session
# index is keyed by the container's cwd (home + basename), not by env name;
# a restore that changes the destination's basename must rekey it or
# /resume silently finds nothing at the new location.
# ---------------------------------------------------------------------------

def test_relocate_claude_project_key_renames_the_keyed_directory(tmp_path):
    """Regression test for a real bug found live, 2026-08-30:
    move-restoring `fabric` as `denim` copied the directory contents
    correctly, but `/resume` showed no sessions in `denim` — Claude Code
    was looking for `-home-dfarrow-denim` and only `-home-dfarrow-fabric`
    existed in the restored state tree, because the archived
    `projects/<key>/` directory is keyed to the *old* container cwd and
    gets copied verbatim."""
    dest_state = tmp_path / 'state'
    proj = dest_state / 'projects' / '-home-dfarrow-fabric'
    proj.mkdir(parents=True)
    (proj / 'session1.jsonl').write_text('{"fake": "transcript"}')

    _relocate_claude_project_key(dest_state, '/Users/dfarrow/fatsec/fabric',
                                 '/Users/dfarrow/fatsec/denim')

    renamed = dest_state / 'projects' / '-home-dfarrow-denim'
    assert renamed.is_dir()
    assert (renamed / 'session1.jsonl').read_text() == '{"fake": "transcript"}'
    assert not proj.exists()


def test_relocate_claude_project_key_is_a_noop_when_basename_unchanged(tmp_path):
    """The common in-place-restore case (same name, same basename): nothing
    to rekey, so the directory must be left exactly as-is."""
    dest_state = tmp_path / 'state'
    proj = dest_state / 'projects' / '-home-dfarrow-fabric'
    proj.mkdir(parents=True)

    _relocate_claude_project_key(dest_state, '/Users/dfarrow/fatsec/fabric',
                                 '/Users/somewhereelse/fabric')

    assert proj.is_dir()


def test_relocate_claude_project_key_handles_no_projects_dir(tmp_path):
    """No `projects/` directory at all (a state tree that never ran a
    session, or --include-credential-only archives) must not raise."""
    dest_state = tmp_path / 'state'
    dest_state.mkdir()
    _relocate_claude_project_key(dest_state, '/a/fabric', '/a/denim')  # must not raise


def test_move_restore_end_to_end_rekeys_the_project_session_directory(
        home, tmp_path, monkeypatch):
    """Integration-level coverage through the real move-restore path (not
    just the unit-level _relocate_claude_project_key above): archives a
    project whose state tree has a Claude session directory keyed to its
    own container path, move-restores it under a new name, and confirms
    the new state tree's projects/ directory is keyed to the *new* name,
    with the transcript content intact."""
    process_dir, config = _register(home, 'fabric', tenant='acme')
    state = state_tree_path('acme', 'fabric')
    proj = state / 'projects' / '-home-dfarrow-fabric'
    proj.mkdir(parents=True)
    (proj / 'session1.jsonl').write_text('{"fake": "transcript"}')

    backup_dir = tmp_path / 'backup'
    config['backup_dir'] = str(backup_dir)
    _run_process_archive(config, argparse.Namespace(
        name=['fabric'], all=False, remove=False, include_credential=False, dry_run=False))

    picks = iter(['fabric', 'move'])
    monkeypatch.setattr('dax_creds.init._q_select', lambda *a, **k: next(picks))
    prompts = iter(['denim', str(home / 'denim'), 'acme'])
    monkeypatch.setattr('dax_creds.init._prompt', lambda *a, **k: next(prompts))
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    config = load_dax_config()
    config['backup_dir'] = str(backup_dir)
    _run_process_restore(config, _args())

    denim_state = state_tree_path('acme', 'denim')
    renamed = denim_state / 'projects' / '-home-dfarrow-denim'
    assert renamed.is_dir()
    assert (renamed / 'session1.jsonl').read_text() == '{"fake": "transcript"}'
    assert not (denim_state / 'projects' / '-home-dfarrow-fabric').exists()
