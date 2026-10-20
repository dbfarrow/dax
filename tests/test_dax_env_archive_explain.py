"""`dax env archive --explain` (was `dax process archive --explain`) — found live, 2026-10: the user went to
run a backup, realized they no longer remembered what `dax backup` actually
did, and `-h` didn't help enough to feel safe running it. `dax backup
--explain` was built first; this is the archive-side counterpart, since
`archive` is the actual BCP/DR mechanism `dax backup` only looks like a
sibling of by name.

`--explain` must work with zero setup — no ~/.dax.yaml required — and must
touch nothing, matching `dax backup --explain`'s own contract. Both explain
texts also interpolate the real configured values (backup_dir, how many
envs are registered) rather than only describing the setting in the
abstract — the whole point of --explain is feeling safe about what's about
to run against *your* actual config.
"""
import argparse

import yaml

from dax import _env_archive_explain_text, cmd_env


def _args(explain=True):
    return argparse.Namespace(env_command='archive', name=[], explain=explain)


def test_explain_prints_without_requiring_a_config(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))

    cmd_env(_args())

    out = capsys.readouterr().out
    assert out.strip() == _env_archive_explain_text(None).strip()
    assert not (home / '.dax.yaml').exists()
    assert 'No ~/.dax.yaml found' in out


def test_explain_mentions_what_it_captures_and_the_dax_backup_distinction(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))

    cmd_env(_args())

    out = capsys.readouterr().out
    for term in ('backup_dir/daily', 'Claude state tree', 'manifest',
                 'never captures: credentials', 'read back', 'dax backup'):
        assert term in out
    # The flags it used to document are gone (decisions 6, 29, 30), and so is
    # the old command name.
    for gone in ('--include-credential', '--remove', '--dry-run', '--all', 'dax process'):
        assert gone not in out


def test_explain_shows_the_real_configured_backup_dir_and_registered_count(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'backup_dir': '~/backups/dax',
            'projects': {
                'demo': {'dir': str(home / 'demo'), 'tenant': 'acme', 'creds': []},
                'other': {'dir': str(home / 'other'), 'tenant': 'acme', 'creds': []},
            },
        }, f)

    cmd_env(_args())

    out = capsys.readouterr().out
    assert '~/backups/dax' in out
    assert '2 env(s) registered' in out


def test_explain_false_falls_through_to_the_real_dispatch(tmp_path, monkeypatch):
    """Regression guard: omitting --explain must behave exactly as before,
    including still requiring ~/.dax.yaml."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))

    try:
        cmd_env(_args(explain=False))
        assert False, 'expected SystemExit for missing ~/.dax.yaml'
    except SystemExit as e:
        assert e.code == 1


def test_explain_is_archive_specific_other_env_commands_unaffected(tmp_path, monkeypatch):
    """--explain is only wired up for `archive` — a namespace from a
    subcommand that never defined the flag (the normal argparse shape) must
    not be mistaken for it."""
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))

    try:
        cmd_env(argparse.Namespace(env_command='destroy', name=[]))
        assert False, 'expected SystemExit for missing ~/.dax.yaml'
    except SystemExit as e:
        assert e.code == 1
