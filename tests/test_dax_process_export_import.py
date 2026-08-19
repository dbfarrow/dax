"""`dax process export`/`import`: move a registered env's project directory
and Claude state tree to another machine.

Deliberately excludes the live Claude credential by default (Keychain
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

from dax import _run_process_export, _run_process_import
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


def _export_args(**overrides):
    base = dict(name='demo', out=None, include_credential=False, dry_run=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _import_args(**overrides):
    base = dict(archive=None, dir=None, name=None, tenant=None, dry_run=False)
    base.update(overrides)
    return argparse.Namespace(**base)


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
    _run_process_export(load_dax_config(), _export_args(out=str(out)))

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
    _run_process_export(load_dax_config(), _export_args(out=str(out)))

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert 'state/.claude.json' in names
    assert 'state/.credentials.json' not in names


def test_export_include_credential_carries_it(tmp_path, home):
    config = load_dax_config()
    _register(config, home, features=['claude_tenant_state'])
    tree = _state_tree(home, 'personal', 'demo')
    (tree / '.credentials.json').write_text('{"claudeAiOauth": {}}')

    out = tmp_path / 'out.tar.gz'
    _run_process_export(load_dax_config(), _export_args(out=str(out), include_credential=True))

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert 'state/.credentials.json' in names


def test_export_manifest_carries_mounts_and_substrate_as_specs_only(tmp_path, home):
    config = load_dax_config()
    _register(config, home, mounts=['/some/sidecar'], substrate='/some/substrate')

    out = tmp_path / 'out.tar.gz'
    _run_process_export(load_dax_config(), _export_args(out=str(out)))

    with tarfile.open(out) as tar:
        import json
        manifest = json.loads(tar.extractfile('dax-export-manifest.json').read())
        # only the spec travels — no attempt to package either path's contents
        names = tar.getnames()
    assert manifest['mounts'] == ['/some/sidecar']
    assert manifest['substrate'] == '/some/substrate'
    assert not any(n.startswith('sidecar') or 'substrate' in n for n in names)


def test_export_no_state_tree_when_tenant_unset(tmp_path, home):
    config = load_dax_config()
    _register(config, home, tenant=None)

    out = tmp_path / 'out.tar.gz'
    _run_process_export(load_dax_config(), _export_args(out=str(out)))

    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert not any(n.startswith('state') for n in names)


def test_export_unknown_name_refuses(home, capsys):
    with pytest.raises(SystemExit):
        _run_process_export(load_dax_config(), _export_args(name='nope'))
    assert 'no env named' in capsys.readouterr().out


def test_export_dry_run_writes_nothing(tmp_path, home):
    config = load_dax_config()
    _register(config, home)

    out = tmp_path / 'out.tar.gz'
    _run_process_export(load_dax_config(), _export_args(out=str(out), dry_run=True))

    assert not out.exists()


# --- import ------------------------------------------------------------

def _do_export(tmp_path, home, **register_kwargs):
    config = load_dax_config()
    project_dir = _register(config, home, **register_kwargs)
    (project_dir / 'file.txt').write_text('hello')
    tenant = register_kwargs.get('tenant', 'personal')
    if tenant and 'claude_tenant_state' in (register_kwargs.get('features') or []):
        tree = _state_tree(home, tenant, register_kwargs.get('name', 'demo'))
        (tree / '.claude.json').write_text('{}')
    out = tmp_path / 'export.tar.gz'
    _run_process_export(load_dax_config(), _export_args(
        name=register_kwargs.get('name', 'demo'), out=str(out)))
    return out


def test_import_round_trip_places_files_and_registers(tmp_path, home):
    archive = _do_export(tmp_path, home, features=['claude_tenant_state'])

    dest_home = tmp_path / 'home2'
    (dest_home / '.local' / 'state').mkdir(parents=True)
    (dest_home / '.dax.yaml').write_text('projects: {}\n')

    import os
    old_home = os.environ['HOME']
    os.environ['HOME'] = str(dest_home)
    try:
        dest_dir = dest_home / 'imported'
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(dest_dir)))

        assert (dest_dir / 'file.txt').read_text() == 'hello'
        state_tree = dest_home / '.local' / 'state' / 'dax' / 'tenants' / 'personal' / 'demo'
        assert (state_tree / '.claude.json').exists()
        assert not (state_tree / '.credentials.json').exists()

        config = load_dax_config()
        assert config['projects']['demo']['dir'] == str(dest_dir)
        assert config['projects']['demo']['tenant'] == 'personal'
    finally:
        os.environ['HOME'] = old_home


def test_import_resolves_derived_credential_name_for_the_new_tenant(tmp_path, home, capsys):
    archive = _do_export(tmp_path, home, creds=['claude'])

    dest_home = tmp_path / 'home2'
    dest_home.mkdir()
    (dest_home / '.dax.yaml').write_text('projects: {}\n')

    import os
    old_home = os.environ['HOME']
    os.environ['HOME'] = str(dest_home)
    try:
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(dest_home / 'imported'),
            name='newname', tenant='newtenant'))
    finally:
        os.environ['HOME'] = old_home

    out = capsys.readouterr().out
    assert 'claude-newtenant-newname' in out


def test_import_refuses_name_collision(tmp_path, home, capsys):
    from dax_creds.init import save_config

    archive = _do_export(tmp_path, home)
    config = load_dax_config()
    config['projects']['demo'] = {'dir': str(home / 'existing'), 'creds': []}
    save_config(config)

    with pytest.raises(SystemExit):
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(home / 'imported')))
    assert 'already registered' in capsys.readouterr().out


def test_import_refuses_outside_home(tmp_path, home, capsys):
    # --name avoids colliding with the source's own 'demo' registration —
    # this test is only about the destination-path check, not the name check.
    archive = _do_export(tmp_path, home)

    with pytest.raises(SystemExit):
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(tmp_path / 'outside'), name='different'))
    assert 'not under your home directory' in capsys.readouterr().out


def test_import_refuses_nonempty_destination(tmp_path, home, capsys):
    archive = _do_export(tmp_path, home)
    dest = home / 'imported'
    dest.mkdir()
    (dest / 'preexisting.txt').write_text('already here')

    with pytest.raises(SystemExit):
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(dest), name='different'))
    assert 'not empty' in capsys.readouterr().out


def test_import_refuses_bad_archive(tmp_path, home, capsys):
    bogus = tmp_path / 'bogus.tar.gz'
    with tarfile.open(bogus, 'w:gz') as tar:
        info = tarfile.TarInfo('not-a-manifest.txt')
        import io
        data = b'nope'
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    with pytest.raises(SystemExit):
        _run_process_import(load_dax_config(), _import_args(
            archive=str(bogus), dir=str(home / 'imported')))
    assert 'not a dax export archive' in capsys.readouterr().out


def test_import_requires_tenant_when_archive_had_state_tree(tmp_path, home, capsys):
    # A real export can never actually produce had_state_tree=True with no
    # tenant (export only creates a state tree when tenant is set) — this
    # guard is defensive, for a hand-edited or future-format manifest, so
    # the archive has to be built directly rather than via _run_process_export.
    import io
    import json

    archive = tmp_path / 'no-tenant.tar.gz'
    manifest = json.dumps({
        'name': 'demo', 'tenant': None, 'image': 'dax-base', 'creds': [],
        'features': [], 'mounts': [], 'substrate': None, 'had_state_tree': True,
    }).encode()
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('dax-export-manifest.json')
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    with pytest.raises(SystemExit):
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(home / 'imported'), tenant=None))
    assert 'no tenant was given' in capsys.readouterr().out


def test_import_dry_run_registers_nothing(tmp_path, home):
    archive = _do_export(tmp_path, home)

    dest_home = tmp_path / 'home2'
    dest_home.mkdir()
    (dest_home / '.dax.yaml').write_text('projects: {}\n')

    import os
    old_home = os.environ['HOME']
    os.environ['HOME'] = str(dest_home)
    try:
        dest_dir = dest_home / 'imported'
        _run_process_import(load_dax_config(), _import_args(
            archive=str(archive), dir=str(dest_dir), dry_run=True))
        assert not dest_dir.exists()
        assert load_dax_config().get('projects') == {}
    finally:
        os.environ['HOME'] = old_home
