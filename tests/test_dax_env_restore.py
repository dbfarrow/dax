"""Tests for `dax env restore` (docs/design/2026-08-22-process-archive-restore.md,
merged with the old `import` by the env-lifecycle consolidation, decisions 3
and 25-33).

The rules under test:
- restore only goes into an empty (`.DS_Store` aside), unregistered cwd, and
  never deletes or overwrites anything — rollback is destroy, then restore;
- the env is named after that folder;
- a renamed restore re-keys every path-keyed Claude record and stops sharing
  an explicitly named Claude credential;
- credentials never come back from an archive;
- archived features dax no longer has are kept, commented out;
- the registry is written with a single save.
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

import dax
from dax import (
    _available_archives, _archive_envs, _extractall_permissive,
    _relocate_claude_project_key, _rename_container_path, _run_env_restore,
    _write_env_archive,
)
from dax_creds.config import load_dax_config, state_tree_path


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text(
        'backup_dir: {}\nprojects: {{}}\n'.format(tmp_path / 'backup'))
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.fixture(autouse=True)
def _no_running_containers(monkeypatch):
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: False)


def _save(config):
    from dax_creds.init import save_config
    save_config(config)


def _register(home, name, tenant=None, creds=None, features=None, content='original'):
    config = load_dax_config()
    env_dir = home / name
    env_dir.mkdir(exist_ok=True)
    (env_dir / 'f.txt').write_text(content)
    entry = {'dir': str(env_dir), 'creds': creds or [], 'image': 'dax-base'}
    if tenant:
        entry['tenant'] = tenant
    if features:
        entry['features'] = features
    config.setdefault('projects', {})[name] = entry
    _save(config)
    return env_dir


def _archive(home, name, **register_kwargs):
    """Register `name`, give it a state tree if it has a tenant, and archive
    it to the managed backups. Returns (env dir, archive path)."""
    env_dir = _register(home, name, **register_kwargs)
    tenant = register_kwargs.get('tenant')
    if tenant:
        tree = state_tree_path(tenant, name)
        session = tree / 'projects' / '-home-dfarrow-{}'.format(name)
        session.mkdir(parents=True)
        (session / 'session1.jsonl').write_text('{"fake": "transcript"}\n')
        (tree / '.claude.json').write_text(json.dumps({'projects': {
            '/home/dfarrow/{}'.format(name): {'hasTrustDialogAccepted': True,
                                              'allowedTools': ['Bash']},
            '/home/dfarrow/other': {'hasTrustDialogAccepted': False},
        }}))
        (tree / 'history.jsonl').write_text(
            json.dumps({'display': 'say hi', 'project': '/home/dfarrow/{}'.format(name)}) + '\n'
            + json.dumps({'display': 'elsewhere', 'project': '/home/dfarrow/other'}) + '\n'
            + json.dumps({'display': 'sub', 'project': '/home/dfarrow/{}/sub'.format(name)}) + '\n')
    config = load_dax_config()
    _archive_envs(config, [name])
    archive = next((Path(config['backup_dir']) / 'daily').glob('{}-*.tar.gz'.format(name)))
    return env_dir, archive


def _destroy(name, tenant=None):
    """What `dax env destroy` leaves behind: nothing."""
    config = load_dax_config()
    entry = config['projects'].pop(name)
    _save(config)
    shutil.rmtree(entry['dir'])
    if tenant:
        shutil.rmtree(state_tree_path(tenant, name), ignore_errors=True)


def _restore(monkeypatch, cwd, source=None, selects=(), confirm=True, path=None,
             tenant='acme'):
    """Run the wizard from `cwd` with scripted answers. Returns the list of
    (prompt, default) pairs each select was shown with. `tenant` answers the
    tenant prompt an archive with no recorded tenant now gets."""
    cwd.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(cwd)
    monkeypatch.setattr('dax._known_tenant_names', lambda: set())
    monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known, **k: tenant)
    selects = list(selects)
    shown = []

    def _select(prompt, choices, default=None):
        shown.append((prompt, default))
        return selects.pop(0)
    monkeypatch.setattr('dax_creds.init._q_select', _select)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: confirm)
    if path is not None:
        monkeypatch.setattr('dax_creds.init._q_path', lambda *a, **k: path)
    _run_env_restore(load_dax_config(), argparse.Namespace(source=source, explain=False))
    return shown


# --- the main paths ------------------------------------------------------

def test_rollback_after_destroy_restores_in_place(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo', tenant='acme')
    _destroy('demo', tenant='acme')

    _restore(monkeypatch, env_dir, source='demo')

    assert (env_dir / 'f.txt').read_text() == 'original'
    entry = load_dax_config()['projects']['demo']
    assert entry['dir'] == str(env_dir) and entry['tenant'] == 'acme'
    assert (state_tree_path('acme', 'demo') / 'projects' / '-home-dfarrow-demo').is_dir()


def test_full_wizard_managed_single_archive_asks_no_date(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo')
    _destroy('demo')

    shown = _restore(monkeypatch, env_dir, selects=['managed', 'demo'])

    assert [p for p, _ in shown] == ['Restore from where?', 'Restore which env?']
    assert (env_dir / 'f.txt').read_text() == 'original'


def test_env_picker_preselects_the_folder_name(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo')
    _archive(home, 'other')
    _destroy('demo')

    shown = _restore(monkeypatch, env_dir, selects=['managed', 'demo'])

    assert ('Restore which env?', 'demo') in shown


def test_an_older_archive_is_reachable(home, monkeypatch):
    """Decision 27: after "break it, then archive it" the newest archive is
    the broken one, so the date list must reach older ones."""
    env_dir, first = _archive(home, 'demo', content='good')
    older = first.with_name('demo-2026-01-01.tar.gz')
    first.rename(older)
    (env_dir / 'f.txt').write_text('broken')
    _archive_envs(load_dax_config(), ['demo'])
    _destroy('demo')

    shown = _restore(monkeypatch, env_dir, source='demo', selects=['2026-01-01'])

    prompt, default = shown[0]
    assert prompt.startswith('Which archive of demo') and default != '2026-01-01'  # newest pre-selected
    assert (env_dir / 'f.txt').read_text() == 'good'


def test_path_shortcut_skips_straight_to_the_summary(home, monkeypatch, tmp_path):
    _register(home, 'demo')
    out = tmp_path / 'demo.tar.gz'
    _write_env_archive(load_dax_config(), 'demo', out)
    _destroy('demo')

    shown = _restore(monkeypatch, home / 'demo', source=str(out))

    assert shown == []
    assert 'demo' in load_dax_config()['projects']


def test_wizard_specific_file(home, monkeypatch, tmp_path):
    _register(home, 'demo')
    out = tmp_path / 'demo.tar.gz'
    _write_env_archive(load_dax_config(), 'demo', out)
    _destroy('demo')

    _restore(monkeypatch, home / 'demo', selects=['file'], path=str(out))

    assert (home / 'demo' / 'f.txt').read_text() == 'original'


def test_a_lone_ds_store_counts_as_empty_and_survives(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo')
    _destroy('demo')
    env_dir.mkdir()
    (env_dir / '.DS_Store').write_bytes(b'finder')

    _restore(monkeypatch, env_dir, source='demo')

    assert (env_dir / 'f.txt').read_text() == 'original'
    assert (env_dir / '.DS_Store').read_bytes() == b'finder'


def test_files_land_directly_in_cwd_not_nested(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo')
    _destroy('demo')

    _restore(monkeypatch, env_dir, source='demo')

    assert not (env_dir / 'project').exists()
    assert (env_dir / 'f.txt').exists()


# --- refusals: nothing touched -------------------------------------------

def _yaml_text(home):
    return (home / '.dax.yaml').read_text()


def test_refuses_a_non_empty_cwd(home, monkeypatch, capsys):
    _archive(home, 'demo')
    target = home / 'full'
    target.mkdir()
    (target / 'mine.txt').write_text('keep me')
    before = _yaml_text(home)

    with pytest.raises(SystemExit):
        _restore(monkeypatch, target, source='demo')

    assert 'not empty' in capsys.readouterr().out
    assert (target / 'mine.txt').read_text() == 'keep me'
    assert _yaml_text(home) == before


def test_refuses_a_registered_cwd_even_if_empty(home, monkeypatch, capsys):
    _archive(home, 'demo')
    registered = home / 'empty-env'
    registered.mkdir()
    config = load_dax_config()
    config['projects']['empty-env'] = {'dir': str(registered), 'creds': []}
    _save(config)
    before = _yaml_text(home)

    with pytest.raises(SystemExit):
        _restore(monkeypatch, registered, source='demo')

    out = capsys.readouterr().out
    assert 'already registered' in out and 'dax env destroy' in out
    assert _yaml_text(home) == before


def test_a_registered_non_empty_cwd_is_reported_as_registered(home, monkeypatch, capsys):
    """Found in the step 3 manual pass: from inside a live env, "not empty"
    was true but less useful than "this is a registered env"."""
    env_dir, _ = _archive(home, 'demo')
    before = _yaml_text(home)

    with pytest.raises(SystemExit):
        _restore(monkeypatch, env_dir, source='demo')

    out = capsys.readouterr().out
    assert "already registered as project 'demo' (and not empty)" in out
    assert 'dax env destroy' in out
    assert (env_dir / 'f.txt').read_text() == 'original'
    assert _yaml_text(home) == before


def test_refuses_a_name_registered_elsewhere(home, monkeypatch, capsys):
    """Decision 25: the folder name is the env name, so ~/tmp/demo can't be
    restored while `demo` lives at ~/demo."""
    _archive(home, 'demo')
    before = _yaml_text(home)

    with pytest.raises(SystemExit):
        _restore(monkeypatch, home / 'tmp' / 'demo', source='demo')

    assert "env named 'demo' is already registered" in capsys.readouterr().out
    assert _yaml_text(home) == before


def test_refuses_leftover_state_under_the_new_name(home, monkeypatch, capsys):
    _archive(home, 'demo', tenant='acme')
    leftover = state_tree_path('acme', 'demo-check')
    leftover.mkdir(parents=True)
    (leftover / 'old.txt').write_text('orphan')

    with pytest.raises(SystemExit):
        _restore(monkeypatch, home / 'demo-check', source='demo')

    assert 'leftover Claude state' in capsys.readouterr().out
    assert (leftover / 'old.txt').read_text() == 'orphan'
    assert 'demo-check' not in load_dax_config()['projects']


def test_refuses_inside_a_registered_env(home, monkeypatch, capsys):
    env_dir, _ = _archive(home, 'demo')

    with pytest.raises(SystemExit):
        _restore(monkeypatch, env_dir / 'sub', source='demo')

    assert 'inside already-registered project' in capsys.readouterr().out


def test_refuses_outside_home(home, monkeypatch, capsys, tmp_path):
    _archive(home, 'demo')

    with pytest.raises(SystemExit):
        _restore(monkeypatch, tmp_path / 'outside', source='demo')

    assert 'not under your home directory' in capsys.readouterr().out


def test_declining_the_summary_changes_nothing(home, monkeypatch, capsys):
    env_dir, _ = _archive(home, 'demo', tenant='acme')
    _destroy('demo', tenant='acme')
    before = _yaml_text(home)

    _restore(monkeypatch, env_dir, source='demo', confirm=False)

    assert 'nothing restored' in capsys.readouterr().out
    assert list(env_dir.iterdir()) == []
    assert not state_tree_path('acme', 'demo').exists()
    assert _yaml_text(home) == before


def test_ctrl_c_changes_nothing(home, monkeypatch, capsys):
    env_dir, _ = _archive(home, 'demo')
    _destroy('demo')
    env_dir.mkdir()
    monkeypatch.chdir(env_dir)

    def _cancel(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr('dax_creds.init._q_select', _cancel)
    _run_env_restore(load_dax_config(), argparse.Namespace(source=None, explain=False))

    assert 'cancelled' in capsys.readouterr().out
    assert list(env_dir.iterdir()) == []


def test_no_archives_found(home, monkeypatch, capsys):
    _restore(monkeypatch, home / 'demo', selects=['managed'])
    assert 'no archives found' in capsys.readouterr().out


def test_unknown_name_shortcut_exits(home, monkeypatch, capsys):
    _archive(home, 'demo')

    with pytest.raises(SystemExit):
        _restore(monkeypatch, home / 'ghost', source='ghost')

    assert "no archive found for 'ghost'" in capsys.readouterr().out


def test_refuses_a_bad_archive(home, monkeypatch, capsys, tmp_path):
    bogus = tmp_path / 'bogus.tar.gz'
    with tarfile.open(bogus, 'w:gz') as tar:
        info = tarfile.TarInfo('not-a-manifest.txt')
        info.size = 4
        tar.addfile(info, io.BytesIO(b'nope'))

    with pytest.raises(SystemExit):
        _restore(monkeypatch, home / 'demo', source=str(bogus))

    assert 'not a dax archive' in capsys.readouterr().out


def test_a_state_tree_archive_with_no_tenant_asks_rather_than_refusing(
        home, monkeypatch, capsys, tmp_path):
    """It used to refuse (nowhere to put the tree); now every env needs a
    tenant anyway, so restore asks for one. Declining at the summary still
    changes nothing."""
    archive = tmp_path / 'no-tenant.tar.gz'
    manifest = json.dumps({'name': 'demo', 'tenant': None, 'had_state_tree': True}).encode()
    with tarfile.open(archive, 'w:gz') as tar:
        info = tarfile.TarInfo('dax-env-manifest.json')
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))

    _restore(monkeypatch, home / 'demo', source=str(archive), confirm=False)

    out = capsys.readouterr().out
    assert 'records no tenant' in out and 'acme' in out
    assert 'demo' not in load_dax_config()['projects']

def test_side_by_side_restore_leaves_the_original_alone(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo', tenant='acme')
    (env_dir / 'f.txt').write_text('live edits')

    _restore(monkeypatch, home / 'demo-check', source='demo')

    assert (env_dir / 'f.txt').read_text() == 'live edits'
    assert (home / 'demo-check' / 'f.txt').read_text() == 'original'
    projects = load_dax_config()['projects']
    assert projects['demo']['dir'] == str(env_dir)
    assert projects['demo-check']['dir'] == str(home / 'demo-check')
    assert state_tree_path('acme', 'demo').is_dir()


def test_renamed_restore_rekeys_all_three_claude_records(home, monkeypatch):
    """Decision 28: the session index, .claude.json's per-project settings,
    and history.jsonl's project fields all follow the new folder name.
    Only exact container paths change — not other projects, not subpaths."""
    _archive(home, 'demo', tenant='acme')

    _restore(monkeypatch, home / 'demo-check', source='demo')

    tree = state_tree_path('acme', 'demo-check')
    assert (tree / 'projects' / '-home-dfarrow-demo-check' / 'session1.jsonl').is_file()
    assert not (tree / 'projects' / '-home-dfarrow-demo').exists()

    keys = json.loads((tree / '.claude.json').read_text())['projects']
    assert keys['/home/dfarrow/demo-check']['allowedTools'] == ['Bash']
    assert '/home/dfarrow/demo' not in keys
    assert '/home/dfarrow/other' in keys

    projects = [json.loads(l)['project'] for l in (tree / 'history.jsonl').read_text().splitlines()]
    assert projects == ['/home/dfarrow/demo-check', '/home/dfarrow/other', '/home/dfarrow/demo/sub']


def test_claude_entries_in_an_archive_are_dropped_on_restore(home, monkeypatch, capsys):
    """Decisions 35-36: the restored env's Claude credential is implied by
    its own folder name, so the archive's Claude entries (a bare `claude`,
    an explicitly named Claude credential, the old feature names) are
    dropped, not carried over. That also stops a renamed copy sharing the
    original's grant (decision 33)."""
    config = load_dax_config()
    config['credentials'] = {
        'claude-acme-ot': {'provider': 'claude'},
        'github-me': {'provider': 'github'},
    }
    _save(config)
    _archive(home, 'ot', tenant='acme', creds=['claude-acme-ot', 'claude', 'github-me'],
             features=['claude_tenant_state', 'claude', 'mounts'])

    _restore(monkeypatch, home / 'ot-check', source='ot')

    entry = load_dax_config()['projects']['ot-check']
    assert list(entry['creds']) == ['github-me']
    assert list(entry['features']) == ['mounts']
    assert '#- claude' not in _yaml_text(home)  # dropped, not kept commented out
    out = capsys.readouterr().out
    assert 'claude-acme-ot-check (its own; logs in fresh)' in out
    assert 'dax creds login claude-acme-ot-check' in out


