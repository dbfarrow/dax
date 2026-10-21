"""`sync_claude_shared_files` seeds the host's user-level CLAUDE.md,
settings.json and settings.local.json into an env's state tree, once
(env-lifecycle consolidation, decision 13).

It replaced a three-way drift check (host vs tree vs a last-synced
manifest), whose only trigger was the retired shared `claude` mount. Now each
file is copied only if the tree doesn't have it yet, and the tree's copy is
never touched again. A later host edit not reaching a seeded tree is the
documented, deliberately deferred gap.
"""
from dax_creds.config import (
    CLAUDE_SHARED_FILES, shared_files_manifest_path, state_tree_path,
    sync_claude_shared_files,
)


def _host(tmp_path, name='CLAUDE.md', content='global instructions'):
    (tmp_path / '.claude').mkdir(exist_ok=True)
    (tmp_path / '.claude' / name).write_text(content)


def test_seeds_a_missing_file_into_the_tree(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _host(tmp_path)

    sync_claude_shared_files('personal', 'fabric')

    assert (state_tree_path('personal', 'fabric') / 'CLAUDE.md').read_text() == 'global instructions'


def test_seeds_every_shared_file_present_on_the_host(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    for name in CLAUDE_SHARED_FILES:
        _host(tmp_path, name, 'host ' + name)

    sync_claude_shared_files('personal', 'fabric')

    tree = state_tree_path('personal', 'fabric')
    for name in CLAUDE_SHARED_FILES:
        assert (tree / name).read_text() == 'host ' + name


def test_an_absent_host_file_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))

    sync_claude_shared_files('personal', 'fabric')

    tree = state_tree_path('personal', 'fabric')
    assert not tree.exists() or list(tree.iterdir()) == []


def test_a_seeded_file_is_never_touched_again(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _host(tmp_path, content='v1')
    sync_claude_shared_files('personal', 'fabric')

    _host(tmp_path, content='v2')
    sync_claude_shared_files('personal', 'fabric')

    assert (state_tree_path('personal', 'fabric') / 'CLAUDE.md').read_text() == 'v1'


def test_a_tree_copy_edited_in_a_container_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _host(tmp_path)
    sync_claude_shared_files('personal', 'fabric')
    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    tree_file.write_text('edited in the container')

    sync_claude_shared_files('personal', 'fabric')

    assert tree_file.read_text() == 'edited in the container'


def test_no_manifest_is_written(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _host(tmp_path)

    sync_claude_shared_files('personal', 'fabric')

    assert not shared_files_manifest_path('personal', 'fabric').exists()


def test_two_envs_are_seeded_independently(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _host(tmp_path, content='v1')
    sync_claude_shared_files('personal', 'fabric')
    _host(tmp_path, content='v2')
    sync_claude_shared_files('personal', 'dax')

    assert (state_tree_path('personal', 'fabric') / 'CLAUDE.md').read_text() == 'v1'
    assert (state_tree_path('personal', 'dax') / 'CLAUDE.md').read_text() == 'v2'


def test_an_empty_tree_copy_is_re_seeded(tmp_path, monkeypatch):
    """A 0-byte copy is never real (a failed nested-mount experiment left one
    in a real tree), and with no drift tracking left this is its only repair."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _host(tmp_path)
    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    tree_file.parent.mkdir(parents=True)
    tree_file.write_text('')

    sync_claude_shared_files('personal', 'fabric')

    assert tree_file.read_text() == 'global instructions'
