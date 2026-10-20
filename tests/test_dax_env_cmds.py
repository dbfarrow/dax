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



# --- set -------------------------------------------------------------------

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


# --- field help ------------------------------------------------------------

# --- env set features -------------------------------------------------------
#
# Added once per-env features became load-bearing: `claude_tenant_state` is opted
# into per env, and switching it on means moving `claude` off the global list and
# onto the envs that still want the shared mount — four edits, which is YAML
# surgery without this.

# --- env set mounts ----------------------------------------------------------
#
# For migrating discernment's processes: each process becomes its own env, and
# needs the discernment sidecar repo mounted alongside its own — as a sibling
# under $HOME (feature_mounts), not nested inside another mount.

def test_show_lists_mounts_when_set(home, capsys):
    _set(load_dax_config(), 'fabric', 'mounts', '~/discernment')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '~/discernment' in out


def test_show_omits_mounts_line_when_unset(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert 'mounts' not in capsys.readouterr().out


def test_show_flags_mounts_set_without_the_feature_active(home, capsys):
    _set(load_dax_config(), 'fabric', 'mounts', '~/discernment')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' in out


def test_show_does_not_flag_mounts_when_the_feature_is_active(home, capsys):
    config = load_dax_config()
    _set(config, 'fabric', 'mounts', '~/discernment')
    _set(config, 'fabric', 'features', 'claude,mounts')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' not in out


# --- env set substrate -------------------------------------------------------
#
# A single path, unlike the generic `mounts` list: dax needs to know
# specifically which mount holds shared/wire.sh and shared/new-process.sh, not
# just that something extra is mounted (docs/design/VALIDATION.md).

def test_show_lists_substrate_when_set(home, capsys):
    _set(load_dax_config(), 'fabric', 'substrate', '~/virgil')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '~/virgil' in out


def test_show_omits_substrate_line_when_unset(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert 'substrate' not in capsys.readouterr().out


def test_show_flags_substrate_set_without_the_feature_active(home, capsys):
    _set(load_dax_config(), 'fabric', 'substrate', '~/virgil')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' in out


def test_show_does_not_flag_substrate_when_the_feature_is_active(home, capsys):
    config = load_dax_config()
    _set(config, 'fabric', 'substrate', '~/virgil')
    _set(config, 'fabric', 'features', 'claude,substrate')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' not in out


# --- env accept-shared-files -------------------------------------------------
#
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
