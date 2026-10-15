#!/usr/bin/env python3
"""Does Anthropic rotate the Claude Code refresh token on use?

This is the open question flagged in docs/design/2026-07-27-tenant-isolation.md
("Open gap: Claude refresh-token rotation") and in
tests/manual/check_claude_grants.py's own LIMIT section, never resolved: if a
refresh mints a *new* refresh token and invalidates the old one, two envs
sharing one grant will silently diverge the moment either refreshes — the
exact failure per-project credential isolation exists to prevent. If it
doesn't rotate, sharing one grant across many envs mapped to the same real
account is just fine, and a real chunk of dax's credential machinery
(BARE_PROVIDER_CREDS derivation, grant_collision() enforcement, the teardown
write-back) is defending against a risk that never happens.

dax's own daemon cannot answer this directly — it has no knowledge of
Anthropic's token endpoint or client_id (see dax_creds/daemon.py's
PassthroughExchanger: Claude refresh is delegated entirely to the real
`claude` binary's own internal logic, never done by dax). So this script
does not perform a refresh itself. It does the two parts that are safe to
automate and error-prone to do correctly by hand:

  1. Force an access-token refresh to happen on the *next* real use, by
     rewriting `expiresAt` to a past timestamp — the same field `claude`
     itself checks, so this is indistinguishable from genuine expiry, not a
     special code path.
  2. Diff the refresh token (as a hash — never printed in full) from before
     that forced expiry to after, so you don't have to eyeball two long
     secrets side by side.

Triggering the actual refresh is still on you: after `force-expire`, launch
`claude` for real (inside the env whose state tree you're testing) and send
at least one message, so it makes a real API round-trip. Then run `check`.

USAGE (run on the host or inside the env's own container — wherever the
target `.credentials.json` is reachable):

    # One grant, does it rotate on its own refresh?
    python3 check_refresh_rotation.py force-expire ~/.claude/.credentials.json
    # ... now actually use claude in that env, send one message ...
    python3 check_refresh_rotation.py check ~/.claude/.credentials.json

    # Cross-talk: does a *second* env sharing the same grant survive the
    # first env's refresh? Copy one real credentials.json into both env's
    # state trees first (deliberately sharing a grant), force-expire and
    # check env A as above, then separately:
    python3 check_refresh_rotation.py force-expire /path/to/envB/.credentials.json
    # ... use claude in env B ...
    python3 check_refresh_rotation.py check /path/to/envB/.credentials.json
    # If env B's own refresh now fails (claude prompts to log in again),
    # that's rotation's cross-talk consequence confirmed directly - this
    # script can't observe that half; watch env B's own behavior for it.

A sidecar backup (`<path>.before-rotation-test.json`) is written by
force-expire and read by check - remove it yourself once you're done; this
script never deletes it automatically, so nothing about the real file is
ever assumed to be recoverable only from memory.
"""
import json
import sys
import time
from hashlib import sha256
from pathlib import Path


def _hash(token):
    """Short hash of a secret, safe to print — never the token itself,
    matching check_claude_grants.py's own convention."""
    if not token:
        return '(none)'
    return sha256(token.encode()).hexdigest()[:12]


def _oauth(blob):
    try:
        data = json.loads(blob)
    except (TypeError, ValueError):
        return None
    oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None
    return oauth if isinstance(oauth, dict) else None


def _backup_path(path):
    return path.with_name(path.name + '.before-rotation-test.json')


def cmd_force_expire(path):
    if not path.is_file():
        sys.exit(f'no such file: {path}')
    original = path.read_text()
    oauth = _oauth(original)
    if oauth is None:
        sys.exit(f'{path} does not look like a Claude credentials blob '
                  '({"claudeAiOauth": {...}} expected)')
    if not oauth.get('refreshToken'):
        sys.exit(f'{path} has no refreshToken — nothing to test a refresh of')

    backup = _backup_path(path)
    if backup.exists():
        sys.exit(f'{backup} already exists — an earlier test run was not '
                  'cleaned up. Remove it (after confirming you no longer '
                  'need it) before starting a new one.')
    backup.write_text(original)

    before_refresh_hash = _hash(oauth['refreshToken'])
    before_access_hash = _hash(oauth.get('accessToken'))
    data = json.loads(original)
    data['claudeAiOauth']['expiresAt'] = 0
    path.write_text(json.dumps(data))

    print(f'Backed up original to {backup}')
    print(f'  refreshToken (before): {before_refresh_hash}')
    print(f'  accessToken  (before): {before_access_hash}')
    print(f'  expiresAt set to 0 (forces a refresh on next real use)')
    print()
    print('Now go use `claude` for real in this env — send at least one '
          'message so it makes a genuine API call — then run:')
    print(f'  python3 {sys.argv[0]} check {path}')


def cmd_check(path):
    if not path.is_file():
        sys.exit(f'no such file: {path}')
    backup = _backup_path(path)
    if not backup.is_file():
        sys.exit(f'no {backup} found — run force-expire first')

    before = _oauth(backup.read_text())
    after = _oauth(path.read_text())
    if after is None:
        sys.exit(f'{path} is no longer a valid Claude credentials blob — '
                  'claude may have failed to refresh it. Check its own '
                  'output/logs for the actual error.')

    before_refresh = _hash(before.get('refreshToken'))
    after_refresh = _hash(after.get('refreshToken'))
    before_access = _hash(before.get('accessToken'))
    after_access = _hash(after.get('accessToken'))

    print(f'refreshToken   before: {before_refresh}')
    print(f'refreshToken   after:  {after_refresh}')
    print(f'accessToken    before: {before_access}')
    print(f'accessToken    after:  {after_access}')
    print(f'expiresAt      after:  {after.get("expiresAt")} '
          f'({_human_time(after.get("expiresAt"))})')
    print()

    if after_access == before_access:
        print('[!] accessToken did NOT change — claude may not have '
              'actually refreshed yet (did you send a real message after '
              'force-expire?). Re-check after using it.')
        return

    if after_refresh != before_refresh:
        print('[!] ROTATED — the refresh token changed on this refresh.')
        print('    Sharing one grant across multiple envs is NOT safe: the')
        print('    first one to refresh invalidates every other copy.')
    else:
        print('[-] UNCHANGED — same refresh token after a real access-token')
        print('    refresh. One data point toward "rotation is not real,"')
        print('    not proof — repeat a couple more times (force-expire,')
        print('    use it, check) before treating this as confirmed.')

    print()
    print(f'(backup left at {backup} — remove it yourself when done)')


def _human_time(ms):
    if not ms:
        return '?'
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(ms / 1000))
    except (TypeError, ValueError, OSError):
        return '?'


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ('force-expire', 'check'):
        sys.exit(f'usage: {sys.argv[0]} force-expire|check <path-to-.credentials.json>')
    path = Path(sys.argv[2]).expanduser()
    if sys.argv[1] == 'force-expire':
        cmd_force_expire(path)
    else:
        cmd_check(path)


if __name__ == '__main__':
    main()