def test_an_archive_with_no_tenant_asks_for_one(home, monkeypatch, capsys):
    """Every env needs a tenant now; an archive from before that rule may
    not record one."""
    env_dir, _ = _archive(home, 'demo')
    _destroy('demo')

    _restore(monkeypatch, env_dir, source='demo', tenant='newco')

    assert 'records no tenant' in capsys.readouterr().out
    assert load_dax_config()['projects']['demo']['tenant'] == 'newco'


# --- credentials, features, the single save ------------------------------

def _rebuild_with(archive, extra_members):
    """Copy `archive`, adding {arcname: bytes}. Stands in for an old archive
    made before decision 29, e.g. one carrying a credential."""
    rebuilt = archive.with_suffix('.rebuilt')
    with tarfile.open(archive) as src, tarfile.open(rebuilt, 'w:gz') as dst:
        for member in src.getmembers():
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        for arcname, data in extra_members.items():
            info = tarfile.TarInfo(arcname)
            info.size = len(data)
            dst.addfile(info, io.BytesIO(data))
    rebuilt.replace(archive)


def test_an_archived_credential_is_stripped_and_reported(home, monkeypatch, capsys):
    env_dir, archive = _archive(home, 'demo', tenant='acme')
    _rebuild_with(archive, {'state/.credentials.json': b'{"claudeAiOauth": {}}'})
    _destroy('demo', tenant='acme')

    _restore(monkeypatch, env_dir, source='demo')

    tree = state_tree_path('acme', 'demo')
    assert (tree / '.claude.json').exists()
    assert not (tree / '.credentials.json').exists()
    assert 'contained a Claude credential; it was not restored' in capsys.readouterr().out


