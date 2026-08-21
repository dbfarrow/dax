"""`dax build` must not report success when it didn't succeed.

Found live: `_runcmd` never checked `subprocess.run`'s return code at all,
so a failed `docker build` (e.g. the useradd/UID-1000 collision covered in
test_build.py) still fell through tagging and cleanup to print "Commence to
take over the world..." — the exact opposite of what happened.

Writes Dockerfile/ca.crt into cwd for real, so every test here sandboxes
cwd via monkeypatch.chdir(tmp_path) — never the actual repo checkout.
"""
import argparse

import pytest

import dax
from dax import cmd_build


def _args(clean=False, test_only=False):
    return argparse.Namespace(clean=clean, test_only=test_only)


@pytest.fixture(autouse=True)
def _stub_build_inputs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(dax, '_get_username', lambda: 'testuser')
    monkeypatch.setattr(dax, '_get_dax_passwd', lambda: 'testpass')
    monkeypatch.setattr(dax, '_get_ca_cert_path', lambda: None)


def test_build_failure_aborts_before_tagging_and_the_success_message(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_runcmd(cmd, test_only=False):
        calls.append(cmd)
        return 1 if cmd[:2] == ['docker', 'build'] else 0

    monkeypatch.setattr(dax, '_runcmd', fake_runcmd)

    with pytest.raises(SystemExit):
        cmd_build(_args())

    out = capsys.readouterr().out
    assert 'docker build failed' in out
    assert 'Commence to take over the world' not in out
    assert not any(c[:2] == ['docker', 'tag'] for c in calls)
    # left in place to inspect, not cleaned up on failure
    assert (tmp_path / 'Dockerfile').exists()


def test_tag_failure_also_aborts_before_the_success_message(tmp_path, monkeypatch, capsys):
    def fake_runcmd(cmd, test_only=False):
        return 1 if cmd[:2] == ['docker', 'tag'] else 0

    monkeypatch.setattr(dax, '_runcmd', fake_runcmd)

    with pytest.raises(SystemExit):
        cmd_build(_args())

    out = capsys.readouterr().out
    assert 'docker tag failed' in out
    assert 'Commence to take over the world' not in out


def test_a_failed_docker_rmi_is_tolerated_not_fatal(tmp_path, monkeypatch, capsys):
    """Expected to fail harmlessly on a first-ever build, when there is no
    prior dax:latest tag yet to remove — this must never abort the build."""
    def fake_runcmd(cmd, test_only=False):
        return 1 if cmd[:2] == ['docker', 'rmi'] else 0

    monkeypatch.setattr(dax, '_runcmd', fake_runcmd)

    cmd_build(_args())

    assert 'Commence to take over the world' in capsys.readouterr().out


def test_success_prints_the_message(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dax, '_runcmd', lambda cmd, test_only=False: 0)

    cmd_build(_args())

    assert 'Commence to take over the world' in capsys.readouterr().out
