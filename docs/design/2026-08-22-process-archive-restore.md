# `dax process archive` / `dax process restore`

## Context

This feature exists for BCP/DR, full stop — it is not a tidiness convenience
for archiving finished processes, and that's not incidental framing, it's
what should drive every design call below (confirmation bars, defaults,
`--all`, unattended-run friendliness). For most of these process repos,
`dax process archive` *is* the backup store, replacing GitHub rather than
supplementing it: plenty have no remote configured at all, many don't belong
on GitHub in the first place (client-confidential content, credentials-
adjacent material, whatever), and git being present elsewhere is not a
safety net this feature gets to lean on. Where a repo *does* have a GitHub
remote, this is still the intended primary backup — GitHub is not assumed to
be doing that job.

Because of that, the mechanism has to work as a real backup tool first:
capable of protecting everything on the machine in one call, and runnable
repeatedly against processes that are still active and ongoing — a daily or
near-daily habit, not a one-shot "this is done, put it away" action. The
single-process archive/unarchive case (a process wraps up, gets snapshotted
and taken out of the active registry, occasionally pulled back in) is real
but secondary; it should fall out of the same mechanism, not shape it. Full
machine-loss restore stays the uncommon path in practice, but the feature
exists because that path has to work, not despite it.

The `export`/`import` and `destroy` commands already give dax a way to move
a process to another machine or to permanently delete one. `archive` and
`restore` are new `dax process` subcommands built by recombining what those
already do: package a process the same way export does, optionally tear it
down the same way destroy does (but gated behind an explicit flag, off by
default), and restore it the same way import does (but sourced from a
well-known backup location via an interactive multi-select in the common
case, rather than requiring an explicit `--dir`/`--tenant` flag for every
target — though `--dir` remains available to relocate a single target away
from its archived location).

Decisions locked in with the user:

- Teardown after archiving is **opt-in via `--remove`**, not the default —
  archiving is a pure, non-destructive snapshot unless asked otherwise. This
  is what makes plain `archive` (no `--remove`) the actual backup mechanism.
- `archive` must support **multiple names or `--all`** in one call, not just
  one process at a time — a BCP tool that can only protect one thing per
  invocation doesn't scale to "back up everything before I travel" or, once
  rotation exists, to an unattended cron run.
- No git-uncommitted-changes warning on archive — that's `destroy`'s own
  concern and unrelated here.
- No "run `dax creds login`" reminder after restore — first launch handles
  that itself.
- Restoring over an already-registered name is a **valid, explicit use
  case** — prompt to confirm, don't refuse.
- No rotation (daily/weekly/monthly) yet — one permanent snapshot per
  archive call, and repeated calls simply accumulate more dated files with
  no cleanup. That's an accepted, temporary gap — but not a rare one:
  plain `archive` (no `--remove`) is expected to run regularly, up to
  daily, from the start, so unbounded per-day file growth starts
  accumulating immediately rather than being a someday concern. Rotation is
  the obvious near-term follow-up once this ships, just not part of this
  build.
- Usage is asymmetric and the design should fit that shape: `archive`
  (backup) is high-frequency, routine, and ideally unattended — no
  confirmation prompt on a plain archive call (already true — it's
  non-destructive). `restore` is low-volume and normally targets a small,
  explicit set of processes; the interactive picker and `--all` exist for
  the rare full-machine-rebuild case, not the common path.

## Design

### Shared packaging, reused rather than duplicated

`_run_process_export` (`dax.py:1859-1960`) already builds the manifest dict
(`name`, `tenant`, `image`, `creds`, `features`, `mounts`, `substrate`,
`had_state_tree`) and writes the tarball (`dax-export-manifest.json` +
`project/` via `_export_tar_filter` + `state/` via
`_state_tree_tar_filter(include_credential)`). Extract that manifest-build +
tar-write portion into a helper:

```python
def _write_process_archive(config, name, out_path, include_credential=False):
    ...
    return manifest  # for the caller to print a summary from
```

`_run_process_export` calls it with `out_path=args.out`. The new
`_run_process_archive` calls it with a computed path under the backup
directory. One addition to the manifest: a `dir` field (the project's
original absolute path) — harmless for export/import (which still require an
explicit `--dir` and ignore it), but needed by `restore` to know where to put
an unarchived process back without asking.

