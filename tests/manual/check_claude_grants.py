#!/usr/bin/env python3
"""Check that each env's Claude credential is an independent OAuth grant.

Run on the **host** (macOS), where the Keychain lives:

    python3 tests/manual/check_claude_grants.py

Per-env credentials only isolate anything if each one came from its own login.
Two entries holding the *same* refresh token are one grant wearing two names:
they authenticate fine, so nothing looks wrong, but a rotation on either kills
both — and that is precisely the failure the per-env naming exists to prevent.

It has happened for real (2026-07-30): a login abandoned at the browser step
still ran the post-login import, which read the credential already sitting in
~/.claude/.credentials.json and stored it under the new per-env name. The result
passed every check except this one.

Refresh tokens are never printed — only a short hash, enough to compare.
Exits non-zero if any grant is shared.

LIMIT, and it bounds what a PASS is worth: the refresh token is a proxy for
grant identity, not the identity itself — the blob carries no grant ID. A copy
is caught only while both entries still hold the *same* token. If each side then
refreshes independently, each gets a new refresh token while still descending
from one OAuth grant, and this check reports PASS. (Whether Anthropic rotates
refresh tokens on use, and whether it invalidates the old one, is unverified —
see the design doc's "Open gap: Claude refresh-token rotation".)

So a PASS is strong evidence right after minting and weaker the longer the
credentials have been in use. The `refresh expires` column is the corroborating
signal: distinct dates spread across the days the logins actually happened
support genuinely separate mints, whereas dates that cluster tightly while
tokens differ is the shape rotation-from-a-shared-grant would produce.
"""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

KEYCHAIN_SERVICE = 'dax-creds'


def keychain_secret(name):
    result = subprocess.run(
        ['security', 'find-generic-password', '-s', KEYCHAIN_SERVICE, '-a', name, '-w'],
        capture_output=True, text=True)
    return None if result.returncode else result.stdout.strip()


def envelope(blob):
    try:
        oauth = json.loads(blob)['claudeAiOauth']
    except (TypeError, ValueError, KeyError):
        return None
    return oauth if isinstance(oauth, dict) else None


def grant_id(oauth):
    """Short hash of the refresh token — the grant's identity, not its value."""
    token = oauth.get('refreshToken') or ''
    if not token:
        return 'NO-REFRESH-TOKEN'
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def when(ms):
    if not ms:
        return '?'
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d')
    except (TypeError, ValueError, OSError):
        return '?'


def claude_credential_names(config):
    """Every Claude credential name: registered ones, plus those derived from
    an env's bare `claude` token. Mirrors dax_creds.config's resolution, but
    standalone so this script runs without importing dax."""
    names = {}
    for name, cred in (config.get('credentials') or {}).items():
        if cred.get('provider') == 'claude':
            names[name] = '(registered)'

    for env_name, project in (config.get('projects') or {}).items():
        tenant = project.get('tenant')
        for entry in project.get('creds') or []:
            if entry != 'claude':
                continue
            if not tenant:
                print(f'  ! env {env_name!r} wants a claude credential but has no tenant')
                continue
            names[f'claude-{tenant}-{env_name}'] = env_name
    return names


def main():
    config_path = Path.home() / '.dax.yaml'
    if not config_path.exists():
        sys.exit(f'no {config_path}')

    try:
        import yaml
    except ImportError:
        sys.exit('pyyaml required: pip install pyyaml')
    config = yaml.safe_load(config_path.read_text()) or {}

    names = claude_credential_names(config)
    if not names:
        sys.exit('no Claude credentials found in ~/.dax.yaml')

    print(f'\n{"credential":34}  {"used by":16}  {"grant":14}  {"refresh expires":15}  access')
    print(f'{"-" * 34}  {"-" * 16}  {"-" * 14}  {"-" * 15}  {"-" * 10}')

    grants = {}
    for name in sorted(names):
        blob = keychain_secret(name)
        if blob is None:
            print(f'{name:34}  {names[name]:16}  (not in Keychain)')
            continue
        oauth = envelope(blob)
        if oauth is None:
            print(f'{name:34}  {names[name]:16}  (not a Claude envelope)')
            continue
        gid = grant_id(oauth)
        grants.setdefault(gid, []).append(name)
        print(f'{name:34}  {names[name]:16}  {gid:14}  '
              f'{when(oauth.get("refreshTokenExpiresAt")):15}  '
              f'{when(oauth.get("expiresAt"))}')

    # The shared ~/.claude/.credentials.json, while feature `claude` still
    # mounts it into every container. Whichever env wrote last owns it, and
    # every container reads it — so a match here tells you which env's grant
    # any non-tenant-state session is actually running on.
    shared = Path.home() / '.claude' / '.credentials.json'
    print()
    if shared.exists():
        oauth = envelope(shared.read_text())
        if oauth:
            gid = grant_id(oauth)
            owners = grants.get(gid, [])
            owner = ', '.join(owners) if owners else 'no credential above'
            print(f'~/.claude/.credentials.json  grant:{gid}  matches: {owner}')
        else:
            print('~/.claude/.credentials.json  present but not a Claude envelope')
    else:
        print('~/.claude/.credentials.json  absent')

    shared_grants = {g: n for g, n in grants.items() if len(n) > 1}
    print()
    if shared_grants:
        print('FAIL — these credentials are the same grant under different names:')
        for gid, sharers in shared_grants.items():
            print(f'  grant:{gid}  {", ".join(sharers)}')
        print('\nEach needs its own `dax creds login`. Clear the duplicates first:')
        for sharers in shared_grants.values():
            for name in sharers[1:]:
                print(f'  dax creds remove {name}')
        return 1

    print(f'PASS — {len(grants)} distinct grant(s) across {sum(len(v) for v in grants.values())} '
          f'credential(s); no sharing.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
