"""Changing an env's tenant with `dax config` (decision 44).

The tenant names the env's Claude state tree
(~/.local/state/dax/tenants/<tenant>/<env>/) and its implied credential
(claude-<tenant>-<env>), so after the edit `dax config` offers to move
both: the tree, the Keychain secret (renamed, so it's the same grant and no
new login), and the relogin record. Nothing inside the tree depends on the
tenant. The env's own folder never moves.
"""
import subprocess
from pathlib import Path

import pytest

import dax
from dax_creds.config import login_state_path, record_login, state_tree_path

SERVICE = 'dax-creds'

CONFIG = """\
# my config
projects:
  fabric:
    dir: {home}/src/fabric
    tenant: personal
  other:
    dir: {home}/src/other
    tenant: personal
"""


class FakeKeyring:
    def __init__(self, stored=None):
        self.stored = dict(stored or {})

    def get_password(self, service, name):
        return self.stored.get((service, name))

    def set_password(self, service, name, value):
        self.stored[(service, name)] = value

    def delete_password(self, service, name):
        del self.stored[(service, name)]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.delenv('EDITOR', raising=False)
    monkeypatch.setattr(dax, '_in_dax_container', lambda: False)
    monkeypatch.setattr(dax, '_container_running_or_unknown', lambda name: False)
    (tmp_path / 'src' / 'fabric').mkdir(parents=True)
    (tmp_path / '.dax.yaml').write_text(CONFIG.format(home=tmp_path))
    return tmp_path


@pytest.fixture
def keyring(monkeypatch):
    kr = FakeKeyring({(SERVICE, 'claude-personal-fabric'): 'the-grant'})
    monkeypatch.setattr('dax_creds.init._keyring_module', kr)
    return kr


def _tree(tenant, name='fabric'):
    tree = state_tree_path(tenant, name)
    (tree / 'projects').mkdir(parents=True)
    (tree / '.claude.json').write_text('{"history": true}')
    return tree


def _edit_and_answer(monkeypatch, edit, answer=True):
    def fake_run(cmd, *a, **k):
        path = Path(cmd[-1])
        path.write_text(edit(path.read_text()))
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(dax.subprocess, 'run', fake_run)
    asked = []

    def confirm(prompt, default=False):
        asked.append(prompt)
        return answer
    monkeypatch.setattr('dax_creds.init._q_confirm', confirm)
    return asked


def _retenant_fabric(text):
    return text.replace('/src/fabric\n    tenant: personal', '/src/fabric\n    tenant: acme')


def test_a_tenant_change_moves_the_tree_secret_and_record(home, keyring, monkeypatch, capsys):
    _tree('personal')
    record_login('claude-personal-fabric')
    asked = _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert asked == ["Move them to 'acme'?"]
    assert (state_tree_path('acme', 'fabric') / '.claude.json').read_text() == '{"history": true}'
    assert not state_tree_path('personal', 'fabric').exists()
    assert keyring.stored == {(SERVICE, 'claude-acme-fabric'): 'the-grant'}
    assert login_state_path('claude-acme-fabric').exists()
    assert not login_state_path('claude-personal-fabric').exists()
    out = capsys.readouterr().out
    assert 'same login, nothing to redo' in out


def test_the_old_tenants_folder_is_removed_once_empty(home, keyring, monkeypatch):
    _tree('personal')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert not state_tree_path('personal', 'fabric').parent.exists()


def test_the_old_tenants_folder_stays_while_another_env_uses_it(home, keyring, monkeypatch):
    _tree('personal')
    _tree('personal', 'other')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert state_tree_path('personal', 'other').exists()


def test_the_envs_own_folder_is_untouched(home, keyring, monkeypatch):
    (home / 'src' / 'fabric' / 'code.py').write_text('x')
    _tree('personal')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert (home / 'src' / 'fabric' / 'code.py').read_text() == 'x'


def test_declining_moves_nothing_and_says_how(home, keyring, monkeypatch, capsys):
    _tree('personal')
    _edit_and_answer(monkeypatch, _retenant_fabric, answer=False)

    dax.cmd_config(None)

    assert state_tree_path('personal', 'fabric').exists()
    assert (SERVICE, 'claude-personal-fabric') in keyring.stored
    out = capsys.readouterr().out
    assert 'not moved' in out and 'mv ' in out and 'claude-personal-fabric' in out


