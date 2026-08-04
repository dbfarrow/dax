"""`dax run` must land the container shell at the project mount, not $HOME.

Backlog item: every session started at $HOME, requiring a manual `cd
<project>` before anything useful. workdir is one of the always-on baseline
features (see dax.py's `_ALWAYS_ON_FEATURES`), so the mount path is always
known ahead of time — `-w` just has to point `docker run` at exactly where
`feature_workdir` mounts it.
"""
import argparse
import os

import yaml

from dax import cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def test_dash_w_lands_the_shell_at_the_workdir_mount(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'fabric'
    repo.mkdir()

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': {'fabric': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)

    cmd_run(_args())

    out = capsys.readouterr().out
    user = os.environ.get('USER') or os.environ.get('LOGNAME')
    assert f'-w /home/{user}/fabric' in out
