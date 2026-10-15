"""`dax run` passes the host's current timezone into the container as
`TZ=<zone>`, unconditionally — like workdir/dotfiles/webpreview, nobody
wants their container defaulting to a different timezone than their own.

Read fresh from `/etc/localtime` at every launch (not baked into the
image), so it tracks the host's actual current setting rather than
whatever it was when the image was built.
"""
import argparse
import os

import pytest
import yaml

from dax import _host_timezone, cmd_run


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def test_host_timezone_reads_the_real_symlink(monkeypatch):
    monkeypatch.setattr(os, 'readlink',
                        lambda path: '/usr/share/zoneinfo/America/New_York')
    assert _host_timezone() == 'America/New_York'


def test_host_timezone_handles_a_nested_zone_name(monkeypatch):
    """Some zones have a slash in the name itself (e.g. America/Argentina/
    Buenos_Aires) — must not be truncated at the first slash after the
    marker."""
    monkeypatch.setattr(
        os, 'readlink',
        lambda path: '/usr/share/zoneinfo/America/Argentina/Buenos_Aires')
    assert _host_timezone() == 'America/Argentina/Buenos_Aires'


def test_host_timezone_none_when_file_is_missing(monkeypatch):
    def _raise(path):
        raise OSError('no such file')
    monkeypatch.setattr(os, 'readlink', _raise)
    assert _host_timezone() is None


def test_host_timezone_none_when_not_a_zoneinfo_path(monkeypatch):
    """A host that doesn't use the symlink convention at all — must not
    guess, just skip setting TZ."""
    monkeypatch.setattr(os, 'readlink', lambda path: '/some/other/path')
    assert _host_timezone() is None


def test_cmd_run_includes_tz_when_detectable(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'fabric'
    repo.mkdir()

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': {'fabric': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr('dax._host_timezone', lambda: 'America/Chicago')

    cmd_run(_args())

    assert '-e TZ=America/Chicago' in capsys.readouterr().out


def test_cmd_run_omits_tz_when_undetectable(tmp_path, monkeypatch, capsys):
    """Must not block the launch, or add a bogus/empty TZ flag, when the
    host's timezone can't be determined."""
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'fabric'
    repo.mkdir()

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'image': 'test/dax:latest', 'projects': {'fabric': project}}, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr('dax._host_timezone', lambda: None)

    cmd_run(_args())

    out = capsys.readouterr().out
    assert 'TZ=' not in out


def test_cmd_run_falls_back_to_configured_default_timezone(tmp_path, monkeypatch, capsys):
    """When the host's timezone can't be detected, `defaults.timezone` in
    ~/.dax.yaml backstops it — the same fallback-value block `defaults.image`
    already uses for a project with no image of its own."""
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'fabric'
    repo.mkdir()

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'image': 'test/dax:latest',
            'defaults': {'timezone': 'America/Chicago'},
            'projects': {'fabric': project},
        }, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr('dax._host_timezone', lambda: None)

    cmd_run(_args())

    assert '-e TZ=America/Chicago' in capsys.readouterr().out


def test_cmd_run_prefers_detected_host_timezone_over_the_configured_default(
        tmp_path, monkeypatch, capsys):
    """The configured default is a backstop, not an override — a real,
    detected host timezone always wins when both are available."""
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'fabric'
    repo.mkdir()

    project = {'dir': str(repo), 'tenant': 'personal', 'creds': []}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({
            'image': 'test/dax:latest',
            'defaults': {'timezone': 'America/Chicago'},
            'projects': {'fabric': project},
        }, f)

    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    monkeypatch.setattr('dax._host_timezone', lambda: 'Europe/Berlin')

    cmd_run(_args())

    out = capsys.readouterr().out
    assert '-e TZ=Europe/Berlin' in out
    assert 'America/Chicago' not in out
