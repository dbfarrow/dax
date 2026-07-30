"""Tests for ~/.dax.yaml round-trip preservation.

`dax creds add`, `dax creds update`, and `dax init` all rewrite the whole
config file. Under pyyaml that deleted every comment and alphabetised every
key, because `safe_load` never sees comments and `dump` defaults to
`sort_keys=True`. The commented-out feature line `#- claude_tenant_state` is
the case that made this urgent: it is a toggle, not decoration, and losing it
silently changes which features a project opts into.
"""
import sys

import pytest

from dax_creds.config import load_dax_config
from dax_creds.init import save_config


CONFIG_WITH_COMMENTS = """\
# dax configuration — hand-maintained, order matters for readability
defaults:
  image: dax-base

# Credentials are delivered by the daemon, never mounted.
credentials:
  claude-personal-fabric:
    provider: claude
  github-dfarrow:
    provider: github

features:
- claude
# Opt-in per project; left off until the shared plugin migration lands.
#- claude_tenant_state
- ssh

projects:
  fabric:
    dir: /Users/dfarrow/src/fabric
    tenant: personal
    creds:
    - claude-personal-fabric
"""


def _write(tmp_path, text):
    (tmp_path / '.dax.yaml').write_text(text)


def _read(tmp_path):
    return (tmp_path / '.dax.yaml').read_text()


def test_comments_survive_a_load_save_cycle(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS)

    save_config(load_dax_config())

    written = _read(tmp_path)
    assert '# dax configuration — hand-maintained' in written
    assert '# Credentials are delivered by the daemon, never mounted.' in written


def test_commented_out_feature_line_survives(tmp_path, monkeypatch):
    """The toggle that motivated this fix. Losing it silently opts a project in."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS)

    save_config(load_dax_config())

    written = _read(tmp_path)
    assert '#- claude_tenant_state' in written
    assert '# Opt-in per project' in written


def test_key_order_is_not_alphabetised(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS)

    save_config(load_dax_config())

    written = _read(tmp_path)
    order = [written.index(k) for k in ('defaults:', 'credentials:', 'features:', 'projects:')]
    assert order == sorted(order), 'top-level keys were reordered'
    # pyyaml's sort_keys=True would have put credentials before defaults.
    assert written.index('credentials:') > written.index('defaults:')


def test_edits_are_applied_while_comments_are_kept(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS)

    config = load_dax_config()
    config['projects']['fabric']['tenant'] = 'ysecurity'
    config['credentials']['claude-ysecurity-fabric'] = {'provider': 'claude'}
    save_config(config)

    reloaded = load_dax_config()
    assert reloaded['projects']['fabric']['tenant'] == 'ysecurity'
    assert reloaded['credentials']['claude-ysecurity-fabric']['provider'] == 'claude'
    assert '#- claude_tenant_state' in _read(tmp_path)


def test_long_paths_are_not_line_wrapped(tmp_path, monkeypatch):
    """ruamel wraps at 80 columns by default; `dir:` values routinely exceed it."""
    monkeypatch.setenv('HOME', str(tmp_path))
    long_dir = '/Users/dfarrow/src/' + ('a' * 100)
    _write(tmp_path, 'projects:\n  p:\n    dir: {}\n'.format(long_dir))

    save_config(load_dax_config())

    assert long_dir in _read(tmp_path)
    assert load_dax_config()['projects']['p']['dir'] == long_dir


def test_saving_is_idempotent_after_the_first_write(tmp_path, monkeypatch):
    """ruamel normalises some scalars once (e.g. `True` -> `true`); it must not
    keep churning the file on every subsequent save."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS + 'multi_tenant: True\n')

    save_config(load_dax_config())
    first = _read(tmp_path)
    save_config(load_dax_config())

    assert _read(tmp_path) == first


def test_save_config_refuses_when_ruamel_is_missing(tmp_path, monkeypatch):
    """Refusing beats falling back: a pyyaml rewrite destroys the file."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS)
    config = load_dax_config()
    monkeypatch.setitem(sys.modules, 'ruamel.yaml', None)

    with pytest.raises(ImportError, match='ruamel.yaml'):
        save_config(config)

    assert _read(tmp_path) == CONFIG_WITH_COMMENTS, 'config was modified despite the refusal'


def test_load_dax_config_falls_back_to_pyyaml_when_ruamel_is_missing(tmp_path, monkeypatch):
    """Reads must keep working — `dax run` calls this and only ever reads."""
    monkeypatch.setenv('HOME', str(tmp_path))
    _write(tmp_path, CONFIG_WITH_COMMENTS)
    monkeypatch.setitem(sys.modules, 'ruamel.yaml', None)

    config = load_dax_config()

    assert config['projects']['fabric']['tenant'] == 'personal'
    assert config['features'] == ['claude', 'ssh']
