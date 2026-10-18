"""`record_login`/`last_login_at`/`relogin_due` — the freshness bookkeeping
behind `dax run`'s proactive Claude relogin (see
tests/test_dax_cmd_run_relogin.py for the integration behavior).

Built because there is no reliable signal on the credential blob itself for
"is this refresh token still good" — `expiresAt` is the short-lived access
token, and `refreshTokenExpiresAt` was already found (2026-07-30) not to
correlate with actual mint date. Rather than guess from the blob, `dax run`
just reruns the login flow before every launch of a `claude_tenant_state`
env — and this is the cooldown that keeps that from popping a browser
window on every single restart in the same day.
"""
from datetime import datetime, timedelta, timezone

import pytest

from dax_creds.config import (
    DEFAULT_RELOGIN_COOLDOWN_HOURS, last_login_at, login_state_path,
    record_login, relogin_due,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    return home


def test_last_login_at_is_none_when_never_recorded(home):
    assert last_login_at('claude-acme-myproc') is None


def test_record_login_then_last_login_at_round_trips(home):
    when = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    record_login('claude-acme-myproc', when=when)
    assert last_login_at('claude-acme-myproc') == when


def test_record_login_writes_beside_the_tenants_dir_not_inside_it(home):
    record_login('claude-acme-myproc')
    path = login_state_path('claude-acme-myproc')
    assert path.is_file()
    assert path.parent.name == 'creds-login'
    assert path.parent.parent == home / '.local' / 'state' / 'dax'


def test_last_login_at_is_none_on_corrupt_state_file(home):
    path = login_state_path('claude-acme-myproc')
    path.parent.mkdir(parents=True)
    path.write_text('not valid json')
    assert last_login_at('claude-acme-myproc') is None


def test_relogin_due_when_never_logged_in(home):
    assert relogin_due('claude-acme-myproc', {}) is True


def test_relogin_not_due_within_default_cooldown(home):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    record_login('claude-acme-myproc', when=now - timedelta(hours=1))
    assert relogin_due('claude-acme-myproc', {}, now=now) is False


def test_relogin_due_after_default_cooldown_elapses(home):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    record_login('claude-acme-myproc',
                 when=now - timedelta(hours=DEFAULT_RELOGIN_COOLDOWN_HOURS, minutes=1))
    assert relogin_due('claude-acme-myproc', {}, now=now) is True


def test_relogin_due_respects_configured_cooldown(home):
    """A shorter configured cooldown fires sooner than the 24h default."""
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    record_login('claude-acme-myproc', when=now - timedelta(hours=2))
    config = {'defaults': {'relogin_cooldown_hours': 1}}
    assert relogin_due('claude-acme-myproc', config, now=now) is True


def test_relogin_due_respects_a_longer_configured_cooldown(home):
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    record_login('claude-acme-myproc', when=now - timedelta(hours=2))
    config = {'defaults': {'relogin_cooldown_hours': 48}}
    assert relogin_due('claude-acme-myproc', config, now=now) is False