def test_a_running_env_is_refused_before_anything_moves(home, keyring, monkeypatch, capsys):
    _tree('personal')
    monkeypatch.setattr(dax, '_container_running_or_unknown', lambda name: True)
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert state_tree_path('personal', 'fabric').exists()
    assert (SERVICE, 'claude-personal-fabric') in keyring.stored
    assert 'is running; stop it first' in capsys.readouterr().out


def test_an_existing_destination_tree_is_refused(home, keyring, monkeypatch, capsys):
    _tree('personal')
    _tree('acme')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert (state_tree_path('personal', 'fabric') / '.claude.json').exists()
    assert (SERVICE, 'claude-personal-fabric') in keyring.stored
    assert 'already exists — nothing moved' in capsys.readouterr().out


def test_an_existing_destination_secret_is_refused(home, keyring, monkeypatch, capsys):
    _tree('personal')
    keyring.stored[(SERVICE, 'claude-acme-fabric')] = 'another-grant'
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert state_tree_path('personal', 'fabric').exists()
    assert keyring.stored[(SERVICE, 'claude-acme-fabric')] == 'another-grant'
    assert 'already has a secret in Keychain' in capsys.readouterr().out


def test_no_secret_yet_just_moves_the_tree(home, monkeypatch, capsys):
    monkeypatch.setattr('dax_creds.init._keyring_module', FakeKeyring())
    _tree('personal')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert state_tree_path('acme', 'fabric').exists()
    assert 'the next `dax run` logs in as claude-acme-fabric' in capsys.readouterr().out


def test_no_tree_yet_still_renames_the_secret(home, keyring, monkeypatch):
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert keyring.stored == {(SERVICE, 'claude-acme-fabric'): 'the-grant'}


def test_other_edits_ask_nothing(home, keyring, monkeypatch):
    asked = _edit_and_answer(monkeypatch, lambda t: t + '    image: dax:latest\n')

    dax.cmd_config(None)

    assert asked == []


def test_a_first_tenant_or_a_new_env_asks_nothing():
    before = b'projects:\n  a: {dir: /a}\n'
    config = {'projects': {'a': {'dir': '/a', 'tenant': 't'},
                           'b': {'dir': '/b', 'tenant': 't'}}}
    assert dax._tenant_changes(before, config) == []


def test_changes_are_found_per_env():
    before = b'projects:\n  a: {tenant: x}\n  b: {tenant: y}\n  c: {tenant: z}\n'
    config = {'projects': {'a': {'tenant': 'x2'}, 'b': {'tenant': 'y'}, 'c': {'tenant': 'z2'}}}
    assert dax._tenant_changes(before, config) == [('a', 'x', 'x2', None), ('c', 'z', 'z2', None)]


# --- review fixes -------------------------------------------------------------------

def test_an_unchanged_yaml_1_1_boolean_is_not_a_change():
    """pyyaml would read the old `on` as True and see a change."""
    before = b'projects:\n  a: {tenant: on}\n'
    assert dax._tenant_changes(before, {'projects': {'a': {'tenant': 'on'}}}) == []


def test_a_numeric_tenant_is_skipped_with_a_warning(capsys):
    before = b'projects:\n  a: {tenant: 2024}\n'
    assert dax._tenant_changes(before, {'projects': {'a': {'tenant': 2025}}}) == []
    assert 'is not a plain name; quote it' in capsys.readouterr().out


def test_the_old_dir_is_checked_for_a_running_container_too(home, keyring, monkeypatch, capsys):
    """The same edit changed dir:, so the running container has the old name."""
    _tree('personal')
    checked = []

    def running(container):
        checked.append(container)
        from dax_creds.init import _container_name_for
        return container == _container_name_for(str(home / 'src' / 'fabric'))
    monkeypatch.setattr(dax, '_container_running_or_unknown', running)
    _edit_and_answer(monkeypatch, lambda t: _retenant_fabric(t).replace(
        '/src/fabric', '/src/fabric2'))

    dax.cmd_config(None)

    assert checked == ['src-fabric']  # the old dir's container, checked first
    assert state_tree_path('personal', 'fabric').exists()
    assert 'is running; stop it first' in capsys.readouterr().out


