"""`auto_claude`: an opt-in feature that skips the login shell entirely,
starting tmux with `claude` as its one window's command instead. Composes
with feature_webpreview's background dax-preview launcher (a baseline
feature, always active) via cmd_run's `_shell_cmd_prefix`/`_final_exec`
split — neither feature clobbers what the other set.
"""
import argparse
import os

import yaml

from dax import cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch, features=None):
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'myproj'
    repo.mkdir()

    project = {'dir': str(repo), 'creds': []}
    if features is not None:
        project['features'] = features
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': {'myproj': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)


def test_without_auto_claude_the_shell_cmd_is_unchanged(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'dax-preview &' in out
    assert 'tmux new-session' not in out
    assert 'exec {}'.format(os.environ.get('SHELL', '/bin/zsh')) in out


def test_auto_claude_runs_tmux_with_claude_as_the_window_command(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, features=['auto_claude'])

    cmd_run(_args())

    out = capsys.readouterr().out
    assert "tmux new-session -n myproj claude" in out


def test_auto_claude_does_not_drop_webpreviews_background_launcher(tmp_path, monkeypatch, capsys):
    """The composition this whole split exists for: webpreview is baseline
    and always runs, so its `dax-preview &` must survive even though
    auto_claude — an opt-in feature running after it — also wants to control
    what happens in the foreground."""
    _setup(tmp_path, monkeypatch, features=['auto_claude'])

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'dax-preview &' in out
    assert 'exec tmux new-session -n myproj claude' in out
