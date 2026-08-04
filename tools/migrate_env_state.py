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

## State strewn across more than one key

The simple case — this env has *always* run in a dax container — has exactly one
`projects/` key: `-<home>-<user>-<env_name>`, because `workdir_name` (dax's mount
basename) never changes. `project_key()` below builds exactly that.

That assumption breaks for state accumulated **before** containerization, i.e. a
directory that used to be a plain nested subdirectory of a larger host tree and
is only now becoming its own registered env (checked against the real
`~/.claude/projects/` on 2026-08-01 while planning discernment's per-process
repo split — see `docs/design/2026-07-27-tenant-isolation.md`). Concretely, for a
process that used to live at `~/work/processes/<name>`:

  * its own sessions were recorded under the *literal nested path's* key,
    `-home-<user>-work-processes-<name>` — not the flat key this script assumes.
  * sessions run with cwd at an ancestor of that path (`~/work/processes`,
    `~/work` itself — before the user `cd`'d into the specific process
    directory, or during general non-process-specific work) recorded under
    *those* keys instead, and those directories are shared across every process
    that was ever a child of them. There is no reliable way to attribute one
    line of that data to a single process without reading its content, so this
    script never tries — it only ever copies data whose key already names, in
    full, a directory the caller identifies as this process's own.

`--legacy-cwd PATH` (repeatable) is how the caller supplies those exact
historical paths — this script cannot infer them, since a rename or restructure
is exactly what makes the nested path different from the new one. Each legacy
cwd's `projects/<key>/` copies into the tree as its own directory, same as the
canonical key, and its own `history.jsonl` lines are filtered in by exact match
on the literal path. It is a straight copy, not a merge into the canonical
key's directory: Claude Code's `/resume` is scoped to the container's *current*
cwd, so a legacy directory won't show up there regardless — flattening it in
would misrepresent old sessions as having happened at the new cwd for no
resumability gain, while a separate directory keeps the provenance honest and
still gets the data off the machine's one shared `~/.claude` and into the tree
before the source is deleted.

Every run also prints a **discovery report** (no flag needed — it costs one
directory listing and one `history.jsonl` scan): any other `projects/` key that
ends with this env's name but wasn't passed via `--legacy-cwd` (a candidate the
caller may have forgotten), and any ancestor key of a key actually being
migrated (a real `projects/` directory that is itself a dash-prefix of a
migrated key — i.e., it holds sessions from a shared parent cwd). Ancestor data
is reported, never copied: it is the commingled case above. The report exists
so silence never gets mistaken for "nothing else was there."

Full runbook: docs/testing/2026-07-31-migrate-env-to-state-tree.md
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from collections import defaultdict
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


def path_to_key(path):
    """Claude Code's `projects/` directory-key mangling for an absolute cwd.

    Ambiguous to reverse (a literal dash in a directory name is indistinguishable
    from a mangled slash), which is exactly why ancestor/leaf detection below
    only ever compares against keys that actually exist on disk, never tries to
    decode one back into a hypothetical path.
    """
    return '-' + str(path).strip('/').replace('/', '-')


def is_running(name):
    try:
        out = subprocess.run(
            ['docker', 'ps', '--filter', f'name={name}', '--format', '{{.Names}}'],
            capture_output=True, text=True, timeout=5)
        return name in out.stdout.split()
    except Exception:
        return False


def all_project_keys():
    d = Path.home() / '.claude' / 'projects'
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir())


def history_project_paths():
    """Map every `projects/` key seen in history.jsonl back to its literal cwd(s).

    Built from real recorded data rather than guessed, since key mangling can't
    be reversed reliably — this is how the discovery report can show an actual
    path for an ancestor key instead of just the opaque dashed name.
    """
    src = Path.home() / '.claude' / 'history.jsonl'
    paths = defaultdict(set)
    counts = defaultdict(int)
    if not src.exists():
        return paths, counts
    for line in src.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            project = json.loads(line).get('project')
        except ValueError:
            continue
        if not project:
            continue
        key = path_to_key(project)
        paths[key].add(project)
        counts[key] += 1
    return paths, counts


def discover(env_name, source_keys):
    """Other keys worth the caller's attention: forgotten leaves, commingled ancestors."""
    keys = all_project_keys()
    source_set = set(source_keys)
    suffix = '-' + env_name.replace('/', '-')
    leaf_others = [k for k in keys if k not in source_set and k.endswith(suffix)]

    ancestors = {}
    for k in source_keys:
        anc = [o for o in keys if o != k and k.startswith(o + '-')]
        for o in anc:
            ancestors.setdefault(o, set()).add(k)

    return leaf_others, ancestors


