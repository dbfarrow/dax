"""`_run_backup` copies configured dotfiles/backup paths into this repo's
own backup/, byte-for-byte comparison so only actually-changed files get
copied. Shared by `dax backup` (verbose=True, the full report) and
`cmd_run` (verbose=False - wired in so an ordinary `dax run` also catches
dotfile drift without being asked, but without spamming "unchanged" for
every dotfile on every ordinary launch).

Stays inside the repo checkout, deliberately - moving it to
~/.local/state/dax/ (tried first, 2026-08, after a real ~/.dax.yaml full of
real client names landed in a diff about to be pushed) turned out to solve
the wrong half of the problem: that path isn't bind-mounted into a
container the way the project's own workdir is, so on dax's own
self-hosted dev loop it would quietly vanish on every container rebuild.
What actually closes the leak is .gitignore, not location - backup/ is
gitignored now, so it can never be `git add`ed regardless of what real
content lands in it. `backup_dir` is still passed explicitly in every test
below regardless, since even the *real* default must never be touched by
a test.
"""
import argparse

import yaml

import dax
from dax import _run_backup, cmd_run


def test_default_backup_dir_is_inside_the_repo_checkout():
    """Confirms the default by reading the source, not by calling it with
    backup_dir=None - the real default must never actually run in a test,
    even one with an empty config where it would technically be a no-op."""
    import inspect
    source = inspect.getsource(_run_backup)
    assert "Path(__file__).parent / 'backup'" in source


def _config(monkeypatch, home, ro=None, rw=None, backup=None):
    monkeypatch.setenv('HOME', str(home))
    return {
        'dotfiles': {'ro': ro or [], 'rw': rw or []},
        'backup': backup or [],
    }


def test_copies_a_changed_file(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.zshrc').write_text('new content')
    backup_dir = tmp_path / 'backup_dest'
    config = _config(monkeypatch, home, ro=['~/.zshrc'])

    updated, unchanged, skipped = _run_backup(config, backup_dir=backup_dir)

    assert updated == ['~/.zshrc']
    assert (backup_dir / '.zshrc').read_text() == 'new content'
    assert 'backed up: ~/.zshrc' in capsys.readouterr().out


def test_skips_a_byte_identical_file(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.zshrc').write_text('same content')
    backup_dir = tmp_path / 'backup_dest'
    backup_dir.mkdir()
    (backup_dir / '.zshrc').write_text('same content')
    config = _config(monkeypatch, home, ro=['~/.zshrc'])

    updated, unchanged, skipped = _run_backup(config, backup_dir=backup_dir)

    assert updated == []
    assert unchanged == ['~/.zshrc']


def test_reports_a_missing_path_as_skipped_without_crashing(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    config = _config(monkeypatch, home, ro=['~/.does-not-exist'])

    updated, unchanged, skipped = _run_backup(config, backup_dir=tmp_path / 'backup_dest')

    assert skipped == ['~/.does-not-exist']


def test_quiet_mode_suppresses_unchanged_and_skipped_but_not_backed_up(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.zshrc').write_text('changed')
    (home / '.vimrc').write_text('same')
    backup_dir = tmp_path / 'backup_dest'
    backup_dir.mkdir()
    (backup_dir / '.vimrc').write_text('same')
    config = _config(monkeypatch, home, ro=['~/.zshrc', '~/.vimrc', '~/.missing'])

    _run_backup(config, verbose=False, backup_dir=backup_dir)

    out = capsys.readouterr().out
    assert 'backed up: ~/.zshrc' in out
    assert 'unchanged' not in out
    assert 'skipped' not in out


def test_directory_entries_recurse_and_compare_per_file(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    (home / '.claude' / 'commands').mkdir(parents=True)
    (home / '.claude' / 'commands' / 'a.md').write_text('a')
    (home / '.claude' / 'commands' / 'b.md').write_text('b')
    backup_dir = tmp_path / 'backup_dest'
    (backup_dir / '.claude' / 'commands').mkdir(parents=True)
    (backup_dir / '.claude' / 'commands' / 'a.md').write_text('a')  # already in sync
    (backup_dir / '.claude' / 'commands' / 'b.md').write_text('stale')  # drifted
    config = _config(monkeypatch, home, backup=['~/.claude/commands/'])

    updated, unchanged, skipped = _run_backup(config, backup_dir=backup_dir)

    assert updated == ['~/.claude/commands/']
    assert (backup_dir / '.claude' / 'commands' / 'b.md').read_text() == 'b'


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def test_cmd_run_calls_backup_check_quietly(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'myproj'
    repo.mkdir()
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': {'myproj': {'dir': str(repo), 'creds': []}}}, f)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)

    calls = []
    monkeypatch.setattr(dax, '_run_backup', lambda config, verbose=True, backup_dir=None: calls.append(verbose))

    cmd_run(_args())

    assert calls == [False]
