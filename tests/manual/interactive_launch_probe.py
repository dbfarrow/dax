#!/usr/bin/env python3
"""Probe Claude Code's *interactive* launch path against an isolated config dir.

Why this exists
---------------
`claude auth status` and `claude -p` both read .credentials.json directly and
report success against a config directory that would prompt for login the moment
it is started interactively. Every automated test we have lives in that blind
spot, which is how "the credential is not landing" was diagnosed for a day when
the credential was landing fine and Claude Code was simply running its first-run
wizard instead of looking at it.

This probe drives the real launch path under a pty and classifies the outcome.

Not a pytest. It needs a real credential and network access, so it is a manual
diagnostic. The unit-level guarantees live in tests/test_dax_creds_claude_wrapper.py.

Usage
-----
    # Does a bare credential authenticate interactively? (expect FAIL)
    python3 tests/manual/interactive_launch_probe.py

    # Does seeding the onboarding flag fix it? (expect PASS)
    python3 tests/manual/interactive_launch_probe.py --seed

    # Probe a config dir that already exists, without copying anything in
    python3 tests/manual/interactive_launch_probe.py --config-dir ~/.claude --in-place

The temporary config directory holds a real OAuth token and is deleted on exit
unless --keep is passed.
"""
import argparse
import json
import os
import pty
import re
import select
import shutil
import sys
import tempfile
import time

ANSI_CSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
ANSI_OSC = re.compile(r'\x1b\][^\x07]*\x07')

# Anything matching this is a token; never let it reach stdout.
SECRET = re.compile(r'sk-ant-\w+-[A-Za-z0-9_-]+')

LOGIN_MARKERS = ('Select login method', 'Opening browser to sign in',
                 'oauth/authorize')
READY_MARKERS = ('Welcome back', 'for shortcuts')
ONBOARDING_MARKERS = ('Choose the text style', "Let's get started")
TRUST_MARKERS = ('trust this folder', 'has not been trusted')


def clean(raw):
    text = raw.decode('utf-8', 'replace')
    text = ANSI_OSC.sub('', text)
    text = ANSI_CSI.sub('', text)
    return SECRET.sub('<redacted>', text)


def squash(text):
    """Collapse blank lines; the TUI emits a lot of them."""
    return '\n'.join(line for line in text.splitlines() if line.strip())


def launch(config_dir, claude_bin, enters, settle, workdir):
    env = dict(os.environ)
    # Strip the parent session's markers, or Claude Code treats this as a child
    # session and takes a different path than a real user launch.
    for key in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_SESSION_ID',
                'CLAUDE_CODE_CHILD_SESSION', 'ANTHROPIC_API_KEY'):
        env.pop(key, None)
    env['CLAUDE_CONFIG_DIR'] = str(config_dir)
    env['TERM'] = 'xterm-256color'

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(workdir)
        os.execve(claude_bin, ['claude'], env)

    def drain(seconds):
        buf = b''
        end = time.time() + seconds
        while time.time() < end:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
        return buf

    screens = [('boot', clean(drain(settle)))]
    for i in range(enters):
        os.write(fd, b'\r')
        time.sleep(0.5)
        screens.append((f'after-enter-{i + 1}', clean(drain(settle))))

    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except OSError:
        pass
    return screens


def classify(screens):
    # The TUI positions individual characters with ANSI codes, so stripping
    # those codes also strips the spaces between words: "Choose the text style"
    # arrives as "Choosethetextstyle". Match with all whitespace removed on both
    # sides rather than trying to reconstruct it.
    squeeze = lambda s: re.sub(r'\s+', '', s)
    transcript = squeeze('\n'.join(text for _, text in screens))
    hit = lambda markers: any(squeeze(m) in transcript for m in markers)
    return {
        'login': hit(LOGIN_MARKERS),
        'ready': hit(READY_MARKERS),
        'onboarding': hit(ONBOARDING_MARKERS),
        'trust': hit(TRUST_MARKERS),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--creds', default=os.path.expanduser('~/.claude/.credentials.json'),
                    help='credential file to copy into the probe config dir')
    ap.add_argument('--seed', action='store_true',
                    help='also write {"hasCompletedOnboarding": true}')
    ap.add_argument('--config-dir', help='use this dir instead of a temp one')
    ap.add_argument('--in-place', action='store_true',
                    help='do not copy credentials in; probe --config-dir as-is')
    ap.add_argument('--claude-bin', default='/usr/bin/claude-real',
                    help='real Claude binary, bypassing the dax wrapper')
    ap.add_argument('--workdir', default=os.path.expanduser('~'),
                    help='cwd for the probed session')
    ap.add_argument('--enters', type=int, default=2,
                    help='Enter keypresses to advance the wizard')
    ap.add_argument('--settle', type=float, default=8.0,
                    help='seconds to wait for each screen')
    ap.add_argument('--keep', action='store_true',
                    help='do not delete the temp config dir (it holds a token)')
    ap.add_argument('--show', action='store_true', help='print captured screens')
    args = ap.parse_args()

    if not os.path.exists(args.claude_bin):
        sys.exit(f'no such binary: {args.claude_bin}')

    temp = None
    if args.config_dir:
        config_dir = os.path.expanduser(args.config_dir)
        os.makedirs(config_dir, exist_ok=True)
    else:
        temp = tempfile.mkdtemp(prefix='claude-probe-')
        config_dir = temp

    try:
        if not args.in_place:
            if not os.path.exists(args.creds):
                sys.exit(f'no credential file at {args.creds}')
            shutil.copy(args.creds, os.path.join(config_dir, '.credentials.json'))
            os.chmod(os.path.join(config_dir, '.credentials.json'), 0o600)
        if args.seed:
            with open(os.path.join(config_dir, '.claude.json'), 'w') as fh:
                json.dump({'hasCompletedOnboarding': True}, fh)

        screens = launch(config_dir, args.claude_bin, args.enters,
                         args.settle, args.workdir)

        if args.show:
            for name, text in screens:
                print(f'\n=============== {name} ===============')
                print(squash(text)[-2000:])

        saw = classify(screens)
        print('\n--- observed ---')
        for key in ('onboarding', 'login', 'trust', 'ready'):
            print(f'  {key:11} {saw[key]}')

        if saw['login']:
            print('\nFAIL: reached a login prompt with a valid credential present.')
            print('      Almost always a missing .claude.json, not a bad credential.')
            return 1
        if saw['ready']:
            print('\nPASS: reached the prompt without a login screen.')
            return 0
        print('\nINCONCLUSIVE: neither a login screen nor a ready prompt was seen.')
        print('      Re-run with --show, and raise --settle on a slow start.')
        return 2
    finally:
        if temp and not args.keep:
            shutil.rmtree(temp, ignore_errors=True)
        elif temp:
            print(f'\nkept (contains a real token): {temp}')


if __name__ == '__main__':
    sys.exit(main())
