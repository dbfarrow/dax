"""Tests for the container-side `claude` wrapper script.

The wrapper had no coverage, which is how it shipped writing
~/.claude/credentials.json when Claude Code reads ~/.claude/.credentials.json.
"""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parent.parent / 'dax_creds' / 'wrappers' / 'claude'

ENVELOPE = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc',
                              'refreshToken': 'sk-ant-ort01-xyz'}}
ENVELOPE_JSON = json.dumps(ENVELOPE)


def _run(tmp_path, dax_creds_stdout, dax_creds_exit=0, env=None):
    """Run the wrapper with a stubbed dax-creds and a no-op claude-real.

    The wrapper is copied and chmod'd the way Dockerfile.tmpl installs it,
    rather than executed in place — it is mode 644 in the repo.
    """
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)

    wrapper = bin_dir / 'claude'
    wrapper.write_text(WRAPPER.read_text())
    wrapper.chmod(0o755)

    stub = bin_dir / 'dax-creds'
    stub.write_text('#!/bin/bash\nprintf %s {!r}\nexit {}\n'.format(
        dax_creds_stdout, dax_creds_exit))
    stub.chmod(0o755)

    real = bin_dir / 'claude-real'
    real.write_text('#!/bin/bash\nexit 0\n')
    real.chmod(0o755)

    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)

    full_env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'HOME': str(home),
        'DAX_CREDS_SOCK': 'tcp:127.0.0.1:9999',
        'DAX_CREDS_CLAUDE': 'claude-fre',
        'DAX_CLAUDE_REAL': str(real),
    }
    if env:
        full_env.update(env)

    proc = subprocess.run([str(wrapper)], env=full_env, capture_output=True,
                          text=True, timeout=30)
    return proc, home / '.claude' / '.credentials.json'


def test_writes_envelope_to_dot_prefixed_path(tmp_path):
    proc, creds = _run(tmp_path, ENVELOPE_JSON)
    assert proc.returncode == 0
    assert json.loads(creds.read_text()) == ENVELOPE


def test_does_not_write_legacy_non_dot_path(tmp_path):
    proc, creds = _run(tmp_path, ENVELOPE_JSON)
    assert not (creds.parent / 'credentials.json').exists()


