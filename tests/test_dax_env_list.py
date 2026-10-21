"""Tests for the merged `dax env list`.

It outer-joins ~/.dax.yaml's projects against a walk of
~/.local/state/dax/tenants/, because an env can be orphaned from either
direction — a registry entry with no state tree, or a state tree with no
registry entry. The second kind is invisible to every other command, and is
what the deletion-grouping rationale needs to see.
"""
import os
import time

import pytest

from dax_creds.init import _human_age, run_env_list


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    # Registry dirs must exist or every row is flagged [!], which is noise here.
    (tmp_path / 'src' / 'fabric').mkdir(parents=True)
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: False)
    return tmp_path


def _tree(home, tenant, project, size=1000):
    path = home / '.local' / 'state' / 'dax' / 'tenants' / tenant / project
    path.mkdir(parents=True)
    (path / '.claude.json').write_bytes(b'x' * size)
    return path


def _rows(out):
    """Data rows only — the name also appears inside the dir column, so
    substring counting over the whole output overcounts."""
    return [line for line in out.splitlines() if line.startswith('  [')
            and 'state tree' not in line and 'directory missing' not in line
            and 'archive only —' not in line]


def _config(home, **overrides):
    project = {'dir': str(home / 'src' / 'fabric'), 'image': 'dax-base', 'creds': []}
    project.update(overrides)
    return {'projects': {'fabric': project}}


def test_tenant_column_comes_from_the_registry(home, capsys):
    run_env_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'tenant' in out and 'personal' in out


def test_state_size_shown_when_the_tree_exists(home, capsys):
    _tree(home, 'personal', 'fabric', size=2048)

    run_env_list(_config(home, tenant='personal'))

    assert '2K' in capsys.readouterr().out


def test_missing_tree_shows_a_dash_not_an_orphan(home, capsys):
    run_env_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert '[?]' not in out


def test_matched_tree_is_claimed_and_not_listed_twice(home, capsys):
    _tree(home, 'personal', 'fabric')

    run_env_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert len(_rows(out)) == 1
    assert '[?]' not in out


def test_orphaned_tree_is_surfaced(home, capsys):
    _tree(home, 'ysecurity', 'acme-process')

    run_env_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'acme-process' in out
    assert '[?]' in out
    assert 'orphaned state tree' in out


def test_env_without_a_tenant_cannot_claim_a_tree(home, capsys):
    """No declared tenant means no path to look under, so the tree is unattributed."""
    _tree(home, 'personal', 'fabric')

    run_env_list(_config(home))  # no tenant

    rows = _rows(capsys.readouterr().out)
    assert len(rows) == 2  # the registry row, plus the tree it could not claim
    assert sum(1 for r in rows if r.startswith('  [?]')) == 1


def test_orphans_are_listed_even_when_the_registry_is_empty(home, capsys):
    """Deregistering everything is exactly when every tree becomes an orphan."""
    _tree(home, 'personal', 'gone')

    run_env_list({'projects': {}})

    out = capsys.readouterr().out
    assert 'gone' in out and '[?]' in out


def test_reports_nothing_at_all_when_there_is_nothing(home, capsys):
    run_env_list({'projects': {}})

    assert 'No environments registered' in capsys.readouterr().out


def test_legend_only_lists_markers_actually_present(home, capsys):
    run_env_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'orphaned state tree' not in out
    assert 'directory missing' not in out


# --- last used -------------------------------------------------------------

def test_recently_touched_tree_reads_as_fresh(home, capsys):
    _tree(home, 'personal', 'fabric')

    run_env_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'used' in out
    assert 'now' in out


def test_old_tree_reports_its_age(home, capsys):
    tree = _tree(home, 'ysecurity', 'stale')
    old = time.time() - 30 * 86400
    os.utime(tree / '.claude.json', (old, old))

    run_env_list(_config(home, tenant='personal'))

    assert '4w' in capsys.readouterr().out


