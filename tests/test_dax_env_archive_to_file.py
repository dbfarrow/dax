"""Archiving one env to a specific file (`dax env archive`'s single-env
branch, which absorbed `dax process export`): what goes into the tarball.
The import side moved to tests/test_dax_env_restore.py with `dax env
restore`, which absorbed `dax process import`.

Never includes the live Claude credential (decision 29; Keychain
grants are per machine — see the credential-isolation decisions this whole
project is built around) and rebuildable dependency directories (the
destination's own env setup regenerates them). substrate/mounts travel as
bare path strings, never their contents.

Follows tests/test_dax_process_cmds.py's fixture style: a fake $HOME per
test, `_q_confirm` defaulted to True via an autouse fixture, `_args(**overrides)`
building the argparse.Namespace `cmd_process` would otherwise build.
"""
import argparse
import tarfile

import pytest

from dax import _write_env_archive
from dax_creds.config import load_dax_config


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text('projects: {}\n')
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.fixture(autouse=True)
def _confirm_proceed(monkeypatch):
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda prompt, default=False: True)


def _export(config, out, name='demo'):
    """What the old `process export` did: one env to one specific file."""
    _write_env_archive(config, name, out)


def _register(config, home, name='demo', tenant='personal', creds=None,
              features=None, mounts=None, substrate=None):
    # save_config, not a bare yaml.dump — config is ruamel's CommentedMap
    # when ruamel is installed (it is, here), and pyyaml's plain dump()
    # doesn't round-trip that representation correctly.
    from dax_creds.init import save_config

    project_dir = home / name
    project_dir.mkdir(exist_ok=True)
    project = {'dir': str(project_dir), 'creds': creds or []}
    if tenant:
        project['tenant'] = tenant
    if features:
        project['features'] = features
    if mounts:
        project['mounts'] = mounts
    if substrate:
        project['substrate'] = substrate
    config.setdefault('projects', {})[name] = project
    save_config(config)
    return project_dir


def _state_tree(home, tenant, name):
    tree = home / '.local' / 'state' / 'dax' / 'tenants' / tenant / name
    tree.mkdir(parents=True)
    return tree


# --- export ------------------------------------------------------------

def test_export_excludes_rebuildable_junk_dirs(tmp_path, home, capsys):
    project_dir = _register(load_dax_config(), home)
    (project_dir / 'src').mkdir()
    (project_dir / 'src' / 'main.py').write_text('code')
    (project_dir / 'node_modules' / 'nested').mkdir(parents=True)
    (project_dir / 'node_modules' / 'nested' / 'big.js').write_text('x' * 500)

    out = tmp_path / 'out.tar.gz'
    _export(load_dax_config(), out)

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert 'project/src/main.py' in names
    assert not any('node_modules' in n for n in names)


def test_export_excludes_live_credential_by_default(tmp_path, home):
    config = load_dax_config()
    project_dir = _register(config, home, features=['claude_tenant_state'])
    tree = _state_tree(home, 'personal', 'demo')
    (tree / '.claude.json').write_text('{}')
    (tree / '.credentials.json').write_text('{"claudeAiOauth": {}}')

    out = tmp_path / 'out.tar.gz'
    _export(load_dax_config(), out)

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert 'state/.claude.json' in names
    assert 'state/.credentials.json' not in names


def test_export_manifest_carries_mounts_as_specs_only_and_no_substrate(tmp_path, home):
    """The substrate is one global setting now (decision 41), so an archive
    doesn't carry a path for it."""
    config = load_dax_config()
    _register(config, home, mounts=['/some/sidecar'], substrate='/some/substrate')

    out = tmp_path / 'out.tar.gz'
    _export(load_dax_config(), out)

    with tarfile.open(out) as tar:
        import json
        manifest = json.loads(tar.extractfile('dax-env-manifest.json').read())
        # only the spec travels — no attempt to package either path's contents
        names = tar.getnames()
    assert manifest['mounts'] == ['/some/sidecar']
    assert 'substrate' not in manifest
    assert not any(n.startswith('sidecar') or 'substrate' in n for n in names)


def test_export_no_state_tree_when_tenant_unset(tmp_path, home):
    config = load_dax_config()
    _register(config, home, tenant=None)

    out = tmp_path / 'out.tar.gz'
    _export(load_dax_config(), out)

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert not any(n.startswith('state') for n in names)


def test_export_unknown_name_refuses(home, capsys):
    with pytest.raises(SystemExit):
        _export(load_dax_config(), 'unused.tar.gz', name='nope')
    assert 'no env named' in capsys.readouterr().out
