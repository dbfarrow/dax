#!/usr/bin/env python3
"""Move one env's accumulated Claude state into its own state tree.

Run on the **host**, where both the shared `~/.claude` and the state trees live.
A container cannot migrate itself: only the env that owns a tree ever has it
mounted, and the source directory it is copying *from* is the very thing it is
writing to.

    python3 tools/migrate_env_state.py dax            # dry run — shows the plan
    python3 tools/migrate_env_state.py dax --apply

Dry run is the default because every step writes into a tree that a live session
would then adopt. Nothing here touches `~/.dax.yaml`: switching the env over is
one `dax env set` call, printed at the end, kept manual because config edits are
the part with real blast radius (a test without HOME isolation destroyed that file
on 2026-07-31).

What moves, and why each is safe to separate — measured, not assumed:

  * `projects/<mangled-cwd>/` — the directory name *is* the container cwd, so it
    is already exactly one env's transcripts plus its auto-memory directory.
  * `history.jsonl` — every line carries a `project` field, so it is filtered
    rather than copied. Copying it whole would pool every other env's prompts into
    this tree, which is the failure the isolation exists to prevent.

What does not move: `CLAUDE.md`, `settings.json`, `settings.local.json` are
user-level and the feature mounts them read-only into every tree. `file-history/`
is keyed by session UUID and mappable, but it is edit-undo history and not worth
the step. Caches regenerate.

`.claude.json` is deliberately **not** handled here. The copy worth having lives
inside the env's running container (35K of trust, allowedTools, and tips history,
already scoped to that one project) and dies with it, so it has to be taken by
hand before shutdown — see the runbook. Without it the wrapper seeds
`{"hasCompletedOnboarding": true}`, which costs re-consent and replayed tips and
nothing else.

Full runbook: docs/testing/2026-07-31-migrate-env-to-state-tree.md
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONTAINER_HOME = '/home'  # container-side $HOME parent; cwd keys are built from it


def fail(msg):
    print(f'  [!] {msg}')
    sys.exit(1)


def load_config():
    path = Path.home() / '.dax.yaml'
    if not path.exists():
        fail(f'no {path}')
    try:
        import yaml
    except ImportError:
        fail('pyyaml required: pip install pyyaml')
    return yaml.safe_load(path.read_text()) or {}


def container_user():
    """The username inside the container, which is what the cwd key is built from.

    Mirrors dax.py's `_get_username()`. Deriving it from `Path.home().name` instead
    happens to work when the host home directory is named after the user, which is
    a coincidence rather than a rule — and getting it wrong builds a `projects/`
    key that matches nothing, so the migration silently copies zero transcripts.
    """
    return os.environ.get('USER') or os.environ.get('LOGNAME') or Path.home().name


def container_name_for(dir_path):
    """Mirrors dax.py's `envname`: the dir path under $HOME, slashes to dashes."""
    home = str(Path.home())
    return str(dir_path).replace(home, '').lstrip('/').replace('/', '-')


def is_running(name):
    try:
        out = subprocess.run(
            ['docker', 'ps', '--filter', f'name={name}', '--format', '{{.Names}}'],
            capture_output=True, text=True, timeout=5)
        return name in out.stdout.split()
    except Exception:
        return False


def project_key(user, env_name):
    """Claude Code's `projects/` directory name for this env's container cwd.

    Stable across the migration: `workdir_name` does not change, so the container
    cwd stays $HOME/<env> and Claude Code keeps using the same key.
    """
    return f'-{CONTAINER_HOME.strip("/")}-{user}-{env_name}'


def plan(env_name, args):
    config = load_config()
    projects = config.get('projects') or {}
    if env_name not in projects:
        known = ', '.join(sorted(projects)) or '(none)'
        fail(f'no env named {env_name!r}. Known: {known}')

    project = projects[env_name]
    tenant = project.get('tenant')
    if not tenant:
        fail(f'env {env_name!r} has no tenant, so it has no tree path.\n'
             f'      Set one first: dax env set {env_name} tenant <name>')

    container = container_name_for(project.get('dir', ''))
    if is_running(container):
        fail(f'container {container!r} is running. Stop it first — a transcript '
             f'copied\n      mid-session is torn.')

    user = args.user or container_user()
    key = project_key(user, env_name)
    src_projects = Path.home() / '.claude' / 'projects' / key
    tree = Path.home() / '.local' / 'state' / 'dax' / 'tenants' / tenant / env_name

    print(f'\n  env        {env_name}  (tenant: {tenant})')
    print(f'  container  {container}  [stopped]')
    print(f'  cwd key    {key}')
    print(f'  tree       {tree}')
    print()

    return config, project, tree, src_projects, key