def test_age_uses_newest_file_not_the_directory(home, capsys):
    """A directory's mtime only moves when entries are added or removed, so an
    edit in place would otherwise look ancient."""
    tree = _tree(home, 'ysecurity', 'mixed')
    old = time.time() - 200 * 86400
    os.utime(tree / '.claude.json', (old, old))
    (tree / 'recent.jsonl').write_text('x')

    run_env_list(_config(home, tenant='personal'))

    row = [l for l in _rows(capsys.readouterr().out) if 'mixed' in l][0]
    assert row.rstrip().endswith('now'), row


def test_env_without_a_tree_shows_no_age(home, capsys):
    run_env_list(_config(home, tenant='personal'))

    row = [l for l in _rows(capsys.readouterr().out) if 'fabric' in l][0]
    assert row.rstrip().endswith('-')


@pytest.mark.parametrize('seconds,expected', [
    (10, 'now'),
    (5 * 60, '5m'),
    (3 * 3600, '3h'),
    (3 * 86400, '3d'),
    (30 * 86400, '4w'),
    (400 * 86400, '1y'),
])
def test_human_age_units(seconds, expected):
    now = 1_000_000_000.0
    assert _human_age(now - seconds, now=now) == expected


def test_human_age_of_never_used_tree():
    assert _human_age(0) == '-'


def test_missing_project_directory_is_flagged(home, capsys):
    run_env_list(_config(home, dir=str(home / 'src' / 'vanished')))

    out = capsys.readouterr().out
    assert '[!]' in out and 'directory missing' in out


# --- the CLI surface after step 8 -------------------------------------------

def _cli(home, *argv, module=False):
    import os
    import subprocess
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, HOME=str(home), PYTHONPATH=str(repo))
    cmd = ([sys.executable, '-m', 'dax_creds.cli'] if module
           else [sys.executable, str(repo / 'dax.py')])
    return subprocess.run(cmd + list(argv), capture_output=True, text=True, env=env, cwd=str(home))


def test_env_list_runs_through_cmd_env(home, monkeypatch, capsys):
    import argparse
    import dax
    (home / '.dax.yaml').write_text(
        'projects:\n  fabric: {dir: %s, tenant: personal}\n' % (home / 'src' / 'fabric'))
    monkeypatch.chdir(home)  # not an env: list must work from anywhere

    dax.cmd_env(argparse.Namespace(env_command='list', explain=False))

    assert 'fabric' in capsys.readouterr().out


@pytest.mark.parametrize('argv', [('envs', 'list'), ('tenant', 'set', 'x', 'y'), ('tenants',)])
def test_envs_tenant_and_tenants_are_gone(home, argv):
    result = _cli(home, *argv)
    assert result.returncode == 2
    assert "invalid choice: '{}'".format(argv[0]) in result.stderr


def test_dax_creds_resolve_tenant_is_gone(home):
    result = _cli(home, 'resolve-tenant', module=True)
    assert result.returncode == 1
    assert "unknown subcommand 'resolve-tenant'" in result.stderr


# --- last archived ----------------------------------------------------------------

def _archive(backup_dir, name, date, age_seconds=None):
    daily = backup_dir / 'daily'
    daily.mkdir(parents=True, exist_ok=True)
    path = daily / '{}-{}.tar.gz'.format(name, date)
    path.write_bytes(b'')
    if age_seconds is not None:
        t = time.time() - age_seconds
        os.utime(path, (t, t))
    return path


def _column(out, header):
    lines = out.splitlines()
    head = next(l for l in lines if header in l and 'tenant' in l)
    start, name_at = head.index(header), head.index('name')
    return {l[name_at:].split()[0]: l[start:].split()[0] for l in _rows(out)}


