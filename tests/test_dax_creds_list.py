"""`dax creds list`'s new `relogin` column, and the `[!] key on disk` warning
now checking the right file per env.

Before this, the table's `stored` column (a bare Keychain-presence check)
and the claude `[!] key on disk` warning (which checked fixed shared host
paths unconditionally, regardless of which env actually used them) were
the only signals offered — neither can show "is this blob's refresh token
actually still honored by Anthropic", because nothing local can (see
CLAUDE.md's 2026-10 "found live" entry and
tests/test_dax_creds_login_state.py). The `relogin` column doesn't try to
answer that either — it surfaces the one thing that *is* knowable locally
and newly tracked: when the login flow last actually ran for this
credential, and whether `dax run`'s cooldown considers it due again. Only
meaningful for a claude credential used by at least one claude_tenant_state
env, since that's the only case `dax run` consults it for at all.

The `[!] key on disk` fix is separate: a claude_tenant_state env's live
credential file lives in its own state tree
(`~/.local/state/dax/tenants/<tenant>/<project>/.credentials.json`), never
in the shared `~/.claude/.credentials.json` the old check looked at
unconditionally — so the warning was checking a path those envs never
write to, and silently missing the one they do.
"""
from datetime import datetime, timedelta, timezone

import pytest

from dax_creds.config import record_login, state_tree_path
from dax_creds.init import run_creds_list


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return home


def _config(tenant_state=True):
    return {
        'credentials': {'claude-acme-myenv': {'provider': 'claude'}},
        'projects': {
            'myenv': {
                'tenant': 'acme', 'creds': ['claude-acme-myenv'],
                'features': ['claude_tenant_state'] if tenant_state else [],
            },
        },
    }


def test_shows_never_when_no_login_recorded(home, capsys):
    run_creds_list(_config())
    out = capsys.readouterr().out
    assert 'never' in out


def test_shows_age_when_recently_logged_in_and_not_due(home, capsys):
    record_login('claude-acme-myenv', when=datetime.now(timezone.utc) - timedelta(hours=1))
    run_creds_list(_config())
    out = capsys.readouterr().out
    assert 'due' not in out
    assert '1h' in out


def test_flags_due_once_cooldown_elapses(home, capsys):
    record_login('claude-acme-myenv', when=datetime.now(timezone.utc) - timedelta(hours=25))
    run_creds_list(_config())
    out = capsys.readouterr().out
    assert 'due' in out


def test_dash_for_non_claude_providers(home, capsys):
    config = {
        'credentials': {'gh-acme': {'provider': 'github'}},
        'projects': {'myenv': {'tenant': 'acme', 'creds': ['gh-acme'], 'features': []}},
    }
    run_creds_list(config)
    out = capsys.readouterr().out
    gh_line = [l for l in out.splitlines() if 'gh-acme' in l]
    assert len(gh_line) == 1 and 'never' not in gh_line[0]


VALID_BLOB = '{"claudeAiOauth": {"refreshToken": "fake-refresh-token"}}'


def _write_tree_credential(tenant, project, content=VALID_BLOB):
    tree = state_tree_path(tenant, project)
    tree.mkdir(parents=True, exist_ok=True)
    cred_file = tree / '.credentials.json'
    cred_file.write_text(content)
    return cred_file


def _write_shared_credential(home, content=VALID_BLOB):
    claude_dir = home / '.claude'
    claude_dir.mkdir(parents=True, exist_ok=True)
    cred_file = claude_dir / '.credentials.json'
    cred_file.write_text(content)
    return cred_file


def test_key_on_disk_checks_the_tenant_state_tree(home, capsys):
    _write_tree_credential('acme', 'myenv')
    run_creds_list(_config())
    out = capsys.readouterr().out
    assert 'key on disk' in out


def test_key_on_disk_silent_when_tree_file_absent_even_with_stale_shared_file(home, capsys):
    """A leftover shared-path file (from some other, unrelated use of
    ~/.claude) must not fire the warning for a claude_tenant_state env that
    never writes there at all."""
    _write_shared_credential(home)
    run_creds_list(_config())
    out = capsys.readouterr().out
    assert 'key on disk' not in out


def test_key_on_disk_silent_when_neither_location_has_anything(home, capsys):
    run_creds_list(_config())
    out = capsys.readouterr().out
    assert 'key on disk' not in out
