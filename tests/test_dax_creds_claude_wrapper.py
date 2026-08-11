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


def _run(tmp_path, dax_creds_stdout, dax_creds_exit=0, env=None, cwd=None):
    """Run the wrapper with a stubbed dax-creds and a no-op claude-real.

    The wrapper is copied and chmod'd the way Dockerfile.tmpl installs it,
    rather than executed in place — it is mode 644 in the repo.

    A stub `tmux` is always on PATH too, logging its own argv to `tmp_path /
    'tmux.log'` (one line per invocation) rather than doing anything real —
    the wrapper only ever calls it when $TMUX is set, so its absence from
    most tests' env means it's simply never invoked.
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

    tmux_log = tmp_path / 'tmux.log'
    tmux = bin_dir / 'tmux'
    tmux.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> {!r}\nexit 0\n'.format(str(tmux_log)))
    tmux.chmod(0o755)

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
                          text=True, timeout=30, cwd=cwd)
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


def test_renames_tmux_window_to_the_current_directory_when_inside_tmux(tmp_path):
    project_dir = tmp_path / 'myproject'
    project_dir.mkdir()

    proc, _ = _run(tmp_path, ENVELOPE_JSON, env={'TMUX': '/tmp/fake-tmux-socket,0,0'},
                   cwd=str(project_dir))

    assert proc.returncode == 0
    assert (tmp_path / 'tmux.log').read_text() == 'rename-window myproject\n'


def test_does_not_touch_tmux_when_not_running_inside_it(tmp_path):
    proc, _ = _run(tmp_path, ENVELOPE_JSON, env={'TMUX': ''})
    assert proc.returncode == 0
    assert not (tmp_path / 'tmux.log').exists()


def test_still_execs_claude_after_renaming_the_window(tmp_path):
    # The rename must never block or replace the actual claude launch.
    proc, _ = _run(tmp_path, ENVELOPE_JSON, env={'TMUX': '/tmp/fake-tmux-socket,0,0'})
    assert proc.returncode == 0


# --- inject-if-absent ------------------------------------------------------
#
# The Keychain copy is a bootstrap, not the running source of truth. Claude Code
# refreshes the credential into its own state tree and nothing propagates back,
# so re-writing the daemon snapshot on every launch would clobber a refreshed
# token with a stale one — and if refresh tokens rotate on use, that stale token
# is already dead. See decision C of docs/design/2026-07-27-tenant-isolation.md.

REFRESHED = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-rotated',
                               'refreshToken': 'sk-ant-ort01-rotated'}}


def _seed_credential(tmp_path, content):
    creds = tmp_path / 'home' / '.claude' / '.credentials.json'
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(content)
    return creds


def test_existing_credential_is_not_overwritten(tmp_path):
    """A token Claude Code refreshed in-tree must survive the next launch."""
    _seed_credential(tmp_path, json.dumps(REFRESHED))

    proc, creds = _run(tmp_path, ENVELOPE_JSON)

    assert proc.returncode == 0
    assert json.loads(creds.read_text()) == REFRESHED


def test_existing_credential_skips_the_daemon_fetch(tmp_path):
    """Nothing is fetched when the tree already has a credential — so a daemon
    that is down or holding a dead snapshot cannot break an existing session."""
    _seed_credential(tmp_path, json.dumps(REFRESHED))

    proc, creds = _run(tmp_path, '', dax_creds_exit=1)

    assert proc.returncode == 0
    assert 'could not fetch' not in proc.stderr
    assert json.loads(creds.read_text()) == REFRESHED


def test_empty_credential_file_is_treated_as_absent(tmp_path):
    """An interrupted write leaves a zero-byte file; that must not wedge auth."""
    _seed_credential(tmp_path, '')

    proc, creds = _run(tmp_path, ENVELOPE_JSON)

    assert json.loads(creds.read_text()) == ENVELOPE


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

# --- CLAUDE_CONFIG_DIR is handed down, never computed ------------------------
#
# Gate B used to live here: DAX_TENANT_STATE switched on a `dax-creds
# resolve-tenant` call that derived the path from cwd. That belonged to the
# multi-tenant model, where one repo could hold several tenants and the answer
# depended on where the user was standing. Each env is single-tenant now, so
# `dax run` knows the path before launch and sets it (decision B).


def _run_logging_calls(tmp_path, env=None):
    """Like `_run`, but records every dax-creds invocation."""
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
        'printf %s {out!r}\n'.format(log=str(call_log), out=ENVELOPE_JSON))
    stub.chmod(0o755)

    real = bin_dir / 'claude-real'
    real.write_text('#!/bin/bash\necho ran > {}\nexit 0\n'.format(
        tmp_path / 'claude-real-ran'))
    real.chmod(0o755)

    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)

    full_env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'HOME': str(home),
        'DAX_CREDS_SOCK': 'tcp:127.0.0.1:9999',
        'DAX_CREDS_CLAUDE': 'claude-personal-fabric',
        'DAX_CLAUDE_REAL': str(real),
    }
    if env:
        full_env.update(env)

    proc = subprocess.run([str(wrapper)], env=full_env, capture_output=True,
                          text=True, timeout=30)
    return proc, home, call_log


def test_resolve_tenant_is_never_invoked(tmp_path):
    """The wrapper must not consult tenant resolution at all any more."""
    proc, _, call_log = _run_logging_calls(tmp_path)

    assert proc.returncode == 0
    assert call_log.read_text().splitlines() == ['get claude-personal-fabric']


def test_dax_tenant_state_no_longer_changes_anything(tmp_path):
    """An image built before decision B carries a wrapper that still contains
    Gate B, so `dax run` deliberately stopped setting this variable. Setting it
    here proves the new wrapper ignores it rather than resolving a path."""
    cfg = tmp_path / 'home' / '.claude'
    proc, home, call_log = _run_logging_calls(
        tmp_path, env={'DAX_TENANT_STATE': '1',
                       'CLAUDE_CONFIG_DIR': str(cfg)})

    assert proc.returncode == 0
    assert call_log.read_text().splitlines() == ['get claude-personal-fabric']
    assert json.loads((cfg / '.credentials.json').read_text()) == ENVELOPE


def test_a_handed_down_config_dir_is_used_verbatim(tmp_path):
    """`dax run` mounts the state tree at exactly this path, so the wrapper
    writing anywhere else would silently miss the mount."""
    cfg = tmp_path / 'home' / '.claude'
    proc, home, _ = _run_logging_calls(tmp_path, env={'CLAUDE_CONFIG_DIR': str(cfg)})

    assert proc.returncode == 0
    assert json.loads((cfg / '.claude.json').read_text()) == {
        'hasCompletedOnboarding': True}
    assert not (home / '.claude.json').exists()


def test_claude_still_runs(tmp_path):
    proc, _, _ = _run_logging_calls(tmp_path)

    assert proc.returncode == 0
    assert (tmp_path / 'claude-real-ran').exists()


# --- substrate gate + settings delivery (docs/design/VALIDATION.md) ---------
#
# `--check` only, never a bare `wire.sh` — the self-heal already ran once at
# container boot (dax_creds/wrappers/entrypoint.sh). These tests fake
# `shared/wire.sh` directly rather than the real virgil script, since only
# its documented `--check` exit-code contract (0 / 1 / the shell's own 127
# when it's missing) is part of dax's side of the contract.

def _run_with_substrate(tmp_path, substrate_root=None, wire_check_rc=0,
                        wire_missing=False, extra_argv=None, env=None,
                        settings_content=None):
    """Like `_run`, but wires up $SUBSTRATE_ROOT and records claude-real's argv."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir(exist_ok=True)

    wrapper = bin_dir / 'claude'
    wrapper.write_text(WRAPPER.read_text())
    wrapper.chmod(0o755)

    real = bin_dir / 'claude-real'
    argv_log = tmp_path / 'claude-real-argv.txt'
    # Not a bare `printf '%s\n' "$@" > log`: with zero args that still prints
    # one blank line (the format string runs once regardless), so an empty
    # argv would misleadingly read back as [''] instead of [].
    real.write_text(
        '#!/bin/bash\n: > {log!r}\nfor a in "$@"; do printf \'%s\\n\' "$a" >> {log!r}; done\n'
        'exit 0\n'.format(log=str(argv_log)))
    real.chmod(0o755)

    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)

    if settings_content is not None:
        settings = home / '.claude' / 'substrate-settings.json'
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(settings_content)

    full_env = {
        'PATH': f'{bin_dir}:/usr/bin:/bin',
        'HOME': str(home),
        'DAX_CLAUDE_REAL': str(real),
    }

    if substrate_root is None:
        substrate_root = tmp_path / 'virgil'
    if not wire_missing:
        shared = substrate_root / 'shared'
        shared.mkdir(parents=True, exist_ok=True)
        wire = shared / 'wire.sh'
        wire.write_text('#!/bin/sh\nexit {}\n'.format(wire_check_rc))
        wire.chmod(0o755)
    full_env['SUBSTRATE_ROOT'] = str(substrate_root)

    if env:
        full_env.update(env)

    argv = [str(wrapper)] + (extra_argv or [])
    proc = subprocess.run(argv, env=full_env, capture_output=True, text=True, timeout=30)
    recorded = argv_log.read_text().splitlines() if argv_log.exists() else None
    return proc, recorded