Backup location reuses the existing convention from `_run_backup`
(`dax.py:2283-2385`, `Path(__file__).parent / 'backup'`, already gitignored,
already chosen specifically because it survives a container rebuild while
`~/.local/state/dax/...` would not). Factor that default into a small
`_default_backup_dir()` helper used by both, rather than hardcoding the path
twice. Archive layout:

```
backup/<name>/archive/<name>-<YYYY-MM-DD>.tar.gz
```

No rotation means a second archive call on the same day overwrites that
day's file — acceptable for now, and reconsidered when rotation is built.

### `dax process archive [name ...]`

```python
process_archive_p = process_sub.add_parser('archive', help='Snapshot one or more processes into a permanent backup')
process_archive_p.add_argument('name', nargs='*', help='Env name(s) to archive (omit for a picker)')
process_archive_p.add_argument('--all', action='store_true', help='Archive every registered env, no picker')
process_archive_p.add_argument('--remove', action='store_true',
    help='Also remove each env after a successful archive (default: snapshot only)')
process_archive_p.add_argument('--include-credential', action='store_true')
process_archive_p.add_argument('--dry-run', action='store_true')
```

`_run_process_archive(config, args)` — target resolution mirrors `destroy`'s
existing shape (`dax.py:2150-2170`) exactly, plus the new `--all` escape
hatch that skips the interactive picker for unattended/bulk use:

1. Resolve the target set: `args.all` → every registered project name;
   explicit `args.name` → those (exit 1 on any unknown name); neither →
   `_q_checkbox` over all registered projects, nothing pre-checked (same
   widget, same "nothing pre-checked" convention `destroy` already uses).
2. `args.dry_run` → for each target print what would be written (and, if
   `--remove`, what would be torn down) and return — no writes.
3. Archive every target unconditionally — this step is non-destructive
   regardless of how many targets there are, so it needs no confirmation
   even in bulk. For each: call `_write_process_archive(...)`, print the
   resulting path.
4. If not `--remove`: done.
5. If `--remove`: **after all archives have succeeded**, apply destroy's
   existing batch-atomicity guard across the whole target set — check every
   target's container isn't running *before* removing any of them (refuse
   and exit if one is, same as `destroy`, `dax.py:2172-2180`) — then print
   one combined summary of everything about to be torn down (directory,
   state tree, derived creds per env — same style as destroy's summary,
   minus the git-uncommitted-changes note) and confirm once with a plain
   Y/n (not destroy's typed `DESTROY` — the data is already safely archived
   at this point, so the bar is lower, and a bulk `--remove` shouldn't
   demand N separate confirmations). Then loop the teardown: same calls
   `_run_process_destroy` makes (`dax.py:2235-2261`) — `shutil.rmtree` the
   project dir, `shutil.rmtree` the state tree + unlink its shared-files
   manifest, `run_creds_remove` per derived credential, delete the
   `~/.dax.yaml` entry — via a small extracted helper
   `_teardown_process(config, name, project)` shared by both `destroy` and
   `archive --remove`, rather than copy-pasting the block. Save once after
   the loop.

### `dax process restore [name ...]`

```python
process_restore_p = process_sub.add_parser('restore', help='Restore one or more archived processes')
process_restore_p.add_argument('name', nargs='*', help='Env name(s) to restore (omit for a picker)')
process_restore_p.add_argument('--all', action='store_true', help='Restore every process with an available archive, no picker')
process_restore_p.add_argument('--dir', help='Relocate: restore to this path instead of the archived dir (single explicit name only)')
process_restore_p.add_argument('--dry-run', action='store_true')
```

`--dir` only makes sense against one unambiguous target, so it's rejected
(exit 1) whenever it's combined with `--all`, the picker (no name given), or
more than one explicit name.

The common case is a small, explicit set of names — someone unarchiving one
or two specific processes. `--all` and the picker exist for the rare
full-machine-rebuild case, not as the primary path.

`_run_process_restore(config, args)`:
1. Scan `backup_dir/*/archive/*.tar.gz`, keep the newest file per env name
   (lexicographic sort works — filenames are `<name>-YYYY-MM-DD.tar.gz`).
2. No archives found → print a message and return.
3. Resolve targets: `args.all` → every env with an available archive;
   explicit `args.name` → those (error on any name with no archive
   available); neither → `_q_checkbox` choices — `"<name> (archived
   <date>)"` — same picker `destroy` already uses, nothing pre-checked.
