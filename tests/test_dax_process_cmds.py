"""Tests for `dax process new` (docs/design/VALIDATION.md's dax contract,
items 2 and 4): scaffold a new substrate-backed process directory and
register it as a dax env.

Every field is either given on the command line or interactively prompted —
dax never lets new-process.sh's own basic prompting fire, and never silently
defaults a field the user didn't supply. Mirrors the `monkeypatch.setattr`
style tests/test_dax_tenant_cmds.py already uses for `_prompt_tenant` rather
than mocking questionary internals directly.

Every process directory used below lives under the fake $HOME
(`home / 'proc'`, never a bare `tmp_path / 'proc'`): `_check_process_dir`
refuses anything outside $HOME, the same way `dax run` would refuse to
launch it later.
"""
import argparse

import pytest
import yaml

from dax import _read_process_types, _run_process_new
from dax_creds.config import load_dax_config


TYPES_TSV = """\
# comment line, ignored
commitment\tdiscernment\tCareer, role, or partnership decisions
ops\tops\tStructural or operational setup for a new way of working
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text('projects: {}\n')
    monkeypatch.setenv('HOME', str(home))
    return home


@pytest.fixture
def substrate(tmp_path):
    root = tmp_path / 'virgil'
    (root / 'shared').mkdir(parents=True)
    (root / 'shared' / 'process-types.tsv').write_text(TYPES_TSV)
    # The real new-process.sh; here it's just a script recording invocation
    # and always succeeding, since new-process.sh's own behavior is not what
    # this file tests (docs/design/scripts/new-process.sh is untracked, not
    # available at test time).
    script = root / 'shared' / 'new-process.sh'
    script.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/../../last-call.txt"\n')
    script.chmod(0o755)
    return root


@pytest.fixture(autouse=True)
def _confirm_proceed(monkeypatch):
    """The "Proceed?" gate defaults to True across this file — tests that
    care about the decline path or --dry-run override this explicitly."""
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda prompt, default=False: True)


def _args(**overrides):
    base = dict(name='demo', dir=None, tenant=None, substrate=None,
                title=None, type=None, git=False, no_git=False, dry_run=False)
    base.update(overrides)
    return argparse.Namespace(**base)


def _last_call(tmp_path):
    return (tmp_path / 'last-call.txt').read_text().splitlines()


# --- fully flag-driven: no field-resolution prompting -----------------------

def test_fully_flag_driven_invokes_new_process_sh_with_resolved_flags(
        home, substrate, tmp_path, monkeypatch):
    def fail_if_prompted(*a, **k):
        raise AssertionError('should not prompt when every flag is given')
    monkeypatch.setattr('dax._prompt_tenant', fail_if_prompted)
    monkeypatch.setattr('dax_creds.init._prompt', fail_if_prompted)
    monkeypatch.setattr('dax_creds.init._q_select', fail_if_prompted)

    process_dir = home / 'proc'
    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(process_dir), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', git=False, no_git=True))

    call = _last_call(tmp_path)
    assert call == ['--dir', str(process_dir), '--title', 'Demo',
                     '--type', 'ops', '--no-git']


def test_registers_project_with_resolved_fields(home, substrate, tmp_path):
    process_dir = home / 'proc'
    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(process_dir), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True))

    project = load_dax_config()['projects']['demo']
    assert project['dir'] == str(process_dir)
    assert project['tenant'] == 'acme'
    assert project['substrate'] == str(substrate)
    assert project['creds'] == ['claude']


def test_features_always_ends_up_substrate_and_claude_tenant_state(
        home, substrate, tmp_path):
    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True))

    project = load_dax_config()['projects']['demo']
    assert project['features'] == ['substrate', 'claude_tenant_state']


def test_git_flag_is_passed_through(home, substrate, tmp_path):
    process_dir = home / 'proc'
    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(process_dir), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', git=True))

    assert '--git' in _last_call(tmp_path)


def test_git_and_no_git_together_is_an_error(home, substrate, tmp_path, capsys):
    config = load_dax_config()
    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
            title='Demo', type='ops', git=True, no_git=True))
    assert 'not both' in capsys.readouterr().out


# --- prompting for missing fields -------------------------------------------

def test_missing_dir_is_prompted(home, substrate, tmp_path, monkeypatch):
    monkeypatch.setattr('dax_creds.init._prompt',
                        lambda prompt, default=None: str(home / 'prompted-dir')
                        if prompt == 'Process directory' else default)
    config = load_dax_config()
    _run_process_new(config, _args(
        tenant='acme', substrate=str(substrate), title='Demo', type='ops', no_git=True))

    assert load_dax_config()['projects']['demo']['dir'] == str(home / 'prompted-dir')


def test_missing_tenant_uses_prompt_tenant(home, substrate, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known: calls.append(subdir) or 'prompted-tenant')

    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), substrate=str(substrate), title='Demo',
        type='ops', no_git=True))

    assert load_dax_config()['projects']['demo']['tenant'] == 'prompted-tenant'
    assert len(calls) == 1


def test_missing_substrate_prompt_defaults_to_top_level_config(
        home, substrate, tmp_path, monkeypatch):
    config = load_dax_config()
    config['substrate'] = str(substrate)

    seen_defaults = {}

    def fake_prompt(prompt, default=None):
        seen_defaults[prompt] = default
        if prompt == 'Substrate path':
            return default
        if prompt == 'Process directory':
            return str(home / 'proc')
        if prompt == 'Process title':
            return 'Demo'
        return default
    monkeypatch.setattr('dax_creds.init._prompt', fake_prompt)

    _run_process_new(config, _args(tenant='acme', type='ops', no_git=True))

    assert seen_defaults['Substrate path'] == str(substrate)


def test_missing_title_is_prompted(home, substrate, tmp_path, monkeypatch):
    monkeypatch.setattr('dax_creds.init._prompt',
                        lambda prompt, default=None: 'Prompted Title'
                        if prompt == 'Process title' else default)
    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
        type='ops', no_git=True))

    call = _last_call(tmp_path)
    assert 'Prompted Title' in call


def test_missing_type_is_selected_from_tsv(home, substrate, tmp_path, monkeypatch):
    monkeypatch.setattr('dax_creds.init._q_select', lambda prompt, choices: choices[0].value)

    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
        title='Demo', no_git=True))

    call = _last_call(tmp_path)
    assert 'commitment' in call  # first row of TYPES_TSV


def test_missing_git_decision_is_confirmed(home, substrate, tmp_path, monkeypatch):
    confirms = []
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda prompt, default=False:
                        confirms.append(prompt) or True)

    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops'))

    assert '--git' in _last_call(tmp_path)
    assert 'git?' in confirms  # distinct from the later "Proceed?" confirm


# --- confirmation summary + dry run ------------------------------------------

def test_dry_run_prints_summary_and_changes_nothing(home, substrate, tmp_path, capsys):
    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True, dry_run=True))

    out = capsys.readouterr().out
    assert str(home / 'proc') in out
    assert str(substrate) in out
    assert 'acme' in out
    assert 'dry run' in out
    assert not (tmp_path / 'last-call.txt').exists()
    assert 'demo' not in load_dax_config().get('projects', {})


def test_declining_the_proceed_prompt_changes_nothing(
        home, substrate, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda prompt, default=False: False)

    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True))

    assert 'aborted' in capsys.readouterr().out
    assert not (tmp_path / 'last-call.txt').exists()
    assert 'demo' not in load_dax_config().get('projects', {})


def test_summary_uses_full_resolved_paths_not_relative_shorthand(
        home, substrate, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(home)
    config = load_dax_config()
    _run_process_new(config, _args(
        dir='proc', tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True, dry_run=True))

    out = capsys.readouterr().out
    assert str(home / 'proc') in out
    assert './proc' not in out


# --- guardrails: bad directory choices ---------------------------------------

def test_relative_dir_is_resolved_to_absolute(home, substrate, tmp_path, monkeypatch):
    monkeypatch.chdir(home)
    config = load_dax_config()
    _run_process_new(config, _args(
        dir='proc', tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True))

    assert load_dax_config()['projects']['demo']['dir'] == str(home / 'proc')


def test_dir_outside_home_is_refused(home, substrate, tmp_path, capsys):
    outside = tmp_path / 'outside-home'
    outside.mkdir()
    config = load_dax_config()
    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(outside), tenant='acme', substrate=str(substrate),
            title='Demo', type='ops', no_git=True))

    assert 'not under your home directory' in capsys.readouterr().out
    assert 'demo' not in load_dax_config().get('projects', {})


def test_dir_colliding_with_existing_project_is_refused(home, substrate, tmp_path, capsys):
    existing_dir = home / 'existing'
    existing_dir.mkdir()
    config = load_dax_config()
    config['projects']['other'] = {'dir': str(existing_dir), 'creds': []}

    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(existing_dir), tenant='acme', substrate=str(substrate),
            title='Demo', type='ops', no_git=True))

    assert 'already registered as project' in capsys.readouterr().out


def test_dir_nested_inside_existing_project_is_refused(home, substrate, tmp_path, capsys):
    existing_dir = home / 'existing'
    existing_dir.mkdir()
    config = load_dax_config()
    config['projects']['other'] = {'dir': str(existing_dir), 'creds': []}

    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(existing_dir / 'nested'), tenant='acme', substrate=str(substrate),
            title='Demo', type='ops', no_git=True))

    assert 'inside already-registered project' in capsys.readouterr().out


def test_dir_enclosing_an_existing_project_is_refused(home, substrate, tmp_path, capsys):
    existing_dir = home / 'parent' / 'existing'
    existing_dir.mkdir(parents=True)
    config = load_dax_config()
    config['projects']['other'] = {'dir': str(existing_dir), 'creds': []}

    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(home / 'parent'), tenant='acme', substrate=str(substrate),
            title='Demo', type='ops', no_git=True))

    assert 'would enclose already-registered project' in capsys.readouterr().out


def test_non_empty_dir_is_noted_but_not_blocked(home, substrate, tmp_path, capsys):
    process_dir = home / 'proc'
    process_dir.mkdir()
    (process_dir / 'something.txt').write_text('pre-existing')

    config = load_dax_config()
    _run_process_new(config, _args(
        dir=str(process_dir), tenant='acme', substrate=str(substrate),
        title='Demo', type='ops', no_git=True))

    assert 'demo' in load_dax_config().get('projects', {})


# --- validation and failure handling -----------------------------------------

def test_unknown_type_flag_is_rejected(home, substrate, tmp_path, capsys):
    config = load_dax_config()
    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(home / 'proc'), tenant='acme', substrate=str(substrate),
            title='Demo', type='not-a-real-type', no_git=True))

    assert 'unknown type' in capsys.readouterr().out
    assert 'demo' not in load_dax_config().get('projects', {})


def test_missing_process_types_tsv_errors_clearly(home, tmp_path, capsys):
    bad_substrate = tmp_path / 'not-a-substrate'
    bad_substrate.mkdir()
    config = load_dax_config()
    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(home / 'proc'), tenant='acme', substrate=str(bad_substrate),
            title='Demo', type='ops', no_git=True))

    assert 'process-types.tsv' in capsys.readouterr().out


def test_new_process_sh_failure_aborts_registration(home, tmp_path, monkeypatch, capsys):
    root = tmp_path / 'virgil'
    (root / 'shared').mkdir(parents=True)
    (root / 'shared' / 'process-types.tsv').write_text(TYPES_TSV)
    script = root / 'shared' / 'new-process.sh'
    script.write_text('#!/bin/sh\nexit 1\n')
    script.chmod(0o755)

    config = load_dax_config()
    with pytest.raises(SystemExit):
        _run_process_new(config, _args(
            dir=str(home / 'proc'), tenant='acme', substrate=str(root),
            title='Demo', type='ops', no_git=True))

    assert 'new-process.sh failed' in capsys.readouterr().out
    assert 'demo' not in load_dax_config().get('projects', {})


def test_read_process_types_ignores_comments(substrate):
    types = _read_process_types(substrate)
    assert ('ops', 'ops', 'Structural or operational setup for a new way of working') in types
    assert not any(t[0].startswith('#') for t in types)


def test_read_process_types_errors_when_tsv_missing(tmp_path):
    with pytest.raises(ValueError, match='process-types.tsv'):
        _read_process_types(tmp_path / 'nope')