def test_no_substrate_root_skips_the_gate_entirely(tmp_path):
    """Identical behavior to before this gate existed."""
    proc, recorded = _run_with_substrate(tmp_path, wire_missing=True,
                                         env={'SUBSTRATE_ROOT': ''})
    assert proc.returncode == 0
    assert recorded == []


def test_wired_correctly_proceeds_and_delivers_settings(tmp_path):
    home = tmp_path / 'home'
    settings = home / '.claude' / 'substrate-settings.json'
    proc, recorded = _run_with_substrate(tmp_path, wire_check_rc=0, settings_content='{}')

    assert proc.returncode == 0
    assert recorded == ['--settings', str(settings)]


def test_wired_correctly_without_a_settings_fragment_still_proceeds(tmp_path):
    proc, recorded = _run_with_substrate(tmp_path, wire_check_rc=0)
    assert proc.returncode == 0
    assert recorded == []


def test_does_not_clobber_a_caller_supplied_settings_flag(tmp_path):
    proc, recorded = _run_with_substrate(
        tmp_path, wire_check_rc=0, settings_content='{}',
        extra_argv=['--settings', '/some/other/file.json'])

    assert proc.returncode == 0
    assert recorded == ['--settings', '/some/other/file.json']


def test_wired_wrong_refuses_and_never_execs_claude(tmp_path):
    proc, recorded = _run_with_substrate(tmp_path, wire_check_rc=1)

    assert proc.returncode == 1
    assert recorded is None
    assert 'not correctly wired' in proc.stderr
    assert 'wire.sh' in proc.stderr


