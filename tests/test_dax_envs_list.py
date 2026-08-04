"""Tests for the merged `dax envs list`.

It outer-joins ~/.dax.yaml's projects against a walk of
~/.local/state/dax/tenants/, because an env can be orphaned from either
direction — a registry entry with no state tree, or a state tree with no
registry entry. The second kind is invisible to every other command, and is
what the deletion-grouping rationale needs to see.
"""
import os
import time

import pytest

from dax_creds.init import _human_age, run_envs_list


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
            and 'state tree' not in line and 'directory missing' not in line]


def _config(home, **overrides):
    project = {'dir': str(home / 'src' / 'fabric'), 'image': 'dax-base', 'creds': []}
    project.update(overrides)
    return {'projects': {'fabric': project}}


def test_tenant_column_comes_from_the_registry(home, capsys):
    run_envs_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'tenant' in out and 'personal' in out


def test_state_size_shown_when_the_tree_exists(home, capsys):
    _tree(home, 'personal', 'fabric', size=2048)

    run_envs_list(_config(home, tenant='personal'))

    assert '2K' in capsys.readouterr().out


def test_missing_tree_shows_a_dash_not_an_orphan(home, capsys):
    run_envs_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert '[?]' not in out


def test_matched_tree_is_claimed_and_not_listed_twice(home, capsys):
    _tree(home, 'personal', 'fabric')

    run_envs_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert len(_rows(out)) == 1
    assert '[?]' not in out


def test_orphaned_tree_is_surfaced(home, capsys):
    _tree(home, 'ysecurity', 'acme-process')

    run_envs_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'acme-process' in out
    assert '[?]' in out
    assert 'orphaned state tree' in out


def test_env_without_a_tenant_cannot_claim_a_tree(home, capsys):
    """No declared tenant means no path to look under, so the tree is unattributed."""
    _tree(home, 'personal', 'fabric')

    run_envs_list(_config(home))  # no tenant

    rows = _rows(capsys.readouterr().out)
    assert len(rows) == 2  # the registry row, plus the tree it could not claim
    assert sum(1 for r in rows if r.startswith('  [?]')) == 1


def test_orphans_are_listed_even_when_the_registry_is_empty(home, capsys):
    """Deregistering everything is exactly when every tree becomes an orphan."""
    _tree(home, 'personal', 'gone')

    run_envs_list({'projects': {}})

    out = capsys.readouterr().out
    assert 'gone' in out and '[?]' in out


def test_reports_nothing_at_all_when_there_is_nothing(home, capsys):
    run_envs_list({'projects': {}})

    assert 'No environments registered' in capsys.readouterr().out


def test_legend_only_lists_markers_actually_present(home, capsys):
    run_envs_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'orphaned state tree' not in out
    assert 'directory missing' not in out


# --- last used -------------------------------------------------------------

def test_recently_touched_tree_reads_as_fresh(home, capsys):
    _tree(home, 'personal', 'fabric')

    run_envs_list(_config(home, tenant='personal'))

    out = capsys.readouterr().out
    assert 'used' in out
    assert 'now' in out


def test_old_tree_reports_its_age(home, capsys):
    tree = _tree(home, 'ysecurity', 'stale')
    old = time.time() - 30 * 86400
    os.utime(tree / '.claude.json', (old, old))

    run_envs_list(_config(home, tenant='personal'))

    assert '4w' in capsys.readouterr().out


def test_age_uses_newest_file_not_the_directory(home, capsys):
    """A directory's mtime only moves when entries are added or removed, so an
    edit in place would otherwise look ancient."""
    tree = _tree(home, 'ysecurity', 'mixed')
    old = time.time() - 200 * 86400
    os.utime(tree / '.claude.json', (old, old))
    (tree / 'recent.jsonl').write_text('x')

    run_envs_list(_config(home, tenant='personal'))

    row = [l for l in _rows(capsys.readouterr().out) if 'mixed' in l][0]
    assert row.rstrip().endswith('now'), row


def test_env_without_a_tree_shows_no_age(home, capsys):
    run_envs_list(_config(home, tenant='personal'))

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
    run_envs_list(_config(home, dir=str(home / 'src' / 'vanished')))

    out = capsys.readouterr().out
    assert '[!]' in out and 'directory missing' in out