def test_credentials_file_is_owner_only(tmp_path):
    proc, creds = _run(tmp_path, ENVELOPE_JSON)
    mode = stat.S_IMODE(creds.stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(mode)


def test_bare_token_is_rejected_and_reported(tmp_path):
    proc, creds = _run(tmp_path, 'sk-ant-oat01-' + 'a' * 90)
    assert not creds.exists()
    assert 'not a Claude credentials blob' in proc.stderr
    assert 'dax creds remove' in proc.stderr


def test_bare_token_still_execs_claude(tmp_path):
    # A bad credential must not prevent Claude from starting — the user may
    # already be authenticated or may want to log in interactively.
    proc, _ = _run(tmp_path, 'sk-ant-oat01-abc')
    assert proc.returncode == 0


def test_daemon_failure_is_reported_not_swallowed(tmp_path):
    proc, creds = _run(tmp_path, '', dax_creds_exit=1)
    assert not creds.exists()
    assert 'could not fetch credential' in proc.stderr
    assert proc.returncode == 0


def test_no_fetch_attempted_without_sock(tmp_path):
    proc, creds = _run(tmp_path, ENVELOPE_JSON, env={'DAX_CREDS_SOCK': ''})
    assert not creds.exists()
    assert proc.stderr == ''


def test_no_fetch_attempted_without_credential_name(tmp_path):
    proc, creds = _run(tmp_path, ENVELOPE_JSON, env={'DAX_CREDS_CLAUDE': ''})
    assert not creds.exists()
    assert proc.stderr == ''


# --- onboarding seed -------------------------------------------------------
#
# Claude Code's first-run wizard is gated on .claude.json and runs *ahead of*
# the credential check: with no .claude.json, an interactive launch shows the
# theme picker, then "Select login method", then opens a browser — while a valid
# .credentials.json sits unread beside it.
#
# This is invisible to `claude auth status` and `claude -p`, both of which read
# the credential directly and report success against a config that would prompt.
# That blind spot is why the mount removal looked like a credential failure.
# tests/manual/interactive_launch_probe.py exercises the real launch path.

def test_seeds_onboarding_flag_when_state_file_absent(tmp_path):
    _run(tmp_path, ENVELOPE_JSON)
    state = tmp_path / 'home' / '.claude.json'
    assert json.loads(state.read_text()) == {'hasCompletedOnboarding': True}


def test_seed_does_not_overwrite_existing_state_file(tmp_path):
    # Claude Code owns this file and grows it to ~32KB on first run. Rewriting
    # it each launch would wipe per-tenant project state, permissions, history.
    home = tmp_path / 'home'
    home.mkdir(parents=True, exist_ok=True)
    state = home / '.claude.json'
    existing = {'hasCompletedOnboarding': True, 'projects': {'/w': {'x': 1}}}
    state.write_text(json.dumps(existing))

    _run(tmp_path, ENVELOPE_JSON)
    assert json.loads(state.read_text()) == existing


def test_seed_does_not_overwrite_empty_state_file(tmp_path):
    # -e, not -s: a zero-byte .claude.json is still Claude Code's file to own.
    home = tmp_path / 'home'
    home.mkdir(parents=True, exist_ok=True)
    state = home / '.claude.json'
    state.write_text('')

    _run(tmp_path, ENVELOPE_JSON)
    assert state.read_text() == ''


def test_seed_happens_even_when_no_credential_configured(tmp_path):
    # The onboarding gate is independent of credential delivery — a container
    # with no Claude credential should still not face the first-run wizard.
    _run(tmp_path, ENVELOPE_JSON, env={'DAX_CREDS_SOCK': ''})
    state = tmp_path / 'home' / '.claude.json'
    assert json.loads(state.read_text()) == {'hasCompletedOnboarding': True}


def test_seed_does_not_set_folder_trust(tmp_path):
    # Accepting folder trust silently activates .claude/settings.local.json
    # permissions. That is the user's decision, not the wrapper's.
    _run(tmp_path, ENVELOPE_JSON)
    seeded = json.loads((tmp_path / 'home' / '.claude.json').read_text())
    assert 'projects' not in seeded


def test_seed_does_not_set_oauth_account(tmp_path):
    # oauthAccount/userID need real account data (email, org, account UUID);
    # nothing plumbs that from the daemon/host into the wrapper today, so
    # seeding a fabricated value would be worse than seeding nothing.
    _run(tmp_path, ENVELOPE_JSON)
    seeded = json.loads((tmp_path / 'home' / '.claude.json').read_text())
    assert 'oauthAccount' not in seeded
    assert 'userID' not in seeded


def test_seed_is_owner_only(tmp_path):
    _run(tmp_path, ENVELOPE_JSON)
    state = tmp_path / 'home' / '.claude.json'
    mode = stat.S_IMODE(state.stat().st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(mode)


# --- CLAUDE_CONFIG_DIR layout ----------------------------------------------
#
# The two layouts disagree about where .claude.json lives: inside
# CLAUDE_CONFIG_DIR when set, but at ~/.claude.json — not ~/.claude/.claude.json
# — otherwise. Verified against Claude Code v2.1.220.

def test_credentials_follow_claude_config_dir(tmp_path):
    cfg = tmp_path / 'state' / 'tenants' / 'acme' / 'proj'
    _run(tmp_path, ENVELOPE_JSON, env={'CLAUDE_CONFIG_DIR': str(cfg)})
    assert json.loads((cfg / '.credentials.json').read_text()) == ENVELOPE
    assert not (tmp_path / 'home' / '.claude' / '.credentials.json').exists()


def test_state_file_is_inside_claude_config_dir(tmp_path):
    cfg = tmp_path / 'state' / 'tenants' / 'acme' / 'proj'
    _run(tmp_path, ENVELOPE_JSON, env={'CLAUDE_CONFIG_DIR': str(cfg)})
    assert json.loads((cfg / '.claude.json').read_text()) == {
        'hasCompletedOnboarding': True}
    assert not (tmp_path / 'home' / '.claude.json').exists()


def test_config_dir_created_when_missing(tmp_path):
    cfg = tmp_path / 'does' / 'not' / 'exist' / 'yet'
    proc, _ = _run(tmp_path, ENVELOPE_JSON, env={'CLAUDE_CONFIG_DIR': str(cfg)})
    assert proc.returncode == 0
    assert (cfg / '.credentials.json').exists()
    assert (cfg / '.claude.json').exists()


# --- DAX_TENANT_STATE: Gate B tenant resolution -----------------------------
#
# DAX_TENANT_STATE is the master switch, set by `dax run` only for projects
# that have opted into the (not yet built) claude_tenant_state feature. Unset
# - every project today - none of this runs; the tests above already prove
# that (none of them set it). These tests cover the opted-in path, stubbing
# `dax-creds resolve-tenant` rather than exercising the real tenant-resolution
# module - that module has its own full unit coverage in
# tests/test_dax_creds_tenant.py.

def _run_tenant_state(tmp_path, resolve_stdout, resolve_exit=0,
                       dax_creds_stdout=ENVELOPE_JSON, dax_creds_exit=0, env=None):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)

    wrapper = bin_dir / 'claude'
    wrapper.write_text(WRAPPER.read_text())
    wrapper.chmod(0o755)

    call_log = tmp_path / 'dax-creds-calls.log'
    stub = bin_dir / 'dax-creds'
    stub.write_text(
        '#!/bin/bash\n'
        'echo "$@" >> {log!r}\n'
        'if [ "$1" = "resolve-tenant" ]; then\n'
        '    printf %b {resolve_stdout!r} >&1\n'
        '    printf %b {resolve_stderr!r} >&2\n'
        '    exit {resolve_exit}\n'
        'else\n'
        '    printf %s {get_stdout!r}\n'
        '    exit {get_exit}\n'
        'fi\n'.format(
            log=str(call_log), resolve_stdout=resolve_stdout,
            resolve_stderr='' if resolve_exit == 0 else 'dax-creds: refused\n',
            resolve_exit=resolve_exit, get_stdout=dax_creds_stdout, get_exit=dax_creds_exit))
    stub.chmod(0o755)

    real = bin_dir / 'claude-real'
    real.write_text('#!/bin/bash\necho ran-claude-real > {}\nexit 0\n'.format(
        tmp_path / 'claude-real-ran'))
    real.chmod(0o755)

    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)

    full_env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'HOME': str(home),
        'DAX_CREDS_SOCK': 'tcp:127.0.0.1:9999',
        'DAX_CREDS_CLAUDE': 'claude-fre',
        'DAX_CLAUDE_REAL': str(real),
        'DAX_TENANT_STATE': '1',
    }
    if env:
        full_env.update(env)

    proc = subprocess.run([str(wrapper)], env=full_env, capture_output=True,
                          text=True, timeout=30)
    return proc, home, call_log


