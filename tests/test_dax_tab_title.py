"""`dax run` names the terminal tab after the env for the session, and gives
the tab back to the terminal when the container exits.

Sent from the host, before the container starts: inside it, tmux keeps the
titles its programs set to itself, so a title set from in there never
reaches iTerm2.
"""
import argparse
import io

import yaml

import dax
from dax import _set_tab_title, cmd_run


class _Terminal(io.StringIO):
    def isatty(self):
        return True


def _registered_env(tmp_path, monkeypatch):
    """Registered as 'bitwarden' at ~/fatsec/bitwarden: the env's name in
    ~/.dax.yaml, not 'fatsec-bitwarden' from its path, is the tab's title."""
    home = tmp_path / 'home'
    repo = home / 'fatsec' / 'bitwarden'
    repo.mkdir(parents=True)
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest',
                   'projects': {'bitwarden': {'dir': str(repo), 'creds': [], 'tenant': 'personal'}}}, f)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)


def test_sets_the_tab_title_on_a_terminal(monkeypatch):
    term = _Terminal()
    monkeypatch.setattr(dax.sys, 'stdout', term)

    _set_tab_title('bitwarden')

    assert term.getvalue() == '\033]1;bitwarden\007'


def test_writes_nothing_when_stdout_is_not_a_terminal(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(dax.sys, 'stdout', out)

    _set_tab_title('bitwarden')

    assert out.getvalue() == ''


def test_control_characters_cannot_end_the_sequence_early(monkeypatch):
    term = _Terminal()
    monkeypatch.setattr(dax.sys, 'stdout', term)

    _set_tab_title('a\007b\033c')

    assert term.getvalue() == '\033]1;abc\007'


def test_dax_run_titles_the_tab_for_the_session_then_clears_it(tmp_path, monkeypatch,
                                                                launch_without_docker):
    _registered_env(tmp_path, monkeypatch)
    term = _Terminal()
    monkeypatch.setattr(dax.sys, 'stdout', term)
    seen_at_launch = []
    monkeypatch.setattr(dax.subprocess, 'run',
                        lambda cmd, *a, **k: seen_at_launch.append(term.getvalue()))

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=False))

    assert seen_at_launch[-1].endswith('\033]1;bitwarden\007')
    assert term.getvalue().endswith('\033]1;\007')


def test_dax_run_preview_leaves_the_tab_alone(tmp_path, monkeypatch):
    _registered_env(tmp_path, monkeypatch)
    term = _Terminal()
    monkeypatch.setattr(dax.sys, 'stdout', term)

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=True))

    assert '\033]1;' not in term.getvalue()