4. `args.dry_run` → print the selected set and what each would do, return.
5. For each selected name, independently (one skip shouldn't abort the
   batch):
   - Peek the manifest inside its tarball (same "read just the manifest
     member first" approach `_run_process_import` uses).
   - If `name` is **already registered**: target dir = its *currently
     registered* `dir` (not the manifest's, in case it moved). Prompt
     `"<name> is already registered at <dir> — restore over it? [y/N]"`; a
     decline skips just this one and moves to the next.
   - If not registered: target dir defaults to the manifest's `dir` field —
     that's what makes capturing it at archive time worth doing, restore
     lands back exactly where it came from with no prompting in the common
     case. `--dir` overrides it for the single-target invocation it's
     restricted to above. In the picker/`--all`/multi-name paths, where
     `--dir` is unavailable, print the manifest's `dir` as the resolved
     target per env and offer a single `"relocate instead? [y/N]"` prompt;
     accepting asks for the replacement path just for that one env, same as
     `--dir` would have supplied, and declining proceeds with the manifest
     path — so the default stays the zero-friction path and relocation is
     always an explicit opt-in, never required busywork for the normal
     restore-to-original-location case. If the resolved target path already
     exists on disk (leftover, unrelated content — not the restore-over
     case), refuse this one with a clear message rather than silently
     overwriting unknown data, same spirit as import's existing
     non-empty-destination guard.
   - Extract with `filter='tar'` (matching import — substrate state trees
     carry absolute-target symlinks that `'data'` rejects). Since this is a
     deliberate replace, `shutil.rmtree` any existing target dir/state tree
     first rather than attempting a merge, then move `project/` and (if
     `had_state_tree`) `state/` into place from the tar's temp extraction
     dir.
   - `register_project(config, name, process_dir, image, creds)` +
     `run_env_set` for `tenant`/`features`/`mounts`/`substrate` from the
     manifest — identical calls to what import already makes.
   - Print one line per env: restored, skipped (declined), or refused
     (occupied path) — no credential-login reminder.
6. `save_config(config)` once after the loop.

## Files touched

- `dax.py` — extract `_write_process_archive`, `_default_backup_dir`,
  `_teardown_process` from the existing export/destroy code; add
  `_run_process_archive`, `_run_process_restore`; add the two subparsers and
  their `cmd_process` dispatch branches; add `dir` to the export manifest.
- `tests/test_dax_process_archive_restore.py` (new) — follow the `home`/
  `_register`/`_args` fixture conventions from
  `tests/test_dax_process_destroy_cmds.py` and
  `tests/test_dax_process_export_import.py`. Cover: default archive leaves
  the env(s) untouched; `--all` archives every registered env with no
  picker; multiple explicit names in one call; picker archive when none
  given; `--remove` tears down only after a single combined confirmation,
  and refuses the whole batch up front if any target's container is
  running; dry-run for both single and bulk; picker restore;
  restore-over-registered prompts and requires confirm (and a decline skips
  without aborting the batch); restore of a not-currently-registered name
  into its original `dir` with no prompting; `--dir` relocates a single
  explicit-name restore instead of using the manifest's `dir`; `--dir`
  rejected when combined with `--all`, the picker, or multiple names;
  declining the relocate prompt in a picker/`--all`/multi-name restore falls
  back to the manifest's `dir`; refusal when the resolved target path is
  unexpectedly occupied; no-archives-found message; a mixed batch (one
  confirmed, one declined) completes the confirmed one.

## Verification

- `python -m pytest tests/test_dax_process_archive_restore.py -v` plus the
  full suite (`python -m pytest`) to confirm no regressions in
  `destroy`/`export`/`import`, since this reuses their internals.
- Manual pass with a scratch `HOME`/`~/.dax.yaml` (same pattern used earlier
  in this project for `process new`/`destroy` dry runs): register a fake
  project, `dax process archive foo`, confirm the env is untouched and the
  tarball exists; `dax process archive foo --remove` with confirmation,
  confirm teardown matches `destroy`'s effect; `dax process restore` with no
  args, confirm the picker lists `foo`, restore it, confirm the directory,
  state tree, and `~/.dax.yaml` entry are back.