def test_substrate_not_mounted_refuses_with_a_distinct_message(tmp_path):
    proc, recorded = _run_with_substrate(tmp_path, wire_missing=True)

    assert proc.returncode == 127
    assert recorded is None
    assert 'not mounted' in proc.stderr


def test_never_runs_a_bare_wire_sh(tmp_path):
    """Only --check — a bare `wire.sh` here would let a `claude` restart
    silently repair wiring broken mid-session, defeating VALIDATION.md's
    Part 4 failure-mode tests."""
    substrate_root = tmp_path / 'virgil'
    shared = substrate_root / 'shared'
    shared.mkdir(parents=True)
    call_log = substrate_root / 'wire-calls.txt'
    wire = shared / 'wire.sh'
    wire.write_text(
        '#!/bin/sh\necho "$@" >> {!r}\n[ "$1" = "--check" ] && exit 0 || exit 1\n'.format(
            str(call_log)))
    wire.chmod(0o755)

    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    wrapper = bin_dir / 'claude'
    wrapper.write_text(WRAPPER.read_text())
    wrapper.chmod(0o755)
    real = bin_dir / 'claude-real'
    real.write_text('#!/bin/bash\nexit 0\n')
    real.chmod(0o755)
    home = tmp_path / 'home'
    home.mkdir()

    subprocess.run([str(wrapper)], env={
        'PATH': f'{bin_dir}:/usr/bin:/bin', 'HOME': str(home),
        'DAX_CLAUDE_REAL': str(real), 'SUBSTRATE_ROOT': str(substrate_root),
    }, capture_output=True, text=True, timeout=30)

    assert call_log.read_text().splitlines() == ['--check']