def print_discovery_report(env_name, source_keys):
    leaf_others, ancestors = discover(env_name, source_keys)
    paths, counts = history_project_paths()

    if leaf_others:
        print("  [?] other projects/ keys end with this env's name but were not")
        print('      requested — pass --legacy-cwd <path> to include them:')
        for k in leaf_others:
            known = ', '.join(sorted(paths.get(k, ()))) or '(no history.jsonl lines — path unknown)'
            print(f'      {k}   {known}')

    if ancestors:
        print('  [?] ancestor directories exist — sessions recorded at a shared')
        print('      parent cwd, commingled across whatever else lived under it.')
        print('      NOT migrated automatically; read them before deciding:')
        for anc_key, children in sorted(ancestors.items()):
            known = ', '.join(sorted(paths.get(anc_key, ()))) or '(path unknown)'
            n = counts.get(anc_key, 0)
            for_ = ', '.join(sorted(children))
            print(f'      {anc_key}   {known}  ({n} history.jsonl lines, parent of {for_})')

    if leaf_others or ancestors:
        print()


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
    canonical_cwd = f'{CONTAINER_HOME}/{user}/{env_name}'

    legacy_cwds = list(args.legacy_cwd or [])
    cwds = [canonical_cwd] + legacy_cwds
    pairs = [(c, path_to_key(c)) for c in cwds]

    seen = set()
    keys = []
    for _, k in pairs:
        if k not in seen:
            seen.add(k)
            keys.append(k)

    tree = Path.home() / '.local' / 'state' / 'dax' / 'tenants' / tenant / env_name

    print(f'\n  env        {env_name}  (tenant: {tenant})')
    print(f'  container  {container}  [stopped]')
    print(f'  tree       {tree}')
    for c, k in pairs:
        tag = 'canonical' if c == canonical_cwd else 'legacy'
        print(f'  {tag:9}  {c}  ({k})')
    print()

    print_discovery_report(env_name, keys)

    return config, project, tree, cwds, keys


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


def copy_projects(keys, tree, apply_it, force):
    src_root = Path.home() / '.claude' / 'projects'
    for key in keys:
        src = src_root / key
        dst = tree / 'projects' / key
        if not src.exists():
            print(f'  [--] no transcripts at {src} — nothing to copy')
            continue
        size = sum(f.stat().st_size for f in src.rglob('*') if f.is_file())
        sessions = len(list(src.glob('*.jsonl')))
        memory = (src / 'memory').is_dir()
        print(f'  [->] {key}: {sessions} session(s), '
              f'{size / 1e6:.1f}MB{", memory/" if memory else ""}')
        print(f'       -> {dst}')
        if dst.exists() and not force:
            print('  [!!] destination already exists. Re-run with --force to overwrite,')
            print('       or move it aside — merging two transcript sets is not safe.')
            continue
        if not apply_it:
            continue
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, symlinks=True)
        print('       copied.')


def filter_history(cwds, tree, apply_it):
    src = Path.home() / '.claude' / 'history.jsonl'
    dst = tree / 'history.jsonl'
    if not src.exists():
        print('  [--] no shared history.jsonl')
        return
    cwd_set = set(cwds)
    kept, total = [], 0
    per_cwd = defaultdict(int)
    for line in src.read_text().splitlines():
        if not line.strip():
            continue
        total += 1
        try:
            project = json.loads(line).get('project')
        except ValueError:
            continue
        if project in cwd_set:
            kept.append(line)
            per_cwd[project] += 1
    for c in cwds:
        print(f'  [->] history.jsonl: {per_cwd.get(c, 0)} lines are {c}')
    print(f'       {len(kept)} of {total} total -> {dst}')
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
    ap.add_argument('--legacy-cwd', action='append', metavar='PATH',
                    help='an additional historical absolute cwd this env used '
                         'before it had its own container (repeatable) — e.g. a '
                         'nested path from before a directory split. Its '
                         'projects/ key and matching history.jsonl lines are '
                         'copied alongside the canonical key, kept in its own '
                         "directory rather than merged (Claude Code's /resume "
                         'is scoped to the current cwd, so merging would not '
                         'make it resumable anyway).')
    args = ap.parse_args()

    config, project, tree, cwds, keys = plan(args.env, args)
    user = args.user or container_user()

    if not args.apply:
        print('  DRY RUN — nothing will be written. Re-run with --apply.\n')

    copy_projects(keys, tree, args.apply, args.force)
    print()
    filter_history(cwds, tree, args.apply)
    print()
    check_credential(tree, args.apply, args.clear_credential)
    print()
    check_claude_json(tree, user, args.env)

    print(f'\n  Next, if the above looks right:')
    print(f'    dax env set {args.env} features claude_tenant_state')
    print(f'    cd {project.get("dir", "<env dir>")} && dax -t run')
    print(f'  Then inside the container:')
    print(f'    ls ~/.claude/projects/   # {", ".join(keys)}')
    print(f'    ls ~/.claude.json        # must NOT exist')
    print(f'    claude                   # /resume lists the migrated sessions')
    print(f'\n  The source is left in place. Remove it only after a successful '
          f'session\n  and a container restart:')
    for key in keys:
        print(f'    rm -rf ~/.claude/projects/{key}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
