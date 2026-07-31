"""`feature_claude_tenant_state` mounts this env's state tree at `~/.claude`.

Decision B. The feature previously mounted trees at
`~/.local/state/dax/tenants/<t>/<p>` and left the container-side `claude` wrapper
to compute `CLAUDE_CONFIG_DIR` from cwd — the multi-tenant shape, where one repo
could hold several tenants. Each env is single-tenant now, so the path is knowable
before launch.

It had no tests at all, which is part of why the shape drifted from the design.
"""
import os

import pytest

import dax as dax_module
from dax import _CLAUDE_HOST_SHARED_DIRS, feature_claude_tenant_state


@pytest.fixture
def config(tmp_path, monkeypatch):
    """A dax run config, with HOME redirected so host paths are inspectable."""
    monkeypatch.setenv('HOME', str(tmp_path))
    return {'workdir_name': 'fabric', 'tenant': 'personal',
            'cwd': '/repos/fabric', 'user': 'dfarrow'}


def volumes(opts):
    return [o[len('--volume='):] for o in opts if o.startswith('--volume=')]


def env_vars(opts):
    out = {}
    for flag, value in zip(opts, opts[1:]):
        if flag == '-e' and '=' in value:
            key, val = value.split('=', 1)
            out[key] = val
    return out


def test_the_tree_mounts_at_the_config_dir_itself(config):
    """Not at a `~/.local/state/dax/...` path inside the container: Claude Code
    scans `~/.claude`, and the whole point is that it finds this env's tree there."""
    vols = volumes(feature_claude_tenant_state(config))

    assert vols[0].endswith(':/home/dfarrow/.claude')
    assert vols[0].startswith(os.path.expanduser(
        '~/.local/state/dax/tenants/personal/fabric'))


def test_config_dir_is_a_constant_pointing_at_the_mount(config):
    """Kept only because it relocates .claude.json *into* the tree — unset, that
    file lands at ~/.claude.json, outside the mount, and is lost on teardown."""
    assert env_vars(feature_claude_tenant_state(config))['CLAUDE_CONFIG_DIR'] == \
        '/home/dfarrow/.claude'


def test_no_tenant_state_switch_is_exported(config):
    """`dax run` must not set DAX_TENANT_STATE any more: a wrapper from an image
    built before decision B still contains Gate B and would resolve its own path,
    overriding the mount. Not setting it is what makes this work unrebuilt."""
    exported = env_vars(feature_claude_tenant_state(config))

    assert 'DAX_TENANT_STATE' not in exported
    assert 'DAX_PROJECT_NAME' not in exported
    assert 'DAX_MULTI_TENANT' not in exported
    assert 'DAX_TENANT_SUBDIR' not in exported


def test_the_tenant_comes_from_the_registry_not_the_filesystem(config, tmp_path):
    """Decision A: a declared label on the env's ~/.dax.yaml entry. A stray
    .dax-tenant file in the repo must have no say."""
    (tmp_path / '.dax-tenant').write_text('some-other-tenant\n')
    config['cwd'] = str(tmp_path)

    assert 'tenants/personal/fabric' in volumes(feature_claude_tenant_state(config))[0]


def test_a_missing_tenant_refuses_rather_than_pooling(config, capsys):
    """Falling back to a default would pool two envs' state, which is the exact
    failure this design exists to prevent (decision C2's rule, applied to state)."""
    config['tenant'] = None

    with pytest.raises(SystemExit) as excinfo:
        feature_claude_tenant_state(config)

    assert excinfo.value.code != 0
    out = capsys.readouterr().out
    assert 'dax env set fabric tenant' in out


# --- the two host directories (decision B2) ---------------------------------

def test_host_commands_and_plugins_nest_inside_the_tree(config, tmp_path):
    for d in _CLAUDE_HOST_SHARED_DIRS:
        (tmp_path / '.claude' / d).mkdir(parents=True)

    vols = volumes(feature_claude_tenant_state(config))

    assert f'{tmp_path}/.claude/commands:/home/dfarrow/.claude/commands' in vols
    assert f'{tmp_path}/.claude/plugins:/home/dfarrow/.claude/plugins' in vols


def test_they_are_mounted_from_the_host_claude_dir_not_a_shared_tree(config, tmp_path):
    """Decision B2: no `shared/` copy, so there is one source of truth and no
    seeding step that can silently leave a project with empty directories."""
    (tmp_path / '.claude' / 'commands').mkdir(parents=True)

    vols = volumes(feature_claude_tenant_state(config))

    assert not any('/state/dax/shared/' in v for v in vols)


def test_the_tree_mount_comes_first(config, tmp_path):
    """Docker orders by destination depth, but emitting the parent first keeps the
    intent legible in `docker run` output and in any future ordering change."""
    (tmp_path / '.claude' / 'commands').mkdir(parents=True)

    vols = volumes(feature_claude_tenant_state(config))

    assert vols[0].endswith(':/home/dfarrow/.claude')


def test_absent_host_directories_are_skipped(config):
    """A host with no commands of its own should not acquire an empty
    ~/.claude/commands as a side effect of running a container."""
    vols = volumes(feature_claude_tenant_state(config))

    assert len(vols) == 1


def test_skills_and_cache_are_not_shared(config, tmp_path):
    """Skills arrive from the discernment sidecar (decision D), so a copy here
    goes stale. Cache is derived and the only one where fetched content can turn
    account-specific."""
    for d in ('skills', 'cache', 'commands'):
        (tmp_path / '.claude' / d).mkdir(parents=True)

    vols = volumes(feature_claude_tenant_state(config))

    assert not any(v.endswith('/skills') or v.endswith('/cache') for v in vols)
    assert 'skills' not in _CLAUDE_HOST_SHARED_DIRS
    assert 'cache' not in _CLAUDE_HOST_SHARED_DIRS


def test_each_env_gets_its_own_tree(config, monkeypatch):
    """The property the whole design is for: two envs must not share a path."""
    fabric = volumes(feature_claude_tenant_state(config))[0]

    other = dict(config, workdir_name='dax')
    assert volumes(feature_claude_tenant_state(other))[0] != fabric


def test_a_different_tenant_changes_the_path(config):
    """`tenant` is the outer key, so ending an engagement is one rm -rf."""
    a = volumes(feature_claude_tenant_state(config))[0]
    b = volumes(feature_claude_tenant_state(dict(config, tenant='acme')))[0]

    assert '/tenants/personal/' in a
    assert '/tenants/acme/' in b