def test_features_dax_no_longer_has_are_kept_commented_out(home, monkeypatch, capsys):
    """Decision 31, with a name that is not and never was a dax feature."""
    env_dir, _ = _archive(home, 'demo', features=['mounts', 'oldfeature'])
    _destroy('demo')

    _restore(monkeypatch, env_dir, source='demo')

    assert list(load_dax_config()['projects']['demo']['features']) == ['mounts']
    assert '#- oldfeature' in _yaml_text(home)
    assert 'kept commented out: oldfeature' in capsys.readouterr().out


def test_all_unknown_features_leave_an_empty_list_with_the_comments(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo', features=['oldfeature', 'gone'])
    _destroy('demo')

    _restore(monkeypatch, env_dir, source='demo')

    text = _yaml_text(home)
    assert '#- oldfeature' in text and '#- gone' in text
    assert list(load_dax_config()['projects']['demo']['features']) == []


def test_the_commented_features_survive_a_later_save(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo', features=['mounts', 'oldfeature'])
    _destroy('demo')
    _restore(monkeypatch, env_dir, source='demo')

    _save(load_dax_config())

    assert '#- oldfeature' in _yaml_text(home)


def test_the_registry_is_saved_exactly_once(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo', tenant='acme', features=['mounts'])
    _destroy('demo', tenant='acme')
    from dax_creds import init
    real_save = init.save_config
    calls = []
    monkeypatch.setattr('dax_creds.init.save_config',
                        lambda config: calls.append(1) or real_save(config))

    _restore(monkeypatch, env_dir, source='demo')

    assert len(calls) == 1
    entry = load_dax_config()['projects']['demo']
    assert entry['tenant'] == 'acme' and list(entry['features']) == ['mounts']


# --- old archives --------------------------------------------------------

def _relabel_manifest_as_legacy(archive):
    rebuilt = archive.with_suffix('.rebuilt')
    with tarfile.open(archive) as src, tarfile.open(rebuilt, 'w:gz') as dst:
        for member in src.getmembers():
            data = src.extractfile(member) if member.isfile() else None
            if member.name == 'dax-env-manifest.json':
                payload = data.read()
                member.name = 'dax-export-manifest.json'
                data = io.BytesIO(payload)
            dst.addfile(member, data)
    rebuilt.replace(archive)


def test_an_archive_with_the_legacy_manifest_name_restores(home, monkeypatch):
    """Decision 23: every archive already in backup_dir predates the rename
    and exists for disaster recovery."""
    env_dir, archive = _archive(home, 'demo')
    _relabel_manifest_as_legacy(archive)
    _destroy('demo')

    _restore(monkeypatch, env_dir, source='demo')

    assert (env_dir / 'f.txt').read_text() == 'original'


def test_absolute_symlinks_in_the_state_tree_survive(home, monkeypatch):
    """wire.sh links each skill into the tree with absolute targets; the
    strict 'data' extraction filter would reject those."""
    env_dir = _register(home, 'demo', tenant='acme')
    tree = state_tree_path('acme', 'demo')
    (tree / 'rules').mkdir(parents=True)
    (tree / 'rules' / '00-substrate.md').symlink_to('/some/absolute/substrate/00-substrate.md')
    _archive_envs(load_dax_config(), ['demo'])
    _destroy('demo', tenant='acme')

    _restore(monkeypatch, env_dir, source='demo')

    link = state_tree_path('acme', 'demo') / 'rules' / '00-substrate.md'
    assert link.is_symlink()
    assert os.readlink(link) == '/some/absolute/substrate/00-substrate.md'


# --- helpers -------------------------------------------------------------

def test_available_archives_lists_every_date_newest_first(tmp_path):
    daily = tmp_path / 'daily'
    daily.mkdir()
    for f in ('demo-2026-01-01.tar.gz', 'demo-2026-03-01.tar.gz', 'my-env-2026-02-01.tar.gz',
              'junk.txt'):
        (daily / f).write_text('')

    found = _available_archives(tmp_path)

    assert [d for _p, d in found['demo']] == ['2026-03-01', '2026-01-01']
    assert [d for _p, d in found['my-env']] == ['2026-02-01']


@pytest.mark.parametrize('value,expected', [
    ('/home/dfarrow/demo', '/home/dfarrow/new'),
    ('/home/someone/demo', '/home/someone/new'),
    ('/home/dfarrow/demo/sub', '/home/dfarrow/demo/sub'),
    ('/home/dfarrow/demo-old', '/home/dfarrow/demo-old'),
    ('/Users/dfarrow/demo', '/Users/dfarrow/demo'),
    (None, None),
])
def test_rename_container_path_only_touches_exact_container_paths(value, expected):
    assert _rename_container_path(value, 'demo', 'new') == expected


def test_relocate_is_a_noop_when_the_folder_name_is_unchanged(tmp_path):
    tree = tmp_path / 'state'
    (tree / 'projects' / '-home-dfarrow-fabric').mkdir(parents=True)
    (tree / '.claude.json').write_text('{"projects": {"/home/dfarrow/fabric": {}}}')
    before = (tree / '.claude.json').read_text()

    _relocate_claude_project_key(tree, '/Users/dfarrow/fatsec/fabric', '/Users/elsewhere/fabric')

    assert (tree / 'projects' / '-home-dfarrow-fabric').is_dir()
    assert (tree / '.claude.json').read_text() == before


def test_relocate_handles_a_tree_with_none_of_the_records(tmp_path):
    tree = tmp_path / 'state'
    tree.mkdir()
    _relocate_claude_project_key(tree, '/a/fabric', '/a/denim')  # must not raise


def test_relocate_leaves_unparseable_history_lines_alone(tmp_path):
    tree = tmp_path / 'state'
    tree.mkdir()
    (tree / 'history.jsonl').write_text(
        'not json\n' + json.dumps({'project': '/home/dfarrow/fabric'}) + '\n')

    _relocate_claude_project_key(tree, '/a/fabric', '/a/denim')

    lines = (tree / 'history.jsonl').read_text().splitlines()
    assert lines[0] == 'not json'
    assert json.loads(lines[1])['project'] == '/home/dfarrow/denim'


def test_extractall_permissive_falls_back_on_pre_312_python(tmp_path, monkeypatch):
    """Found live on the host: `filter='tar'` didn't exist as an
    `extractall()` keyword until Python 3.12 (PEP 706), and the host runs
    3.9. Forces the fallback by removing the PEP 706 feature marker."""
    src = tmp_path / 'src'
    src.mkdir()
    (src / 'f.txt').write_text('hello')
    tar_path = tmp_path / 'test.tar.gz'
    with tarfile.open(tar_path, 'w:gz') as tar:
        tar.add(src / 'f.txt', arcname='f.txt')

    monkeypatch.delattr(tarfile, 'tar_filter', raising=True)

    dest = tmp_path / 'dest'
    with tarfile.open(tar_path) as tar:
        _extractall_permissive(tar, dest)

    assert (dest / 'f.txt').read_text() == 'hello'


# --- reached through `dax env` / the CLI ---------------------------------

def test_cmd_env_restore_runs_from_an_unregistered_cwd(home, monkeypatch):
    env_dir, _ = _archive(home, 'demo')
    _destroy('demo')
    env_dir.mkdir()
    monkeypatch.chdir(env_dir)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)
    monkeypatch.setattr('dax._known_tenant_names', lambda: set())
    monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known, **k: 'acme')

    dax.cmd_env(argparse.Namespace(env_command='restore', source='demo', explain=False))

    assert 'demo' in load_dax_config()['projects']


def _cli(home, *argv):
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(repo / 'dax.py'), *argv],
                          capture_output=True, text=True, env=env, cwd=str(home))


def test_explain_works_with_no_config(tmp_path):
    home = tmp_path / 'empty-home'
    home.mkdir()

    result = _cli(home, 'env', 'restore', '--explain')

    assert result.returncode == 0
    assert result.stdout.startswith('dax env restore')
    assert 'EMPTY folder' in result.stdout
    assert not (home / '.dax.yaml').exists()


@pytest.mark.parametrize('flag', ['--all', '--dir', '--dry-run', '--name', '--tenant'])
def test_the_old_restore_and_import_flags_are_gone(home, flag):
    result = _cli(home, 'env', 'restore', 'demo', flag, 'x')
    assert result.returncode == 2
    assert 'unrecognized arguments' in result.stderr


@pytest.mark.parametrize('sub', ['restore', 'import'])
def test_process_restore_and_import_are_gone(home, sub):
    result = _cli(home, 'process', sub, 'demo')
    assert result.returncode == 2
    assert 'invalid choice' in result.stderr  # `process` itself is gone since step 4
