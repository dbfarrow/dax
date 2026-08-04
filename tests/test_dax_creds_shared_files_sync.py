"""`sync_claude_shared_files` copies the host's user-level CLAUDE.md/settings.json/
settings.local.json into a `claude_tenant_state` env's state tree.

Replaces a nested read-only file bind mount (decision B2), abandoned 2026-07-31
after it turned out not to deliver the host's content at all. A plain byte-diff
between host and tree can't tell "host moved on since last sync" (safe to
overwrite) apart from "something changed the tree's copy independently" (unsafe
to clobber) — that's what the manifest of last-synced hashes is for.
"""
from dax_creds.config import (
    CLAUDE_SHARED_FILES, state_tree_path, sync_claude_shared_files,
)


def _write_host_claude_md(tmp_path, content):
    (tmp_path / '.claude').mkdir(exist_ok=True)
    (tmp_path / '.claude' / 'CLAUDE.md').write_text(content)


def test_first_sync_copies_the_host_file_into_the_tree(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'global instructions')

    warnings = sync_claude_shared_files('personal', 'fabric')

    tree = state_tree_path('personal', 'fabric')
    assert (tree / 'CLAUDE.md').read_text() == 'global instructions'
    assert warnings == []


def test_absent_host_file_is_skipped_without_creating_anything(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))

    warnings = sync_claude_shared_files('personal', 'fabric')

    tree = state_tree_path('personal', 'fabric')
    assert not tree.exists() or list(tree.iterdir()) == []
    assert warnings == []


def test_second_sync_is_a_no_op_when_nothing_changed(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')
    sync_claude_shared_files('personal', 'fabric')

    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    mtime_before = tree_file.stat().st_mtime_ns

    warnings = sync_claude_shared_files('personal', 'fabric')

    assert tree_file.stat().st_mtime_ns == mtime_before
    assert warnings == []


def test_host_update_is_picked_up_when_tree_copy_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')
    sync_claude_shared_files('personal', 'fabric')

    _write_host_claude_md(tmp_path, 'v2')
    warnings = sync_claude_shared_files('personal', 'fabric')

    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    assert tree_file.read_text() == 'v2'
    assert warnings == []


def test_independently_edited_tree_copy_is_left_alone_and_warned(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')
    sync_claude_shared_files('personal', 'fabric')

    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    tree_file.write_text('edited independently in a container')
    _write_host_claude_md(tmp_path, 'v2')

    warnings = sync_claude_shared_files('personal', 'fabric')

    assert tree_file.read_text() == 'edited independently in a container'
    assert warnings == ['CLAUDE.md']


def test_force_overwrites_drifted_tree_copy_with_the_host_version(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')
    sync_claude_shared_files('personal', 'fabric')

    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    tree_file.write_text('edited independently in a container')
    _write_host_claude_md(tmp_path, 'v2')

    warnings = sync_claude_shared_files('personal', 'fabric', force=True)

    assert tree_file.read_text() == 'v2'
    assert warnings == []


def test_force_resyncs_after_a_previous_drift_warning(tmp_path, monkeypatch):
    """Once forced, the manifest reflects the adopted content, so a later
    ordinary sync doesn't immediately re-flag it as drifted."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')
    sync_claude_shared_files('personal', 'fabric')
    (state_tree_path('personal', 'fabric') / 'CLAUDE.md').write_text('drifted')
    sync_claude_shared_files('personal', 'fabric', force=True)

    warnings = sync_claude_shared_files('personal', 'fabric')

    assert warnings == []


def test_manifest_lives_beside_the_tree_not_inside_it(tmp_path, monkeypatch):
    """It must not appear inside the tree the container mounts at ~/.claude."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')

    sync_claude_shared_files('personal', 'fabric')

    tree = state_tree_path('personal', 'fabric')
    assert not any(p.name.endswith('.json') for p in tree.iterdir())
    manifest = tree.parent / 'fabric.shared-files.json'
    assert manifest.is_file()


def test_two_envs_get_independent_manifests(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_host_claude_md(tmp_path, 'v1')
    sync_claude_shared_files('personal', 'fabric')

    (state_tree_path('personal', 'fabric') / 'CLAUDE.md').write_text('drifted for fabric only')
    _write_host_claude_md(tmp_path, 'v2')

    fabric_warnings = sync_claude_shared_files('personal', 'fabric')
    dax_warnings = sync_claude_shared_files('personal', 'dax')

    assert fabric_warnings == ['CLAUDE.md']
    assert dax_warnings == []
    assert (state_tree_path('personal', 'dax') / 'CLAUDE.md').read_text() == 'v2'


def test_all_three_shared_filenames_are_covered(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.claude').mkdir()
    for f in CLAUDE_SHARED_FILES:
        (tmp_path / '.claude' / f).write_text(f'host {f}')

    sync_claude_shared_files('personal', 'fabric')

    tree = state_tree_path('personal', 'fabric')
    for f in CLAUDE_SHARED_FILES:
        assert (tree / f).read_text() == f'host {f}'
