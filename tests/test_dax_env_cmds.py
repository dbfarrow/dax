"""Tests for `dax env show` and the unknown-key check (`dax env set` and its
`run_env_set` helper are gone: env fields are hand-edited in ~/.dax.yaml).
"""
import pytest

from dax_creds.config import (
    load_dax_config, state_tree_path,
    unknown_env_keys, describe_unknown_env_keys,
)
from dax_creds.init import run_env_show, save_config


CONFIG = """\
# hand-maintained
credentials:
  claude-personal-fabric:
    provider: claude
  github-dfarrow:
    provider: github

features:
- claude
#- claude_tenant_state

projects:
  fabric:
    dir: /Users/dfarrow/src/fabric
    image: dax-base
    creds:
    - claude-personal-fabric
  legacy:
    dir: /Users/dfarrow/src/legacy
    image: dax-base
    multi_tenant: true
    tenant_subdir: processes
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.dax.yaml').write_text(CONFIG)
    return tmp_path

def _set(config, name, field, value, valid_features=None):
    """Hand-edit one field of an env entry, the way the user now does in
    ~/.dax.yaml; list fields take a comma-separated string."""
    if field in ('creds', 'features', 'mounts'):
        value = [v.strip() for v in value.split(',') if v.strip()]
    config['projects'][name][field] = value
    save_config(config)


# --- show ------------------------------------------------------------------

def test_show_reports_unset_tenant(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert '(unset)' in capsys.readouterr().out


def test_show_reports_tenant_and_state_path(home, capsys):
    config = load_dax_config()
    _set(config, 'fabric', 'tenant', 'personal')
    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'personal' in out
    assert 'state/dax/tenants/personal/fabric' in out
    assert 'not created yet' in out


def test_show_flags_retired_multi_tenant_fields_as_retired(home, capsys):
    run_env_show(load_dax_config(), 'legacy')

    out = capsys.readouterr().out
    assert 'retired key(s) multi_tenant, tenant_subdir — ignored; remove them' in out


def test_show_flags_a_typo_d_key(home, capsys):
    config = load_dax_config()
    config['projects']['fabric']['tennant'] = 'personal'

    run_env_show(config, 'fabric')

    out = capsys.readouterr().out
    assert 'unknown key(s) tennant — ignored (typo?)' in out


def test_show_is_silent_about_keys_when_all_are_known(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert 'unknown key' not in capsys.readouterr().out


def test_unknown_env_keys_reports_every_env_with_one():
    config = {'projects': {
        'clean': {'dir': '/x', 'image': 'i', 'creds': [], 'tenant': 't'},
        'typo': {'dir': '/y', 'feautres': ['mounts']},
        'legacy': {'dir': '/z', 'multi_tenant': True, 'tenant_subdir': 'p'},
    }}

    assert unknown_env_keys(config) == {
        'typo': ['feautres'],
        'legacy': ['multi_tenant', 'tenant_subdir'],
    }


def test_unknown_env_keys_handles_no_projects():
    assert unknown_env_keys({}) == {}
    assert unknown_env_keys({'projects': None}) == {}


def test_show_unknown_env_raises(home):
    with pytest.raises(KeyError):
        run_env_show(load_dax_config(), 'nope')


# --- bare provider tokens --------------------------------------------------

def test_show_resolves_a_bare_token_to_the_derived_name(home, capsys):
    _set(load_dax_config(), 'fabric', 'tenant', 'personal')
    _set(load_dax_config(), 'fabric', 'creds', 'claude')
    capsys.readouterr()  # drop the `set` echoes

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '-> claude-personal-fabric' in out
    # This fixture already registers claude-personal-fabric, so it is not flagged.
    assert 'not registered yet' not in out


def test_show_flags_a_derived_name_that_is_not_registered(home, capsys):
    _set(load_dax_config(), 'fabric', 'tenant', 'ysecurity')
    _set(load_dax_config(), 'fabric', 'creds', 'claude')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '-> claude-ysecurity-fabric   [implied]' in out


def test_show_explains_a_bare_token_that_cannot_resolve(home, capsys):
    """No tenant means no derivable name — say so here, not at launch."""
    _set(load_dax_config(), 'fabric', 'creds', 'claude')
    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'unresolved' in out and 'no tenant' in out


# --- mounts ----------------------------------------------------------------

def test_show_lists_mounts_when_set(home, capsys):
    _set(load_dax_config(), 'fabric', 'mounts', '~/discernment')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '~/discernment' in out


def test_show_omits_mounts_line_when_unset(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert '  mounts   ' not in capsys.readouterr().out


def test_show_has_no_mounts_feature_note(home, capsys):
    """A non-empty `mounts:` switches the feature on by itself (decision 40),
    so there's no "inactive" state to flag."""
    _set(load_dax_config(), 'fabric', 'mounts', '~/discernment')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    assert 'inactive' not in capsys.readouterr().out


