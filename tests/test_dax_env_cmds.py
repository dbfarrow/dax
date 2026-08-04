"""Tests for `dax env show` / `dax env set`.

These exist so changing one field on a registered env doesn't require walking
`dax init`'s full wizard (image -> credential checkbox -> new-credential loop)
just to edit a single string.
"""
import pytest

from dax_creds.config import (
    ENV_FIELDS, env_field_help, load_dax_config, state_tree_path,
    sync_claude_shared_files,
)
from dax_creds.init import run_env_accept_shared_files, run_env_set, run_env_show


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


# --- set -------------------------------------------------------------------

def test_set_tenant_persists_to_disk(home):
    run_env_set(load_dax_config(), 'fabric', 'tenant', 'personal')

    assert load_dax_config()['projects']['fabric']['tenant'] == 'personal'


def test_set_preserves_comments(home):
    """The whole point of the ruamel prerequisite — `set` is a routine writer."""
    run_env_set(load_dax_config(), 'fabric', 'tenant', 'personal')

    written = (home / '.dax.yaml').read_text()
    assert '#- claude_tenant_state' in written
    assert '# hand-maintained' in written


def test_set_creds_splits_a_comma_list(home):
    run_env_set(load_dax_config(), 'fabric', 'creds',
                'claude-personal-fabric, github-dfarrow')

    assert load_dax_config()['projects']['fabric']['creds'] == [
        'claude-personal-fabric', 'github-dfarrow']


def test_set_creds_rejects_undefined_credential(home):
    with pytest.raises(ValueError, match='bogus'):
        run_env_set(load_dax_config(), 'fabric', 'creds', 'claude-personal-fabric,bogus')

    assert 'bogus' not in (home / '.dax.yaml').read_text()


def test_set_unknown_env_names_the_known_ones(home):
    with pytest.raises(KeyError) as excinfo:
        run_env_set(load_dax_config(), 'nope', 'tenant', 'x')

    assert 'fabric' in excinfo.value.args[0] and 'legacy' in excinfo.value.args[0]


def test_set_unknown_field_lists_valid_fields(home):
    with pytest.raises(ValueError) as excinfo:
        run_env_set(load_dax_config(), 'fabric', 'nonsense', 'x')

    message = excinfo.value.args[0]
    for field in ENV_FIELDS:
        assert field in message


def test_set_does_not_write_when_it_rejects(home):
    before = (home / '.dax.yaml').read_text()

    with pytest.raises(ValueError):
        run_env_set(load_dax_config(), 'fabric', 'nonsense', 'x')

    assert (home / '.dax.yaml').read_text() == before


# --- show ------------------------------------------------------------------