def check_credential(tree, apply_it, clear):
    cred = tree / '.credentials.json'
    if not cred.exists():
        print('  [ok] no credential in the tree — it will bootstrap from Keychain')
        return
    # Blocks the bootstrap rather than merely being stale: the wrapper injects
    # only when the file is absent or empty (-s), never when it is invalid.
    print(f'  [!!] {cred} exists.')
    print('       This BLOCKS the Keychain bootstrap: the wrapper injects only when')
    print('       the file is absent or empty, not when it is stale or invalid.')
    if not clear:
        print('       Re-run with --clear-credential, or delete it by hand.')
        return
    if apply_it:
        cred.unlink()
        print('       deleted.')
    else:
        print('       would delete.')


def copy_projects(src, tree, apply_it, force):
    dst = tree / 'projects' / src.name
    if not src.exists():
        print(f'  [--] no transcripts at {src} — nothing to copy')
        return
    size = sum(f.stat().st_size for f in src.rglob('*') if f.is_file())
    sessions = len(list(src.glob('*.jsonl')))
    memory = (src / 'memory').is_dir()
    print(f'  [->] {src.name}: {sessions} session(s), '
          f'{size / 1e6:.1f}MB{", memory/" if memory else ""}')
    print(f'       -> {dst}')
    if dst.exists() and not force:
        print('  [!!] destination already exists. Re-run with --force to overwrite,')
        print('       or move it aside — merging two transcript sets is not safe.')
        return
    if not apply_it:
        return
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, symlinks=True)
    print('       copied.')


def filter_history(env_name, user, tree, apply_it):
    src = Path.home() / '.claude' / 'history.jsonl'
    dst = tree / 'history.jsonl'
    if not src.exists():
        print('  [--] no shared history.jsonl')
        return
    env_cwd = f'{CONTAINER_HOME}/{user}/{env_name}'
    kept, total = [], 0
    for line in src.read_text().splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            if json.loads(line).get('project') == env_cwd:
                kept.append(line)
        except ValueError:
            continue
    print(f'  [->] history.jsonl: {len(kept)} of {total} lines are {env_cwd}')
    print(f'       -> {dst}')
    if dst.exists():
        print(f'  [!!] {dst} already exists and would be replaced.')
    if apply_it:
        tree.mkdir(parents=True, exist_ok=True)
        dst.write_text('\n'.join(kept) + ('\n' if kept else ''))
        print('       written.')


def check_claude_json(tree, user, env_name):
    path = tree / '.claude.json'
    if not path.exists():
        print('  [--] no .claude.json in the tree. The wrapper will seed')
        print('       {"hasCompletedOnboarding": true} — costs re-consent and tips.')
        print('       To keep the real one, copy it out of the container BEFORE')
        print('       shutdown (see the runbook) — it dies with the container.')
        return
    try:
        keys = list((json.loads(path.read_text()).get('projects') or {}))
    except ValueError:
        print(f'  [!!] {path} is not valid JSON')
        return
    expected = f'{CONTAINER_HOME}/{user}/{env_name}'
    extra = [k for k in keys if k != expected]
    if extra:
        print(f'  [!!] {path} carries other projects: {", ".join(extra)}')
        print('       It came from a shared-mount host rather than this container,')
        print("       so it would pull other envs' state into this tree.")
    else:
        print(f'  [ok] .claude.json present, scoped to {expected}')


def main():
    ap = argparse.ArgumentParser(
        description="Move an env's Claude state into its own tree.",
        epilog='Dry run by default. See '
               'docs/testing/2026-07-31-migrate-env-to-state-tree.md')
    ap.add_argument('env', help='env name as registered in ~/.dax.yaml')
    ap.add_argument('--apply', action='store_true', help='actually write')
    ap.add_argument('--force', action='store_true',
                    help='overwrite an existing transcript dir in the tree')
    ap.add_argument('--clear-credential', action='store_true',
                    help='delete a credentials file found in the tree')
    ap.add_argument('--user', help='container username (default: host username)')
    args = ap.parse_args()

    config, project, tree, src_projects, key = plan(args.env, args)
    user = args.user or container_user()

    if not args.apply:
        print('  DRY RUN — nothing will be written. Re-run with --apply.\n')

    copy_projects(src_projects, tree, args.apply, args.force)
    print()
    filter_history(args.env, user, tree, args.apply)
    print()
    check_credential(tree, args.apply, args.clear_credential)
    print()
    check_claude_json(tree, user, args.env)

    print(f'\n  Next, if the above looks right:')
    print(f'    dax env set {args.env} features claude_tenant_state')
    print(f'    cd {project.get("dir", "<env dir>")} && dax -t run')
    print(f'  Then inside the container:')
    print(f'    ls ~/.claude/projects/   # only {key}')
    print(f'    ls ~/.claude.json        # must NOT exist')
    print(f'    claude                   # /resume lists the migrated sessions')
    print(f'\n  The source is left in place. Remove it only after a successful '
          f'session\n  and a container restart:')
    print(f'    rm -rf ~/.claude/projects/{key}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