def test_archived_column_shows_the_newest_archive(home, capsys):
    import dax
    backup_dir = home / 'backups'
    _archive(backup_dir, 'fabric', '2026-10-01', age_seconds=20 * 86400)
    _archive(backup_dir, 'fabric', '2026-10-18', age_seconds=3 * 86400)
    config = _config(home, tenant='personal')
    config['backup_dir'] = str(backup_dir)

    archived, _ = dax._last_archived(config)
    run_env_list(config, archived=archived)

    out = capsys.readouterr().out
    assert 'archived' in out
    assert _column(out, 'archived') == {'fabric': '3d'}


def test_an_env_never_archived_says_never(home, capsys):
    run_env_list(_config(home, tenant='personal'), archived={})

    assert _column(capsys.readouterr().out, 'archived') == {'fabric': 'never'}


def test_a_hyphenated_name_is_matched_exactly(home):
    import dax
    backup_dir = home / 'backups'
    _archive(backup_dir, 'fabric-old', '2026-10-18')
    config = {'backup_dir': str(backup_dir), 'projects': {}}

    archived, _ = dax._last_archived(config)

    assert set(archived) == {'fabric-old'}


def test_a_missing_backup_dir_is_unknown_not_never(home, capsys):
    import dax
    config = _config(home, tenant='personal')
    config['backup_dir'] = str(home / 'not-mounted')

    archived, backup_dir = dax._last_archived(config)
    assert archived is None
    run_env_list(config, missing_backup_dir=backup_dir)

    out = capsys.readouterr().out
    assert _column(out, 'archived') == {'fabric': '?'}
    assert 'backup_dir ~/not-mounted not found' in out


def test_an_orphaned_tree_shows_its_archive_too(home, capsys):
    _tree(home, 'ysecurity', 'gone')

    run_env_list(_config(home, tenant='personal'), archived={'gone': time.time() - 7 * 86400})

    assert _column(capsys.readouterr().out, 'archived')['gone'] == '1w'


def test_dax_env_list_passes_the_archive_ages(home, monkeypatch, capsys):
    import argparse
    import dax
    import yaml
    backup_dir = home / 'backups'
    _archive(backup_dir, 'fabric', '2026-10-18', age_seconds=3 * 86400)
    (home / '.dax.yaml').write_text(yaml.dump({
        'backup_dir': str(backup_dir), **_config(home, tenant='personal')}))

    dax.cmd_env(argparse.Namespace(env_command='list', name=None, explain=False))

    assert _column(capsys.readouterr().out, 'archived') == {'fabric': '3d'}


# --- grouping -------------------------------------------------------------------

def test_rows_are_grouped_by_tenant_then_sorted_by_name(home, capsys):
    for d in ('zeta', 'alpha', 'mid', 'loose'):
        (home / 'src' / d).mkdir()
    _tree(home, 'acme', 'orphan')
    config = {'projects': {
        'zeta': {'dir': str(home / 'src' / 'zeta'), 'tenant': 'personal'},
        'loose': {'dir': str(home / 'src' / 'loose')},
        'mid': {'dir': str(home / 'src' / 'mid'), 'tenant': 'acme'},
        'alpha': {'dir': str(home / 'src' / 'alpha'), 'tenant': 'personal'},
    }}

    run_env_list(config)

    lines = capsys.readouterr().out.splitlines()
    head = next(l for l in lines if 'tenant' in l and 'name' in l)
    at = head.index('name')
    body = lines[lines.index(head) + 2:]
    body = body[:next(i for i, l in enumerate(body) if l.strip().startswith('[?] orphaned'))]
    names = [l[at:].split()[0] if l.strip() else '' for l in body]
    while names and names[-1] == '':
        names.pop()
    # acme (mid, orphan), personal (alpha, zeta), then no tenant (loose)
    assert names == ['mid', 'orphan', '', 'alpha', 'zeta', '', 'loose']


