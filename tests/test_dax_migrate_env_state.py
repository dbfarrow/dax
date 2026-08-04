"""Tests for `tools/migrate_env_state.py`'s pure logic.

Imported via importlib since it's a standalone host-only script under `tools/`,
not part of the installed `dax_creds` package. Only `path_to_key`, `discover`,
`filter_history`, and `copy_projects` are covered here — `plan()`/`main()` shell
out to `docker ps` and read `~/.dax.yaml`, and are exercised instead by the
scripted dry runs in docs/testing/2026-07-31-migrate-env-to-state-tree.md.

`home`/`tmp_path` isolation matters doubly here: this script's whole job is
reading a real `~/.claude`, so a leaked real `HOME` wouldn't just risk a
destructive write (the usual worry — see conftest.py) but would also silently
mix real transcript data into test assertions.
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / 'tools' / 'migrate_env_state.py'
_spec = importlib.util.spec_from_file_location('migrate_env_state', _SCRIPT)
migrate_env_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_env_state)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    return tmp_path


def _write_history(home, projects):
    (home / '.claude').mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({'project': p}) for p in projects]
    (home / '.claude' / 'history.jsonl').write_text('\n'.join(lines) + '\n')


def _make_project_dir(home, key, sessions=1):
    d = home / '.claude' / 'projects' / key
    d.mkdir(parents=True, exist_ok=True)
    for i in range(sessions):
        (d / f'session-{i}.jsonl').write_text('{}\n')
    return d


class TestPathToKey:

    def test_mangles_slashes_to_dashes(self):
        assert migrate_env_state.path_to_key('/home/dfarrow/dax') == '-home-dfarrow-dax'

    def test_strips_trailing_slash(self):
        assert migrate_env_state.path_to_key('/home/dfarrow/dax/') == '-home-dfarrow-dax'


class TestDiscover:
    """Against the real shape found in ~/.claude/projects/ on 2026-08-01: a
    process migrating out of a larger host tree leaves its own nested key plus
    ancestor keys shared with whatever else lived under that tree."""

    def test_finds_a_forgotten_legacy_leaf_key(self, home):
        _make_project_dir(home, '-home-dfarrow-self-employment-setup')
        _make_project_dir(home, '-home-dfarrow-work-processes-self-employment-setup')

        leaf_others, ancestors = migrate_env_state.discover(
            'self-employment-setup', ['-home-dfarrow-self-employment-setup'])

        assert leaf_others == ['-home-dfarrow-work-processes-self-employment-setup']
        assert ancestors == {}

    def test_finds_ancestor_keys_of_a_migrated_legacy_key(self, home):
        _make_project_dir(home, '-home-dfarrow')
        _make_project_dir(home, '-home-dfarrow-work')
        _make_project_dir(home, '-home-dfarrow-work-processes')
        _make_project_dir(home, '-home-dfarrow-work-processes-self-employment-setup')

        leaf_others, ancestors = migrate_env_state.discover(
            'self-employment-setup',
            ['-home-dfarrow-self-employment-setup',
             '-home-dfarrow-work-processes-self-employment-setup'])

        assert set(ancestors) == {
            '-home-dfarrow', '-home-dfarrow-work', '-home-dfarrow-work-processes'}
        assert ancestors['-home-dfarrow-work-processes'] == {
            '-home-dfarrow-work-processes-self-employment-setup'}

    def test_no_false_positive_leaf_without_a_dash_boundary(self, home):
        # 'network' ends with the letters "work" but not the dash-delimited
        # suffix '-work' — must not be mistaken for a legacy key of env 'work'.
        _make_project_dir(home, '-home-dfarrow-network')

        leaf_others, _ = migrate_env_state.discover('work', ['-home-dfarrow-work'])

        assert leaf_others == []

    def test_already_requested_keys_are_not_reported_as_forgotten(self, home):
        _make_project_dir(home, '-home-dfarrow-self-employment-setup')

        leaf_others, _ = migrate_env_state.discover(
            'self-employment-setup', ['-home-dfarrow-self-employment-setup'])

        assert leaf_others == []


class TestFilterHistory:

    def test_keeps_only_exact_cwd_matches(self, home):
        _write_history(home, [
            '/home/dfarrow/self-employment-setup',
            '/home/dfarrow/work/processes/self-employment-setup',
            '/home/dfarrow/work',  # ancestor — must never be pulled in
        ])
        tree = home / 'tree'

        migrate_env_state.filter_history(
            ['/home/dfarrow/self-employment-setup',
             '/home/dfarrow/work/processes/self-employment-setup'],
            tree, apply_it=True)

        kept = [json.loads(l)['project']
                for l in (tree / 'history.jsonl').read_text().splitlines()]
        assert set(kept) == {
            '/home/dfarrow/self-employment-setup',
            '/home/dfarrow/work/processes/self-employment-setup',
        }

    def test_dry_run_writes_nothing(self, home):
        _write_history(home, ['/home/dfarrow/dax'])
        tree = home / 'tree'

        migrate_env_state.filter_history(['/home/dfarrow/dax'], tree, apply_it=False)

        assert not (tree / 'history.jsonl').exists()


class TestCopyProjects:

    def test_each_key_lands_in_its_own_directory_not_merged(self, home):
        _make_project_dir(home, '-home-dfarrow-self-employment-setup', sessions=2)
        _make_project_dir(
            home, '-home-dfarrow-work-processes-self-employment-setup', sessions=1)
        tree = home / 'tree'

        migrate_env_state.copy_projects(
            ['-home-dfarrow-self-employment-setup',
             '-home-dfarrow-work-processes-self-employment-setup'],
            tree, apply_it=True, force=False)

        canonical = tree / 'projects' / '-home-dfarrow-self-employment-setup'
        legacy = tree / 'projects' / '-home-dfarrow-work-processes-self-employment-setup'
        assert len(list(canonical.glob('*.jsonl'))) == 2
        assert len(list(legacy.glob('*.jsonl'))) == 1

    def test_dry_run_writes_nothing(self, home):
        _make_project_dir(home, '-home-dfarrow-dax')
        tree = home / 'tree'

        migrate_env_state.copy_projects(
            ['-home-dfarrow-dax'], tree, apply_it=False, force=False)

        assert not tree.exists()

    def test_refuses_to_overwrite_existing_destination_without_force(self, home):
        _make_project_dir(home, '-home-dfarrow-dax')
        tree = home / 'tree'
        dst = tree / 'projects' / '-home-dfarrow-dax'
        dst.mkdir(parents=True)
        (dst / 'stale.jsonl').write_text('old')

        migrate_env_state.copy_projects(
            ['-home-dfarrow-dax'], tree, apply_it=True, force=False)

        assert (dst / 'stale.jsonl').read_text() == 'old'
        assert not (dst / 'session-0.jsonl').exists()

    def test_force_overwrites_existing_destination(self, home):
        _make_project_dir(home, '-home-dfarrow-dax')
        tree = home / 'tree'
        dst = tree / 'projects' / '-home-dfarrow-dax'
        dst.mkdir(parents=True)
        (dst / 'stale.jsonl').write_text('old')

        migrate_env_state.copy_projects(
            ['-home-dfarrow-dax'], tree, apply_it=True, force=True)

        assert not (dst / 'stale.jsonl').exists()
        assert (dst / 'session-0.jsonl').exists()