def test_show_reports_unset_tenant(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert '(unset)' in capsys.readouterr().out


def test_show_reports_tenant_and_state_path(home, capsys):
    config = load_dax_config()
    run_env_set(config, 'fabric', 'tenant', 'personal')
    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'personal' in out
    assert 'state/dax/tenants/personal/fabric' in out
    assert 'not created yet' in out


def test_show_flags_deprecated_multi_tenant_fields(home, capsys):
    run_env_show(load_dax_config(), 'legacy')

    out = capsys.readouterr().out
    assert 'multi_tenant' in out and 'tenant_subdir' in out
    assert 'deprecated' in out


def test_show_unknown_env_raises(home):
    with pytest.raises(KeyError):
        run_env_show(load_dax_config(), 'nope')


# --- bare provider tokens --------------------------------------------------

def test_set_accepts_a_bare_provider_token(home):
    run_env_set(load_dax_config(), 'fabric', 'creds', 'claude,github-dfarrow')

    assert load_dax_config()['projects']['fabric']['creds'] == ['claude', 'github-dfarrow']


def test_show_resolves_a_bare_token_to_the_derived_name(home, capsys):
    run_env_set(load_dax_config(), 'fabric', 'tenant', 'personal')
    run_env_set(load_dax_config(), 'fabric', 'creds', 'claude')
    capsys.readouterr()  # drop the `set` echoes

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '-> claude-personal-fabric' in out
    # This fixture already registers claude-personal-fabric, so it is not flagged.
    assert 'not registered yet' not in out


def test_show_flags_a_derived_name_that_is_not_registered(home, capsys):
    run_env_set(load_dax_config(), 'fabric', 'tenant', 'ysecurity')
    run_env_set(load_dax_config(), 'fabric', 'creds', 'claude')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '-> claude-ysecurity-fabric' in out
    assert 'not registered yet' in out


def test_show_explains_a_bare_token_that_cannot_resolve(home, capsys):
    """No tenant means no derivable name — say so here, not at launch."""
    run_env_set(load_dax_config(), 'fabric', 'creds', 'claude')
    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'unresolved' in out and 'no tenant' in out


# --- field help ------------------------------------------------------------

def test_field_help_documents_every_settable_field():
    text = env_field_help()
    for field, description in ENV_FIELDS.items():
        assert field in text and description in text


# --- env set features -------------------------------------------------------
#
# Added once per-env features became load-bearing: `claude_tenant_state` is opted
# into per env, and switching it on means moving `claude` off the global list and
# onto the envs that still want the shared mount — four edits, which is YAML
# surgery without this.

def test_env_set_features_replaces_the_list(home):
    config = {'projects': {'fabric': {'dir': '/repos/fabric'}}}

    run_env_set(config, 'fabric', 'features', 'claude_tenant_state',
                valid_features={'claude', 'claude_tenant_state', 'ssh'})

    assert config['projects']['fabric']['features'] == ['claude_tenant_state']


def test_env_set_features_accepts_a_comma_separated_list(home):
    config = {'projects': {'fabric': {'dir': '/repos/fabric'}}}

    run_env_set(config, 'fabric', 'features', 'claude, ssh',
                valid_features={'claude', 'claude_tenant_state', 'ssh'})

    assert config['projects']['fabric']['features'] == ['claude', 'ssh']


def test_env_set_features_rejects_a_misspelled_feature(home):
    """A misspelled feature in a config is silently ignored at launch — the same
    failure mode a typo'd credential name had, where `claud-fre` sat unused for
    weeks."""
    config = {'projects': {'fabric': {'dir': '/repos/fabric'}}}

    with pytest.raises(ValueError) as excinfo:
        run_env_set(config, 'fabric', 'features', 'claude_tenant_stat',
                    valid_features={'claude', 'claude_tenant_state'})

    assert 'not a dax feature' in excinfo.value.args[0]
    assert 'features' not in config['projects']['fabric']


def test_env_set_features_validation_is_skipped_without_a_list(home):
    """The valid set comes from dax.py, so callers that have none still work."""
    config = {'projects': {'fabric': {'dir': '/repos/fabric'}}}

    run_env_set(config, 'fabric', 'features', 'anything')

    assert config['projects']['fabric']['features'] == ['anything']


def test_features_is_a_documented_field():
    assert 'features' in ENV_FIELDS
    assert 'features' in env_field_help()


# --- env set mounts ----------------------------------------------------------
#
# For migrating discernment's processes: each process becomes its own env, and
# needs the discernment sidecar repo mounted alongside its own — as a sibling
# under $HOME (feature_mounts), not nested inside another mount.

def test_env_set_mounts_splits_a_comma_list(home):
    run_env_set(load_dax_config(), 'fabric', 'mounts', '~/discernment, ~/other')

    assert load_dax_config()['projects']['fabric']['mounts'] == ['~/discernment', '~/other']


def test_mounts_is_a_documented_field():
    assert 'mounts' in ENV_FIELDS
    assert 'mounts' in env_field_help()


def test_show_lists_mounts_when_set(home, capsys):
    run_env_set(load_dax_config(), 'fabric', 'mounts', '~/discernment')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '~/discernment' in out


def test_show_omits_mounts_line_when_unset(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert 'mounts' not in capsys.readouterr().out


def test_show_flags_mounts_set_without_the_feature_active(home, capsys):
    run_env_set(load_dax_config(), 'fabric', 'mounts', '~/discernment')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' in out


def test_show_does_not_flag_mounts_when_the_feature_is_active(home, capsys):
    config = load_dax_config()
    run_env_set(config, 'fabric', 'mounts', '~/discernment')
    run_env_set(config, 'fabric', 'features', 'claude,mounts')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' not in out


# --- env set substrate -------------------------------------------------------
#
# A single path, unlike the generic `mounts` list: dax needs to know
# specifically which mount holds shared/wire.sh and shared/new-process.sh, not
# just that something extra is mounted (docs/design/VALIDATION.md).

def test_env_set_substrate(home):
    run_env_set(load_dax_config(), 'fabric', 'substrate', '~/virgil')

    assert load_dax_config()['projects']['fabric']['substrate'] == '~/virgil'


def test_substrate_is_a_documented_field():
    assert 'substrate' in ENV_FIELDS
    assert 'substrate' in env_field_help()


def test_show_lists_substrate_when_set(home, capsys):
    run_env_set(load_dax_config(), 'fabric', 'substrate', '~/virgil')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert '~/virgil' in out


def test_show_omits_substrate_line_when_unset(home, capsys):
    run_env_show(load_dax_config(), 'fabric')

    assert 'substrate' not in capsys.readouterr().out


def test_show_flags_substrate_set_without_the_feature_active(home, capsys):
    run_env_set(load_dax_config(), 'fabric', 'substrate', '~/virgil')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' in out


def test_show_does_not_flag_substrate_when_the_feature_is_active(home, capsys):
    config = load_dax_config()
    run_env_set(config, 'fabric', 'substrate', '~/virgil')
    run_env_set(config, 'fabric', 'features', 'claude,substrate')
    capsys.readouterr()

    run_env_show(load_dax_config(), 'fabric')

    out = capsys.readouterr().out
    assert 'inactive' not in out


# --- env accept-shared-files -------------------------------------------------
#
# The escape hatch for the drift warning `sync_claude_shared_files` prints at
# `dax run` time: resolving it is always an explicit act, never automatic.

def test_accept_shared_files_requires_a_tenant(home):
    with pytest.raises(ValueError, match='no tenant'):
        run_env_accept_shared_files(load_dax_config(), 'fabric')


def test_accept_shared_files_unknown_env_raises(home):
    with pytest.raises(KeyError, match='fabric'):
        run_env_accept_shared_files(load_dax_config(), 'nope')


def test_accept_shared_files_adopts_the_host_version_over_drift(home):
    run_env_set(load_dax_config(), 'fabric', 'tenant', 'personal')
    (home / '.claude').mkdir()
    (home / '.claude' / 'CLAUDE.md').write_text('v1')
    sync_claude_shared_files('personal', 'fabric')

    tree_file = state_tree_path('personal', 'fabric') / 'CLAUDE.md'
    tree_file.write_text('edited independently in a container')
    (home / '.claude' / 'CLAUDE.md').write_text('v2')

    run_env_accept_shared_files(load_dax_config(), 'fabric')

    assert tree_file.read_text() == 'v2'
