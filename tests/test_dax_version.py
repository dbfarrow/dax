"""Product versioning (decision 42): `dax --version`, the `dax.version`
label `dax build` stamps on the image, and `dax run`'s warning when the
image was built by a different version than the host's dax.

The in-image copy of dax/dax_creds is frozen at build time, so an image
that wasn't rebuilt silently runs old wrapper and credential code; the
warning turns the "needs a rebuild" notes into something dax says itself.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

import dax
from dax import _image_version as real_image_version

REPO = Path(__file__).resolve().parent.parent


def test_version_file_is_semver():
    parts = (REPO / 'VERSION').read_text().split()[0].split('.')
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


def test_dash_dash_version_prints_the_version_file(tmp_path):
    result = subprocess.run([sys.executable, str(REPO / 'dax.py'), '--version'],
                            capture_output=True, text=True,
                            env=dict(os.environ, HOME=str(tmp_path)))
    assert result.returncode == 0
    assert result.stdout.strip() == 'dax ' + (REPO / 'VERSION').read_text().strip()


def test_without_a_version_file_the_installed_package_version_is_used(monkeypatch, tmp_path):
    """In the image dax is a plain pip install, with no VERSION beside it."""
    monkeypatch.setattr(dax, '__file__', str(tmp_path / 'dax.py'))
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '9.9.9')
    assert dax._get_version() == '9.9.9'


def test_build_stamps_the_version_label(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dax, '_get_username', lambda: 'testuser')
    monkeypatch.setattr(dax, '_get_dax_passwd', lambda: 'testpass')
    monkeypatch.setattr(dax, '_get_ca_cert_path', lambda: None)
    monkeypatch.setattr(dax, '_get_version', lambda: '3.1.0')
    calls = []
    monkeypatch.setattr(dax, '_runcmd', lambda cmd, test_only=False: calls.append(cmd) or 0)

    dax.cmd_build(argparse.Namespace(clean=False, test_only=False))

    build = next(c for c in calls if c[:2] == ['docker', 'build'])
    assert build[build.index('--label') + 1] == 'dax.version=3.1.0'
    assert 'dax:3.1.0' in build


# --- reading the label ----------------------------------------------------------

def _docker_says(monkeypatch, returncode=0, stdout='', raises=None):
    def fake_run(cmd, *a, **k):
        if raises:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr='')
    monkeypatch.setattr(dax.subprocess, 'run', fake_run)


def test_the_label_is_read(monkeypatch):
    _docker_says(monkeypatch, stdout='3.0.0\n')
    assert real_image_version('dax:latest') == '3.0.0'


@pytest.mark.parametrize('stdout', ['', '<no value>\n'])
def test_an_unlabelled_image_reads_as_empty(monkeypatch, stdout):
    _docker_says(monkeypatch, stdout=stdout)
    assert real_image_version('dax:latest') == ''


def test_no_such_image_or_no_docker_reads_as_unknown(monkeypatch):
    _docker_says(monkeypatch, returncode=1)
    assert real_image_version('dax:latest') is None
    _docker_says(monkeypatch, raises=FileNotFoundError('docker'))
    assert real_image_version('dax:latest') is None


# --- the warning ------------------------------------------------------------------

@pytest.fixture
def host_is(monkeypatch):
    def _set(host, built):
        monkeypatch.setattr(dax, '_get_version', lambda: host)
        monkeypatch.setattr(dax, '_image_version', lambda image: built)
    return _set


def test_a_matching_image_is_silent(host_is, capsys):
    host_is('3.0.0', '3.0.0')
    dax._warn_if_image_is_stale('dax:latest')
    assert capsys.readouterr().out == ''


def test_a_different_version_is_warned_about(host_is, capsys):
    host_is('3.1.0', '3.0.0')
    dax._warn_if_image_is_stale('dax:latest')
    out = capsys.readouterr().out
    assert 'image dax:latest was built by dax 3.0.0; this is dax 3.1.0' in out
    assert 'dax build' in out


def test_an_unlabelled_dax_image_predates_versioning(host_is, capsys):
    host_is('3.0.0', '')
    dax._warn_if_image_is_stale('dax:latest')
    assert 'predates dax versioning' in capsys.readouterr().out


def test_an_unlabelled_custom_image_is_left_alone(host_is, capsys):
    host_is('3.0.0', '')
    dax._warn_if_image_is_stale('kali:rolling')
    assert capsys.readouterr().out == ''


def test_an_unreadable_image_is_silent(host_is, capsys):
    host_is('3.0.0', None)
    dax._warn_if_image_is_stale('dax:latest')
    assert capsys.readouterr().out == ''


def test_dax_run_checks_the_image_it_will_launch(tmp_path, monkeypatch, capsys):
    import yaml
    home = tmp_path / 'home'
    repo = home / 'fabric'
    repo.mkdir(parents=True)
    (home / '.dax.yaml').write_text(yaml.dump({
        'image': 'dax:latest', 'credentials': {},
        'projects': {'fabric': {'dir': str(repo), 'tenant': 'personal'}}}))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)
    seen = []
    monkeypatch.setattr(dax, '_image_version', lambda image: seen.append(image) or '2.0')
    monkeypatch.setattr(dax, '_get_version', lambda: '3.0.0')

    dax.cmd_run(argparse.Namespace(features=None, ports=None, test_only=True))

    assert seen == ['dax:latest']
    assert 'was built by dax 2.0; this is dax 3.0.0' in capsys.readouterr().out