def test_docker_not_answering_refuses(home, keyring, monkeypatch, capsys):
    _tree('personal')
    monkeypatch.setattr(dax, '_container_running_or_unknown', lambda name: None)
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert state_tree_path('personal', 'fabric').exists()
    assert "couldn't ask docker" in capsys.readouterr().out


def test_container_check_answers(monkeypatch):
    def run(result=None, raises=None):
        def fake(cmd, *a, **k):
            if raises:
                raise raises
            return result
        monkeypatch.setattr(dax.subprocess, 'run', fake)
    run(subprocess.CompletedProcess([], 0, stdout='dax-x\n'))
    assert dax._container_running_or_unknown('dax-x') is True
    run(subprocess.CompletedProcess([], 0, stdout='dax-xy\n'))
    assert dax._container_running_or_unknown('dax-x') is False
    run(raises=subprocess.TimeoutExpired('docker', 10))
    assert dax._container_running_or_unknown('dax-x') is None
    run(raises=FileNotFoundError('docker'))
    assert dax._container_running_or_unknown('dax-x') is False


def test_a_pre_3_manifest_does_not_keep_the_old_tenant_folder(home, keyring, monkeypatch):
    from dax_creds.config import shared_files_manifest_path
    _tree('personal')
    shared_files_manifest_path('personal', 'fabric').write_text('{}')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert not state_tree_path('personal', 'fabric').parent.exists()


def test_a_failed_tree_move_stops_before_keychain(home, keyring, monkeypatch, capsys):
    _tree('personal')
    def boom(src, dst):
        raise OSError('permission denied')
    monkeypatch.setattr(dax.shutil, 'move', boom)
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    out = capsys.readouterr().out
    assert 'moving the state tree failed: permission denied' in out
    assert 'Keychain was not touched' in out
    assert keyring.stored == {(SERVICE, 'claude-personal-fabric'): 'the-grant'}


class FlakyDelete(FakeKeyring):
    def __init__(self, stored, failures):
        super().__init__(stored)
        self.failures = failures

    def delete_password(self, service, name):
        if self.failures:
            self.failures -= 1
            raise RuntimeError('keychain locked')
        super().delete_password(service, name)


def test_a_failed_delete_is_retried_once(home, monkeypatch, capsys):
    kr = FlakyDelete({(SERVICE, 'claude-personal-fabric'): 'g'}, failures=1)
    monkeypatch.setattr('dax_creds.init._keyring_module', kr)
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert kr.stored == {(SERVICE, 'claude-acme-fabric'): 'g'}


def test_a_delete_that_keeps_failing_says_how_to_remove_it(home, monkeypatch, capsys):
    """`dax creds remove` can't: the old name is no longer one dax knows."""
    kr = FlakyDelete({(SERVICE, 'claude-personal-fabric'): 'g'}, failures=2)
    monkeypatch.setattr('dax_creds.init._keyring_module', kr)
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    out = capsys.readouterr().out
    assert 'the same login is now under both names'.lower() in out.lower()
    assert "keyring.delete_password('dax-creds', 'claude-personal-fabric')" in out
    assert 'dax creds remove' not in out


def test_the_by_hand_steps_never_name_dax_creds_remove(home, keyring, monkeypatch, capsys):
    _tree('personal')
    _edit_and_answer(monkeypatch, _retenant_fabric, answer=False)

    dax.cmd_config(None)

    out = capsys.readouterr().out
    assert "keyring.delete_password('dax-creds', 'claude-personal-fabric')" in out
    assert 'dax creds remove' not in out


def test_a_registered_old_entry_is_mentioned(home, keyring, monkeypatch, capsys):
    path = home / '.dax.yaml'
    path.write_text(path.read_text() + 'credentials:\n  claude-personal-fabric: {provider: claude}\n')
    _edit_and_answer(monkeypatch, _retenant_fabric)

    dax.cmd_config(None)

    assert 'is also registered under `credentials:`' in capsys.readouterr().out


def test_a_renamed_env_reports_its_stranded_tree(home, keyring, monkeypatch, capsys):
    _tree('personal')
    _edit_and_answer(monkeypatch, lambda t: t.replace('  fabric:\n', '  fabric2:\n'))

    dax.cmd_config(None)

    out = capsys.readouterr().out
    assert "env 'fabric' is no longer in ~/.dax.yaml, but its Claude state is still at" in out
