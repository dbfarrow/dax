"""`config['image']` resolution in `cmd_run`.

Two real, live incidents drove this:

1. A ~/.dax.yaml written entirely in the `defaults: {image: ...}` shape,
   with no bare top-level `image:` key and no promotion of a project's own
   `image:` override, crashed `dax run` outright with a bare KeyError.
2. Fixing that by making a project's own `image:` win over the bare
   top-level key then broke a *real* working setup: `dax init`/`dax process
   new` write `image: dax-base` into every project entry as routine
   scaffolding boilerplate — never a real image (dax build only ever
   produces dax:<version>/dax:latest) and never meant to override anything.
   It was harmless only because that promotion didn't exist yet, so a real
   top-level `image: dax:latest` always won regardless. The instant
   project-specific started winning, every project's inert placeholder
   became load-bearing at once and broke every one of them.

The fix for both: project-specific still wins (matching tenant/mounts/
substrate's existing precedent), but `dax-base`/`dax-base:latest` are
normalized to `dax:latest` wherever they're found — the same rewrite
`cmd_creds_login` already applies for the identical reason.
"""
import argparse

import yaml

from dax import cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def _setup(tmp_path, monkeypatch, project, top_level_image=None, defaults_image=None):
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'myenv'
    repo.mkdir()
    project = dict(project, dir=str(repo), creds=[])

    config = {'projects': {'myenv': project}}
    if top_level_image is not None:
        config['image'] = top_level_image
    if defaults_image is not None:
        config['defaults'] = {'image': defaults_image}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump(config, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)


def test_defaults_shape_with_no_top_level_key_no_longer_crashes(tmp_path, monkeypatch, capsys):
    """The original incident: only `defaults: {image: ...}` set, no bare
    top-level `image:`, project also carries the same placeholder."""
    _setup(tmp_path, monkeypatch, {'image': 'dax-base'}, defaults_image='dax-base')

    cmd_run(_args())  # must not raise

    assert 'dax:latest' in capsys.readouterr().out


def test_stale_project_level_dax_base_no_longer_shadows_a_real_top_level_image(
        tmp_path, monkeypatch, capsys):
    """The regression this exact fix caused and then had to fix again: a
    real top-level `image: dax:latest` plus a project entry still carrying
    the scaffolding placeholder must still resolve to the real image, not
    the placeholder."""
    _setup(tmp_path, monkeypatch, {'image': 'dax-base'}, top_level_image='dax:latest')

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'dax:latest' in out
    assert 'dax-base' not in out


def test_a_genuinely_custom_project_image_is_still_respected(tmp_path, monkeypatch, capsys):
    """Only the literal scaffolding placeholder gets rewritten - a real,
    deliberately-set custom image must never be second-guessed."""
    _setup(tmp_path, monkeypatch, {'image': 'my-custom-image:v3'}, top_level_image='dax:latest')

    cmd_run(_args())

    assert 'my-custom-image:v3' in capsys.readouterr().out


def test_no_project_override_falls_back_to_the_real_top_level_image(tmp_path, monkeypatch, capsys):
    _setup(tmp_path, monkeypatch, {}, top_level_image='dax:latest')

    cmd_run(_args())

    assert 'dax:latest' in capsys.readouterr().out