# --- substrate (one global path, decision 41) ---------------------------------

def test_show_displays_the_global_substrate_for_an_env_that_opts_in(home, capsys):
    config = load_dax_config()
    config['substrate'] = '~/virgil'
    config['projects']['fabric']['features'] = ['substrate']

    run_env_show(config, 'fabric')

    assert 'substrate ~/virgil   (global)' in capsys.readouterr().out


def test_show_says_when_the_global_substrate_is_missing(home, capsys):
    config = load_dax_config()
    config['projects']['fabric']['features'] = ['substrate']

    run_env_show(config, 'fabric')

    assert 'NOT SET' in capsys.readouterr().out


def test_show_omits_substrate_when_the_env_does_not_opt_in(home, capsys):
    config = load_dax_config()
    config['substrate'] = '~/virgil'

    run_env_show(config, 'fabric')

    assert '~/virgil' not in capsys.readouterr().out


def test_a_per_env_substrate_is_reported_as_retired(home, capsys):
    config = load_dax_config()
    config['projects']['fabric']['substrate'] = '~/old'

    run_env_show(config, 'fabric')

    out = capsys.readouterr().out
    assert 'retired key substrate — ignored; move its path to a top-level `substrate:`' in out


def test_show_does_not_flag_substrate_when_the_feature_is_active(home, capsys):
    config = load_dax_config()
    _set(config, 'fabric', 'substrate', '~/virgil')
    _set(config, 'fabric', 'features', 'claude,substrate')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' not in out


# --- key descriptions ------------------------------------------------------

def test_describe_unknown_env_keys_separates_typos_from_retired_keys():
    assert describe_unknown_env_keys(['tennant']) == 'unknown key(s) tennant — ignored (typo?)'
    assert describe_unknown_env_keys(['multi_tenant']) == (
        'retired key(s) multi_tenant — ignored; remove them')
    assert describe_unknown_env_keys(['tennant', 'tenant_subdir']) == (
        'unknown key(s) tennant — ignored (typo?); retired key(s) tenant_subdir — ignored; remove them')


def test_show_notes_old_claude_entries(home, capsys):
    config = load_dax_config()
    config['projects']['fabric']['features'] = ['claude_tenant_state']
    config['projects']['fabric']['creds'] = ['claude']
    config['projects']['fabric']['tenant'] = 'personal'

    run_env_show(config, 'fabric')

    out = capsys.readouterr().out
    assert 'still lists claude_tenant_state (features), claude (creds)' in out


def test_show_displays_the_implied_credential_with_nothing_in_creds(home, capsys):
    config = load_dax_config()
    config['projects']['fabric']['creds'] = []
    config['projects']['fabric']['tenant'] = 'personal'

    run_env_show(config, 'fabric')

    assert '-> claude-personal-fabric   [implied]' in capsys.readouterr().out


# --- an empty list key ------------------------------------------------------
#
# Hand-editing can leave `creds:` with nothing after it, which YAML reads as
# null; every reader treats that like a missing key.

def test_an_empty_creds_key_is_read_as_no_creds(home, capsys):
    from dax_creds.init import run_creds_list, run_env_list
    config = load_dax_config()
    config['projects']['fabric']['creds'] = None
    config['projects']['fabric']['tenant'] = 'personal'

    run_env_list(config)
    run_creds_list(config)
    run_env_show(config, 'fabric')

    assert '-> claude-personal-fabric   [implied]' in capsys.readouterr().out


def test_show_says_never_used_for_an_empty_state_tree(home, capsys):
    config = load_dax_config()
    config['projects']['fabric']['tenant'] = 'personal'
    state_tree_path('personal', 'fabric').mkdir(parents=True)

    run_env_show(config, 'fabric')

    out = capsys.readouterr().out
    assert '[0B, never used]' in out
    assert '- ago' not in out