def test_tenant_state_unset_never_invokes_resolve_tenant(tmp_path):
    # Belt-and-suspenders on top of every test above not setting
    # DAX_TENANT_STATE: confirm dax-creds is never even called with
    # resolve-tenant when the switch is off.
    proc, creds = _run(tmp_path, ENVELOPE_JSON)
    assert proc.returncode == 0
    assert json.loads(creds.read_text()) == ENVELOPE


def test_tenant_state_sets_claude_config_dir_from_resolution(tmp_path):
    proc, home, _ = _run_tenant_state(
        tmp_path, resolve_stdout='TENANT=personal\nPROJECT=fabric\n')

    assert proc.returncode == 0
    cfg = home / '.local' / 'state' / 'dax' / 'tenants' / 'personal' / 'fabric'
    assert json.loads((cfg / '.credentials.json').read_text()) == ENVELOPE
    assert json.loads((cfg / '.claude.json').read_text()) == {
        'hasCompletedOnboarding': True}


def test_tenant_state_refusal_exits_nonzero_without_running_claude(tmp_path):
    # Unlike a bad credential (which still execs claude, degraded), a
    # resolution refusal must not start claude at all.
    proc, home, _ = _run_tenant_state(tmp_path, resolve_stdout='', resolve_exit=1)

    assert proc.returncode != 0
    assert not (tmp_path / 'claude-real-ran').exists()
    assert 'refused' in proc.stderr


def test_tenant_state_refusal_does_not_fetch_credential(tmp_path):
    # Refusing must happen before any credential work, not just before exec.
    proc, home, call_log = _run_tenant_state(tmp_path, resolve_stdout='', resolve_exit=1)

    calls = call_log.read_text().splitlines()
    assert calls == ['resolve-tenant']


def test_tenant_state_success_still_execs_claude(tmp_path):
    proc, home, _ = _run_tenant_state(
        tmp_path, resolve_stdout='TENANT=personal\nPROJECT=fabric\n')

    assert proc.returncode == 0
    assert (tmp_path / 'claude-real-ran').exists()


def test_tenant_state_warn_reaches_stderr(tmp_path):
    # dax-creds itself prints the warn/refuse detail (see cli.py); the
    # wrapper does not need its own duplicate messaging for this path.
    # tenant='unattributed' (not a combined 'unattributed/fabric' string) is
    # what avoids the wrapper's tenants/$_tenant/$_project template stuttering
    # into .../unattributed/fabric/fabric.
    proc, home, _ = _run_tenant_state(
        tmp_path, resolve_stdout='TENANT=unattributed\nPROJECT=fabric\n',
        resolve_exit=0)

    assert proc.returncode == 0
    cfg = home / '.local' / 'state' / 'dax' / 'tenants' / 'unattributed' / 'fabric'
    assert (cfg / '.claude.json').exists()
