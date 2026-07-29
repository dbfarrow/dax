import argparse
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import yaml

from dax import (
    cmd_tenant,
    cmd_tenants,
    _known_tenant_names,
    _prompt_tenant,
    _ensure_tenants_classified,
    _NEW_TENANT_CHOICE,
)


def _write_dax_yaml(home, projects):
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump({'projects': projects}, f)


def _declare(path, tenant):
    path.mkdir(parents=True, exist_ok=True)
    (path / '.dax-tenant').write_text(tenant)


def _mark_used(state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / '.claude.json').write_text('{}')


# --- dax tenant set: unchanged, just declares -------------------------------

def test_cmd_tenant_set_writes_dax_tenant_file(tmp_path):
    subdir = tmp_path / 'project_ysecurity'
    subdir.mkdir()
    args = argparse.Namespace(tenant_command='set', subdir=str(subdir), tenant='ysecurity')

    cmd_tenant(args)

    assert (subdir / '.dax-tenant').read_text() == 'ysecurity'


def test_cmd_tenant_set_overwrites_existing_declaration(tmp_path):
    subdir = tmp_path / 'project_a'
    subdir.mkdir()
    (subdir / '.dax-tenant').write_text('old-tenant')
    args = argparse.Namespace(tenant_command='set', subdir=str(subdir), tenant='new-tenant')

    cmd_tenant(args)

    assert (subdir / '.dax-tenant').read_text() == 'new-tenant'


def test_cmd_tenant_set_errors_on_nonexistent_dir(tmp_path, capsys):
    missing = tmp_path / 'does-not-exist'
    args = argparse.Namespace(tenant_command='set', subdir=str(missing), tenant='ysecurity')

    with pytest.raises(SystemExit):
        cmd_tenant(args)

    assert 'not a directory' in capsys.readouterr().out


# --- dax tenants: registry- and declaration-driven table --------------------

def test_cmd_tenants_shows_single_tenant_project_with_path(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'fabric'
    _declare(repo, 'personal')
    _write_dax_yaml(home, {'fabric': {'dir': str(repo)}})
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l]
    assert lines[0].split() == ['TENANT', 'PROJECT', 'SESSION', 'PATH']
    assert any(line.split() == ['personal', 'fabric', 'no', str(repo)] for line in lines[1:])


