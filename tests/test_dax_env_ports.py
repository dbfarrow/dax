"""Stored webpreview ports in dax itself: `dax env ports [--assign]`, the
launch rules, the retired per-folder override, and the env index page
(docs/design/2026-10-23-webpreview-ports.md)."""
import argparse

import pytest
import yaml

import dax
from dax import _run_env_ports, cmd_run, load_config
from dax_creds.config import load_dax_config, unknown_env_keys
from dax_creds.webpreview import index_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: False)
    # The suite runs inside a dax container; these tests are about the host.
    monkeypatch.setattr(dax, '_in_dax_container', lambda: False)
    return home


def _write(home, projects, defaults=None):
    cfg = {'image': 'test/dax:latest', 'projects': projects}
    if defaults:
        cfg['defaults'] = defaults
    (home / '.dax.yaml').write_text(yaml.dump(cfg))


def _env(home, name, port=None, tenant='personal'):
    d = home / name
    d.mkdir(exist_ok=True)
    entry = {'dir': str(d), 'creds': [], 'tenant': tenant}
    if port is not None:
        entry['webpreview'] = {'port': port}
    return entry


def _ports(assign):
    return argparse.Namespace(assign=assign)


def _confirm(monkeypatch, answer=True):
    asked = []
    monkeypatch.setattr('dax_creds.init._q_confirm',
                        lambda prompt, default=False: asked.append(prompt) or answer)
    return asked


# --- the env key ---------------------------------------------------------------------

def test_webpreview_is_a_known_env_key():
    assert unknown_env_keys({'projects': {'a': {'dir': '/a', 'webpreview': {'port': 8001}}}}) == {}


# --- dax env ports --------------------------------------------------------------------

def test_ports_lists_every_env_and_points_at_assign(home, capsys):
    _write(home, {'a': _env(home, 'a', 8123), 'b': _env(home, 'b')})
    _run_env_ports(load_dax_config(), _ports(False))
    out = capsys.readouterr().out
    assert '8123' in out and 'none' in out
    assert 'dax env ports --assign' in out


def test_assign_writes_ports_keeps_prev_and_refreshes_the_page(home, monkeypatch, capsys):
    _write(home, {'a': _env(home, 'a'), 'b': _env(home, 'b', 8500)})
    before = (home / '.dax.yaml').read_text()
    asked = _confirm(monkeypatch)

    _run_env_ports(load_dax_config(), _ports(True))

    projects = load_dax_config()['projects']
    assert 8000 <= projects['a']['webpreview']['port'] < 9000
    assert projects['b']['webpreview']['port'] == 8500
    assert (home / '.dax.yaml.prev').read_text() == before
    assert asked and index_path().is_file()
    assert '>a<' in index_path().read_text()


def test_assign_writes_nothing_when_declined(home, monkeypatch):
    _write(home, {'a': _env(home, 'a')})
    before = (home / '.dax.yaml').read_text()
    _confirm(monkeypatch, answer=False)

    _run_env_ports(load_dax_config(), _ports(True))

    assert (home / '.dax.yaml').read_text() == before
    assert not (home / '.dax.yaml.prev').exists()


def test_assign_with_nothing_to_change_asks_nothing(home, monkeypatch, capsys):
    _write(home, {'a': _env(home, 'a', 8001)})
    asked = _confirm(monkeypatch)
    _run_env_ports(load_dax_config(), _ports(True))
    assert asked == []
    assert 'nothing to change' in capsys.readouterr().out


def test_assign_after_a_base_change_renumbers_and_names_running_envs(home, monkeypatch, capsys):
    _write(home, {'a': _env(home, 'a', 8001)}, defaults={'webpreview_base_port': 20000})
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: True)
    _confirm(monkeypatch)

    _run_env_ports(load_dax_config(), _ports(True))

    assert 20000 <= load_dax_config()['projects']['a']['webpreview']['port'] < 21000
    out = capsys.readouterr().out
    assert 'keeps 8001 until restarted' in out
    assert 'restart these to move them to their new port: a' in out


def test_assign_refuses_inside_a_container(home, monkeypatch, capsys):
    _write(home, {'a': _env(home, 'a')})
    monkeypatch.setattr(dax, '_in_dax_container', lambda: True)
    with pytest.raises(SystemExit):
        _run_env_ports(load_dax_config(), _ports(True))
    assert 'only runs on the host' in capsys.readouterr().out
    assert 'webpreview' not in (home / '.dax.yaml').read_text()


# --- launching -------------------------------------------------------------------------

def _run_args(test_only=True):
    return argparse.Namespace(features=None, ports=None, test_only=test_only)


@pytest.mark.real_webpreview_port
def test_run_uses_the_envs_stored_port(home, monkeypatch, capsys):
    _write(home, {'myproj': _env(home, 'myproj', 8765)})
    monkeypatch.chdir(home / 'myproj')
    monkeypatch.setattr(dax, '_is_port_free', lambda p: True)

    cmd_run(_run_args())

    out = capsys.readouterr().out
    assert '8765:8765' in out and 'DAX_PREVIEW_PORT=8765' in out


@pytest.mark.real_webpreview_port
def test_run_refuses_an_env_with_no_stored_port(home, monkeypatch, capsys):
    _write(home, {'myproj': _env(home, 'myproj')})
    monkeypatch.chdir(home / 'myproj')
    monkeypatch.setattr(dax, '_is_port_free', lambda p: True)

    with pytest.raises(SystemExit):
        cmd_run(_run_args())
    assert 'dax env ports --assign' in capsys.readouterr().out


def test_a_preview_run_does_not_write_the_page(home, monkeypatch):
    _write(home, {'myproj': _env(home, 'myproj', 8765)})
    monkeypatch.chdir(home / 'myproj')
    cmd_run(_run_args(test_only=True))
    assert not index_path().exists()


def test_a_real_run_writes_the_page(home, monkeypatch, launch_without_docker):
    _write(home, {'myproj': _env(home, 'myproj', 8765)})
    monkeypatch.chdir(home / 'myproj')
    cmd_run(_run_args(test_only=False))
    assert 'http://localhost:8765/' in index_path().read_text()


# --- the retired override ------------------------------------------------------------

def test_a_cwd_local_webpreview_block_is_ignored_with_a_warning(home, monkeypatch, capsys):
    _write(home, {'myproj': _env(home, 'myproj', 8765)})
    (home / 'myproj' / '.dax.yaml').write_text('webpreview:\n  port: 9999\n')
    monkeypatch.chdir(home / 'myproj')

    config = load_config()

    assert 'webpreview' not in config
    assert 'is no longer read' in capsys.readouterr().out


def test_a_top_level_webpreview_block_is_ignored_with_a_warning(home, monkeypatch, capsys):
    (home / '.dax.yaml').write_text(yaml.dump({'webpreview': {'port': 9999}, 'projects': {}}))
    (home / 'somewhere').mkdir()
    monkeypatch.chdir(home / 'somewhere')

    config = load_config()

    assert 'webpreview' not in config
    assert '(top level) is no longer read' in capsys.readouterr().out