def _group_order(out):
    lines = out.splitlines()
    head = next(l for l in lines if 'tenant' in l and 'name' in l)
    at = head.index('tenant')
    seen = []
    for l in _rows(out):
        t = l[at:].split()[0]
        if not seen or seen[-1] != t:
            seen.append(t)
    return seen


def test_tenants_follow_the_order_of_the_tenants_block(home, capsys):
    import yaml
    for d in ('a', 'b', 'c', 'd', 'e'):
        (home / 'src' / d).mkdir()
    # Loaded the way dax loads it, so a bare `personal:` (no settings) is a
    # place in the order and nothing else.
    config = yaml.safe_load("""
tenants:
  personal:
  ysecurity:
    claude: {chrome_profile: Profile 5}
  acme:
""")
    config['projects'] = {
        'a': {'dir': str(home / 'src' / 'a'), 'tenant': 'acme'},
        'b': {'dir': str(home / 'src' / 'b'), 'tenant': 'zulu'},
        'c': {'dir': str(home / 'src' / 'c'), 'tenant': 'personal'},
        'd': {'dir': str(home / 'src' / 'd'), 'tenant': 'beta'},
        'e': {'dir': str(home / 'src' / 'e'), 'tenant': 'ysecurity'},
    }

    run_env_list(config)

    # listed order first, then the rest alphabetically
    assert _group_order(capsys.readouterr().out) == [
        'personal', 'ysecurity', 'acme', 'beta', 'zulu']


def test_a_malformed_tenants_block_falls_back_to_alphabetical(home, capsys):
    for d in ('a', 'b'):
        (home / 'src' / d).mkdir()
    config = {'tenants': ['zulu'], 'projects': {
        'a': {'dir': str(home / 'src' / 'a'), 'tenant': 'zulu'},
        'b': {'dir': str(home / 'src' / 'b'), 'tenant': 'acme'}}}

    run_env_list(config)

    assert _group_order(capsys.readouterr().out) == ['acme', 'zulu']


# --- archive-only envs (--all) ------------------------------------------------------

def _archive_only_setup(home):
    _tree(home, 'personal', 'fabric')
    _tree(home, 'ysecurity', 'orphan')
    # fabric is registered and orphan has a tree, so only parked-a/b are archive-only
    return {'fabric': time.time() - 86400, 'orphan': time.time() - 86400,
            'parked-b': time.time() - 30 * 86400, 'parked-a': time.time() - 400 * 86400}


def test_all_lists_archive_only_envs_last_under_a_synthetic_tenant(home, capsys):
    archived = _archive_only_setup(home)

    run_env_list(_config(home, tenant='personal'), archived=archived, show_all=True)

    out = capsys.readouterr().out
    assert _group_order(out) == ['personal', 'ysecurity', '(archive-only)']
    rows = [r for r in _rows(out) if r.startswith('  [a]')]
    assert len(rows) == 2 and 'parked-a' in rows[0] and 'parked-b' in rows[1]
    assert _column(out, 'archived')['parked-a'] == '1y'
    assert 'archive only — to bring it back' in out
    assert 'only as archives' not in out


def test_without_all_there_is_just_a_count(home, capsys):
    archived = _archive_only_setup(home)

    run_env_list(_config(home, tenant='personal'), archived=archived)

    out = capsys.readouterr().out
    assert '(archive-only)' not in out
    assert '2 more envs exist only as archives (dax env list --all)' in out


def test_one_archive_only_env_is_singular(home, capsys):
    run_env_list(_config(home, tenant='personal'), archived={'gone': time.time()})

    assert '1 more env exists only as archives' in capsys.readouterr().out


def test_no_footer_when_every_archive_has_a_row(home, capsys):
    run_env_list(_config(home, tenant='personal'), archived={'fabric': time.time()})

    assert 'only as archives' not in capsys.readouterr().out


def test_an_empty_registry_still_mentions_archives(home, capsys):
    run_env_list({'projects': {}}, archived={'gone': time.time()})

    out = capsys.readouterr().out
    assert 'No environments registered' in out
    assert '1 more env exists only as archives (dax env list --all).' in out