def test_cmd_tenants_shows_multi_tenant_projects_with_paths(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'discernment'
    _declare(repo / 'project_ysecurity_comp_model', 'ysecurity')
    _declare(repo / 'project_augment', 'augment')
    _write_dax_yaml(home, {'discernment': {'dir': str(repo), 'multi_tenant': True}})
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    out = capsys.readouterr().out
    assert str(repo / 'project_ysecurity_comp_model') in out
    assert str(repo / 'project_augment') in out
    assert 'ysecurity' in out
    assert 'augment' in out


def test_cmd_tenants_shows_declared_multi_tenant_root_at_repo_root_path(
        tmp_path, monkeypatch, capsys):
    # The root's own project entry must show the true repo root as its path,
    # not tenant_base/<reponame> (which would be nonsensical, e.g.
    # discernment/processes/discernment).
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'discernment'
    _declare(repo, 'personal')
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')
    _write_dax_yaml(home, {
        'discernment': {'dir': str(repo), 'multi_tenant': True, 'tenant_subdir': 'processes'},
    })
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    lines = [l.split() for l in capsys.readouterr().out.splitlines()]
    assert ['personal', 'discernment', 'no', str(repo)] in lines
    assert ['ysecurity', 'project_ysecurity_comp_model', 'no',
            str(repo / 'processes' / 'project_ysecurity_comp_model')] in lines


def test_cmd_tenants_multi_tenant_with_tenant_subdir_shows_full_path(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'discernment'
    _declare(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')
    _write_dax_yaml(home, {
        'discernment': {'dir': str(repo), 'multi_tenant': True, 'tenant_subdir': 'processes'},
    })
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    out = capsys.readouterr().out
    assert str(repo / 'processes' / 'project_ysecurity_comp_model') in out


def test_cmd_tenants_marks_session_yes_when_state_exists(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'fabric'
    _declare(repo, 'personal')
    _write_dax_yaml(home, {'fabric': {'dir': str(repo)}})
    _mark_used(home / '.local' / 'state' / 'dax' / 'tenants' / 'personal' / 'fabric')
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    out = capsys.readouterr().out
    lines = [l.split() for l in out.splitlines()]
    assert ['personal', 'fabric', 'yes', str(repo)] in lines


def test_cmd_tenants_recognizes_credentials_json_as_used_too(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'fabric'
    _declare(repo, 'personal')
    _write_dax_yaml(home, {'fabric': {'dir': str(repo)}})
    state_dir = home / '.local' / 'state' / 'dax' / 'tenants' / 'personal' / 'fabric'
    state_dir.mkdir(parents=True)
    (state_dir / '.credentials.json').write_text('{}')
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    out = capsys.readouterr().out
    lines = [l.split() for l in out.splitlines()]
    assert ['personal', 'fabric', 'yes', str(repo)] in lines


def test_cmd_tenants_skips_projects_whose_dir_does_not_exist_on_this_machine(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    _write_dax_yaml(home, {'ghost': {'dir': str(tmp_path / 'nonexistent')}})
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    out = capsys.readouterr().out
    assert 'no projects resolve to a tenant yet' in out


def test_cmd_tenants_handles_no_dax_yaml(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path / 'empty-home'))

    cmd_tenants(argparse.Namespace())  # must not raise

    assert 'no ~/.dax.yaml found' in capsys.readouterr().out


def test_cmd_tenants_columns_are_aligned_to_fixed_width(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    short_repo = tmp_path / 'repos' / 'a'
    long_repo = tmp_path / 'repos' / 'a-much-longer-project-name'
    _declare(short_repo, 'x')
    _declare(long_repo, 'personal')
    _write_dax_yaml(home, {
        'a': {'dir': str(short_repo)},
        'b': {'dir': str(long_repo)},
    })
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    lines = [l for l in capsys.readouterr().out.splitlines() if l]  # blank separators excluded
    project_col_start = lines[0].index('PROJECT')
    # every row's PROJECT column must start at the same character offset
    assert all(len(line) > project_col_start for line in lines)
    project_values = {'a', 'a-much-longer-project-name'}
    for line in lines[1:]:
        for value in project_values:
            if line[project_col_start:].startswith(value):
                break
        else:
            pytest.fail('row {!r} does not have its PROJECT column aligned'.format(line))


def test_cmd_tenants_blank_line_separates_tenant_groups(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo_a = tmp_path / 'repos' / 'fabric'
    repo_b = tmp_path / 'repos' / 'discernment'
    _declare(repo_a, 'personal')
    _declare(repo_b / 'project_ysecurity_comp_model', 'ysecurity')
    _declare(repo_b / 'project_augment', 'augment')
    _write_dax_yaml(home, {
        'fabric': {'dir': str(repo_a)},
        'discernment': {'dir': str(repo_b), 'multi_tenant': True},
    })
    monkeypatch.setenv('HOME', str(home))

    cmd_tenants(argparse.Namespace())

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == ''  # leading blank line before the whole report
    assert lines[-1] == ''  # trailing blank line after it
    assert lines[1].split()[0] == 'TENANT'

    # Between the header and the trailing blank: one group per tenant (3
    # tenants - augment, personal, ysecurity), separated by exactly one blank
    # line each - never within a tenant's own rows.
    body = lines[2:-1]
    blank_indices = [i for i, l in enumerate(body) if l == '']
    assert len(blank_indices) == 2
    for i in blank_indices:
        assert body[i - 1].split()[0] != body[i + 1].split()[0]


# --- _known_tenant_names -----------------------------------------------------

def test_known_tenant_names_empty_when_no_dax_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'empty-home'))

    assert _known_tenant_names() == set()


def test_known_tenant_names_across_single_and_multi_tenant_projects(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    fabric = tmp_path / 'repos' / 'fabric'
    discernment = tmp_path / 'repos' / 'discernment'
    _declare(fabric, 'personal')
    _declare(discernment / 'project_ysecurity_comp_model', 'ysecurity')
    _write_dax_yaml(home, {
        'fabric': {'dir': str(fabric)},
        'discernment': {'dir': str(discernment), 'multi_tenant': True},
    })
    monkeypatch.setenv('HOME', str(home))

    assert _known_tenant_names() == {'personal', 'ysecurity'}


# --- _prompt_tenant -----------------------------------------------------------

def test_prompt_tenant_picks_from_known_list(tmp_path):
    picked = []

    def fake_select(prompt, choices, default):
        picked.append((prompt, choices, default))
        return 'ysecurity'

    result = _prompt_tenant(tmp_path / 'sub', {'ysecurity', 'personal'}, _select=fake_select)

    assert result == 'ysecurity'
    prompt, choices, default = picked[0]
    assert set(choices) == {'ysecurity', 'personal', _NEW_TENANT_CHOICE}
    assert default is None


def test_prompt_tenant_new_tenant_choice_falls_through_to_text_prompt(tmp_path):
    text_calls = []

    def fake_text(prompt, default):
        text_calls.append((prompt, default))
        return 'brand-new-tenant'

    result = _prompt_tenant(
        tmp_path / 'sub', {'ysecurity'},
        _select=lambda p, c, d: _NEW_TENANT_CHOICE, _text=fake_text)

    assert result == 'brand-new-tenant'
    assert len(text_calls) == 1


def test_prompt_tenant_uses_text_prompt_when_nothing_known_yet(tmp_path):
    result = _prompt_tenant(tmp_path / 'sub', set(), _text=lambda p, d: 'first-tenant')

    assert result == 'first-tenant'


def test_prompt_tenant_passes_current_value_as_select_default(tmp_path):
    captured = {}

    def fake_select(prompt, choices, default):
        captured['default'] = default
        return default

    _prompt_tenant(tmp_path / 'sub', {'ysecurity', 'personal'}, default='personal',
                    _select=fake_select)

    assert captured['default'] == 'personal'


def test_prompt_tenant_raises_on_cancelled_select(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _prompt_tenant(tmp_path / 'sub', {'ysecurity'}, _select=lambda p, c, d: None)


def test_prompt_tenant_raises_on_empty_text(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _prompt_tenant(tmp_path / 'sub', set(), _text=lambda p, d: '   ')


# --- _ensure_tenants_classified ----------------------------------------------

def test_ensure_tenants_classified_single_tenant_prompts_when_undeclared(
        tmp_path, monkeypatch):
    repo = tmp_path / 'fabric'
    repo.mkdir()
    monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known, default=None: 'personal')

    _ensure_tenants_classified({'cwd': str(repo), 'multi_tenant': False})

    assert (repo / '.dax-tenant').read_text() == 'personal'


def test_ensure_tenants_classified_single_tenant_skips_when_already_declared(
        tmp_path, monkeypatch):
    repo = tmp_path / 'fabric'
    _declare(repo, 'personal')

    def fail_if_called(*a, **k):
        raise AssertionError('should not prompt for an already-declared root')

    monkeypatch.setattr('dax._prompt_tenant', fail_if_called)

    _ensure_tenants_classified({'cwd': str(repo), 'multi_tenant': False})

    assert (repo / '.dax-tenant').read_text() == 'personal'


def test_ensure_tenants_classified_multi_tenant_classifies_root_and_children(
        tmp_path, monkeypatch):
    repo = tmp_path / 'discernment'
    (repo / 'processes' / 'project_a').mkdir(parents=True)
    (repo / 'processes' / 'project_b').mkdir(parents=True)
    prompted = []

    def fake_prompt(subdir, known, default=None):
        prompted.append(subdir)
        return 'tenant-for-{}'.format(subdir.name)

    monkeypatch.setattr('dax._prompt_tenant', fake_prompt)

    _ensure_tenants_classified({
        'cwd': str(repo), 'multi_tenant': True, 'tenant_subdir': 'processes',
    })

    assert (repo / '.dax-tenant').read_text() == 'tenant-for-discernment'
    assert (repo / 'processes' / 'project_a' / '.dax-tenant').read_text() == 'tenant-for-project_a'
    assert (repo / 'processes' / 'project_b' / '.dax-tenant').read_text() == 'tenant-for-project_b'
    assert len(prompted) == 3


def test_ensure_tenants_classified_multi_tenant_skips_already_declared_children(
        tmp_path, monkeypatch):
    repo = tmp_path / 'discernment'
    _declare(repo, 'personal')
    _declare(repo / 'processes' / 'project_a', 'ysecurity')
    new_child = repo / 'processes' / 'project_b'
    new_child.mkdir(parents=True)

    monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known, default=None: 'augment')

    _ensure_tenants_classified({
        'cwd': str(repo), 'multi_tenant': True, 'tenant_subdir': 'processes',
    })

    assert (repo / '.dax-tenant').read_text() == 'personal'  # untouched
    assert (repo / 'processes' / 'project_a' / '.dax-tenant').read_text() == 'ysecurity'  # untouched
    assert new_child.joinpath('.dax-tenant').read_text() == 'augment'  # filled in


def test_ensure_tenants_classified_reclassify_all_reprompts_existing(tmp_path, monkeypatch):
    repo = tmp_path / 'fabric'
    _declare(repo, 'old-tenant')
    seen_defaults = []

    def fake_prompt(subdir, known, default=None):
        seen_defaults.append(default)
        return 'new-tenant'

    monkeypatch.setattr('dax._prompt_tenant', fake_prompt)

    _ensure_tenants_classified({'cwd': str(repo), 'multi_tenant': False}, reclassify_all=True)

    assert seen_defaults == ['old-tenant']
    assert (repo / '.dax-tenant').read_text() == 'new-tenant'


def test_ensure_tenants_classified_skips_not_a_project_subdir_entries(tmp_path, monkeypatch):
    repo = tmp_path / 'discernment'
    _declare(repo, 'personal')
    (repo / 'processes' / '.git').mkdir(parents=True)
    (repo / 'processes' / 'node_modules').mkdir(parents=True)

    def fail_if_called_for_vendor_dirs(subdir, known, default=None):
        if subdir.name in ('.git', 'node_modules'):
            raise AssertionError('must not classify vendor/tooling directories')
        return 'ysecurity'

    monkeypatch.setattr('dax._prompt_tenant', fail_if_called_for_vendor_dirs)

    _ensure_tenants_classified({
        'cwd': str(repo), 'multi_tenant': True, 'tenant_subdir': 'processes',
    })

    assert not (repo / 'processes' / '.git' / '.dax-tenant').exists()
    assert not (repo / 'processes' / 'node_modules' / '.dax-tenant').exists()


# --- dax tenant classify ------------------------------------------------------

def test_cmd_tenant_classify_errors_when_not_registered(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'unregistered'
    repo.mkdir()
    _write_dax_yaml(home, {})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)

    args = argparse.Namespace(tenant_command='classify')
    with pytest.raises(SystemExit):
        cmd_tenant(args)

    assert 'not a registered dax project' in capsys.readouterr().out


def test_cmd_tenant_classify_invokes_reclassify_all_with_project_settings(
        tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repos' / 'discernment'
    repo.mkdir(parents=True)
    _write_dax_yaml(home, {
        'discernment': {'dir': str(repo), 'multi_tenant': True, 'tenant_subdir': 'processes'},
    })
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)

    captured = {}

    def fake_ensure(config, reclassify_all=False):
        captured['config'] = config
        captured['reclassify_all'] = reclassify_all

    monkeypatch.setattr('dax._ensure_tenants_classified', fake_ensure)

    cmd_tenant(argparse.Namespace(tenant_command='classify'))

    assert captured['reclassify_all'] is True
    assert captured['config']['cwd'] == str(repo)
    assert captured['config']['multi_tenant'] is True
    assert captured['config']['tenant_subdir'] == 'processes'