def test_dax_env_list_all_flag(home, capsys):
    import argparse
    import dax
    import yaml
    backup_dir = home / 'backups'
    _archive(backup_dir, 'parked-a', '2026-01-01')
    (home / '.dax.yaml').write_text(yaml.dump({
        'backup_dir': str(backup_dir), **_config(home, tenant='personal')}))

    dax.cmd_env(argparse.Namespace(env_command='list', name=None, explain=False, all=True))

    assert '(archive-only)' in capsys.readouterr().out


# --- hidden tenants (decision 45) ----------------------------------------------------

def _hidden_setup(home):
    import yaml
    for d in ('a', 'b', 'c', 'd'):
        (home / 'src' / d).mkdir()
    _tree(home, 'test', 'orphan')
    config = yaml.safe_load("""
tenants:
  personal:
  test:
    hidden: true
  parked:
    hidden: true
  ysecurity:
    hidden: false
""")
    config['projects'] = {
        'a': {'dir': str(home / 'src' / 'a'), 'tenant': 'personal'},
        'b': {'dir': str(home / 'src' / 'b'), 'tenant': 'parked'},
        'c': {'dir': str(home / 'src' / 'c'), 'tenant': 'test'},
        'd': {'dir': str(home / 'src' / 'd'), 'tenant': 'ysecurity'},
    }
    return config


def test_hidden_tenants_are_left_out_and_counted(home, capsys):
    run_env_list(_hidden_setup(home))

    out = capsys.readouterr().out
    assert _group_order(out) == ['personal', 'ysecurity']
    # the orphaned tree under test is hidden with it
    assert '3 envs hidden in test, parked (dax env list --all)' in out


def test_all_shows_hidden_tenants_in_their_order(home, capsys):
    run_env_list(_hidden_setup(home), show_all=True)

    out = capsys.readouterr().out
    assert _group_order(out) == ['personal', 'test', 'parked', 'ysecurity']
    assert 'hidden in' not in out


def test_the_footer_combines_hidden_and_archive_only(home, capsys):
    run_env_list(_hidden_setup(home), archived={'gone': time.time(), 'gone2': time.time()})

    assert ('3 envs hidden in test, parked; 2 more exist only as archives '
            '(dax env list --all)') in capsys.readouterr().out


def test_one_hidden_env_is_singular(home, capsys):
    config = _hidden_setup(home)
    del config['projects']['c']
    (home / '.local/state/dax/tenants/test/orphan/.claude.json').unlink()
    (home / '.local/state/dax/tenants/test/orphan').rmdir()

    run_env_list(config)

    assert '1 env hidden in parked (dax env list --all)' in capsys.readouterr().out


def test_when_everything_is_hidden_say_so(home, capsys):
    config = _hidden_setup(home)
    for n in ('a', 'd'):
        del config['projects'][n]

    run_env_list(config)

    out = capsys.readouterr().out
    assert 'Every env is in a hidden tenant.' in out
    assert 'hidden in test, parked (dax env list --all).' in out


def test_hidden_must_be_a_boolean():
    from dax_creds.config import unknown_tenant_entries
    config = {'tenants': {'t': {'hidden': 'yes please'}},
              'projects': {'x': {'dir': '/x', 'tenant': 't'}}}
    assert unknown_tenant_entries(config) == [
        "`tenants.t.hidden` should be true or false, not 'yes please' — ignored"]
    config['tenants']['t']['hidden'] = True
    assert unknown_tenant_entries(config) == []


def test_a_non_boolean_hidden_does_not_hide():
    from dax_creds.config import hidden_tenants
    assert hidden_tenants({'tenants': {'t': {'hidden': 'true'}, 'u': {'hidden': True},
                                       'v': None, 'w': {}}}) == {'u'}
