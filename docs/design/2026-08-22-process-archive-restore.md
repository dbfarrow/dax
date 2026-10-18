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
`_default_backup_dir(config)` helper used by both, rather than hardcoding
the path twice. A top-level `backup_dir:` in `~/.dax.yaml` overrides it —
real BCP/DR media (an external drive, a NAS share) is the point, but
pointing there trades away the "survives a container rebuild" guarantee,
so that's a choice the user opts into explicitly rather than one dax
assumes for them.

Archive layout is flat within a rotation tier, never nested per project —
the filename and the manifest inside the tarball already carry every bit
of identity a per-project directory would add, so nesting by name bought
nothing:

```
backup_dir/daily/<name>-<YYYY-MM-DD>.tar.gz
```

`daily` anticipates rotation (still not built — see below): weekly/
monthly/quarterly will land as sibling flat directories under `backup_dir`,
so rotating a file is just moving it between two flat directories, never
restructuring a per-project tree.

No rotation means a second archive call on the same day overwrites that
day's file — acceptable for now, and reconsidered when rotation is built.

A missing/moved project directory is skipped with a warning, never fatal
to the rest of the batch — the whole point of `--all` is to protect
everything on the machine in one call, so one stale registry entry must
not be able to sink every other env's backup with it. `--dry-run` runs the
same existence check, so it predicts exactly what the real run will skip
rather than only discovering the problem once the flag comes off.

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

**Superseded 2026-08-30: the picker is single-select, one-at-a-time, not a
checkbox multi-select — and it grew a "move restore" option.** Live-tested
on the host: the checkbox-batch design below was the original plan, but
actual usage (deciding what to do with one archived snapshot at a time,
sometimes wanting to restore it as a *second*, separate env alongside a
still-live original rather than overwrite anything) fit a different shape
better. What shipped:

```python
process_restore_p = process_sub.add_parser('restore', help='Restore one or more archived processes')
process_restore_p.add_argument('name', nargs='*', help='Env name(s) to restore, non-interactively and in place (omit for the interactive picker)')
process_restore_p.add_argument('--all', action='store_true', help='Restore every process with an available archive, non-interactively and in place')
process_restore_p.add_argument('--dir', help='Relocate: restore to this path instead of the archived dir (only valid with exactly one explicit name)')
process_restore_p.add_argument('--dry-run', action='store_true')
```

`--dir` only makes sense against one unambiguous target, so it's rejected
(exit 1) whenever it's combined with `--all`, the picker (no name given), or
more than one explicit name.

`_run_process_restore(config, args)`:
1. Scan `backup_dir/daily/*.tar.gz` (the only tier that exists until
   rotation is built — once weekly/monthly/quarterly land as sibling flat
   directories, this scan extends to all of them), parse each filename back
   into `(name, date)` via a `-YYYY-MM-DD.tar.gz` suffix pattern — safe even
   though `name` itself may contain hyphens, since the date suffix has a
   fixed, unambiguous shape — and keep the newest file per name
   (lexicographic date comparison works directly on the parsed date).
2. No archives found → print a message and return.
3. `args.all` or explicit `args.name` → **non-interactive, restore in
   place**, one call per name, independently (one skip shouldn't abort the
   batch — see `_restore_in_place` below). This is the scripted/
   full-machine-rebuild path.
4. Neither given → the **interactive picker**, one at a time
   (`_run_process_restore_interactive`):
   - Loop: `_q_select` (single-select, not `_q_checkbox` — restore is rare
     and low-volume, and deciding what to do with one pick before ever
     seeing the next fits better than checking several boxes up front) over
     `"<name> (archived <date>)"` for every available archive, plus a
     `Done` choice; a bare ESC/Ctrl-C is treated the same as picking `Done`.
     Picking `Done` (or nothing available) ends the command.
   - For whichever name is picked, a second `_q_select`: **"Restore in
     place"**, **"Move restore (new name and location)"**, or **"Cancel"**
     (back to the top-level list, re-showing `[currently registered]` next
     to any name that changed since the last time through).
   - After handling the pick (or cancelling), loop back to the top-level
     list — several can be handled in one sitting without re-invoking the
     command.

**`_restore_in_place(config, name, archive, args)`** — the logic behind
both the non-interactive path and the picker's "Restore in place" choice:
   - Peek the manifest inside the tarball (same "read just the manifest
     member first" approach `_run_process_import` uses).
   - If `name` is **already registered**: target dir = its *currently
     registered* `dir` (not the manifest's, in case it moved). `--dry-run`
     reports that it *would* prompt to confirm and returns — critically,
     **before** ever calling `_q_confirm`, not after: an early version
     checked `args.dry_run` only after the confirm call, which meant
     `--dry-run` against an already-registered name blocked on stdin same
     as a real run, found live when a scripted `--dry-run` test hung.
     Otherwise prompts `"<name> is already registered at <dir> — restore
     over it? [y/N]"`; a decline skips just this one.
   - If not registered: target dir defaults to the manifest's `dir` field —
     that's what makes capturing it at archive time worth doing, restore
     lands back exactly where it came from with no prompting in the common
     case. `--dir` overrides it (single-explicit-name path only, per the
     restriction above). Refuses (skips, doesn't abort the batch) if the
     resolved path already exists and is non-empty (leftover, unrelated
     content — not the restore-over case) or fails
     `_process_dir_conflict` (nested in/around, or colliding with, another
     registered project — same guardrail `process new`/`import` already
     use).
   - Extract via the shared `_extract_process_archive` helper (below).

**`_restore_moved(config, name, archive, manifest, args)`** — the picker's
"Move restore" choice, new this session: restores the picked snapshot under
a **different name and directory**, leaving whatever's currently registered
under the original name (if anything) completely untouched. Useful both for
pulling an old snapshot back as a second, independent copy alongside a
still-live original, and for relocating a process whose original path no
longer makes sense.
   - Prompts for a new env name; re-prompts (not a hard abort) on a
     collision with anything already registered, or on a blank answer
     (which cancels).
   - Prompts for a new directory; re-prompts on an occupied path or a
     `_process_dir_conflict` failure, same non-fatal-retry treatment. This
     is why `_check_process_dir` got split into a pure
     `_process_dir_conflict(config, dir) -> message | None` and a thin
     `sys.exit`-on-failure wrapper — the interactive retry loop needs the
     pure form; every existing caller (`process new`, `import`) keeps using
     the wrapper unchanged.
   - If the manifest carries a state tree, prompts for a tenant (default:
     the manifest's own), refusing the move if it ends up blank — same
     "no tenant, no state tree" rule the non-interactive path enforces.
   - Prints a full-paths confirmation summary, then a plain `Y/n` (name
     already picked at this exact command, so risk of restoring the *wrong*
     thing is low — that's the destroy/archive-`--remove` distinction
     the confirmation-bar decision already draws) before extracting.

**`_extract_process_archive(config, archive, manifest, name, process_dir, tenant)`**
— the one extraction+registration core shared by every restore path
(in-place, moved, batch/`--all`): extract via `filter='tar'` (matching
import — substrate state trees carry absolute-target symlinks that `'data'`
rejects), `shutil.rmtree` any existing target dir/state tree first since a
restore is always a deliberate replace, never a merge, then move `project/`
and (if `had_state_tree`) `state/` into place from the tar's temp extraction
dir, then `register_project` + `run_env_set` for
`tenant`/`features`/`mounts`/`substrate` from the manifest — identical calls
to what import already makes. Returns the manifest's `creds` list so the
caller can print `dax creds login <name>` reminders (never a login
reminder for a declined/skipped restore).

## Files touched

- `dax.py`:
  - `_default_backup_dir(config)` — base directory for both `dax backup`
    and `process archive`/`restore`; a top-level `backup_dir:` in
    `~/.dax.yaml` overrides the repo-checkout default (an external
    drive/NAS share is the real BCP/DR target, at the cost of the
    survives-a-rebuild guarantee the repo-relative default has).
  - `_archive_path_for(backup_dir, name, today=None)` —
    `backup_dir/daily/<name>-<date>.tar.gz`, flat within the tier (no
    per-project subdirectory — the filename and the manifest already carry
    every bit of identity that would add) so weekly/monthly/quarterly can
    land later as sibling flat directories with no restructuring.
  - `_write_process_archive`, `_teardown_process` extracted from the
    existing export/destroy code, shared with `archive`/`archive --remove`.
  - `_process_dir_conflict(config, dir) -> message | None` extracted from
    `_check_process_dir` (which is now a thin `sys.exit`-on-failure
    wrapper around it) — the pure form the move-restore reprompt loop
    needs, since a bad directory there should ask again, not exit.
  - `_run_process_archive` — single status line per target under one
    header (destination + date printed once, not repeated per line), name
    column padded so directory paths line up; a missing/moved project
    directory skips that one target with a warning rather than aborting
    the whole batch (the earlier version hard `sys.exit(1)`'d deep inside
    `_write_process_archive`, which `--dry-run` never exercised, so a
    clean dry run gave no warning the real run would later abort);
    `--remove`'s existing running-container guard is unaffected structurally.
  - `_available_archives`, `_read_archive_manifest`,
    `_extract_process_archive`, `_print_restore_login_reminders`,
    `_restore_in_place`, `_restore_moved`,
    `_run_process_restore_interactive`, `_run_process_restore` — see the
    restore design above.
  - Both subparsers (`archive`, `restore`) and their `cmd_process` dispatch
    branches; `dir` added to the export manifest.
- `.dax.yaml.example` — documents `backup_dir:`.
- `tests/test_dax_process_archive_restore.py` (new) — archive-side coverage:
  default archive leaves the env untouched; `--all`/multiple explicit
  names/picker; a missing directory is skipped (not fatal) and `--dry-run`
  predicts the same skip; every-target-missing returns cleanly; `--remove`
  tears down only after one combined confirmation and refuses the whole
  batch up front if any target's container is running; declining the
  remove confirmation keeps the env; dry-run writes nothing; unknown name
  exits.
- `tests/test_dax_process_restore.py` (new) — restore-side coverage:
  non-interactive restore of an unregistered name lands at the archived
  dir; `--dry-run` writes nothing and, critically, does **not** call
  `_q_confirm` for an already-registered name (the hang regression);
  restore-over-registered replaces content after confirm, and a decline
  leaves it untouched; `--all`; unknown name exits; no-archives-found
  message; `--dir` relocates a single unregistered restore and is rejected
  with `--all` or multiple names; the interactive picker: `Done` changes
  nothing, pick-then-in-place, move-restore to a fresh name/dir (original
  left untouched), reprompt on a name collision, cancel on a blank name.

## Verification

- `python -m pytest tests/test_dax_process_archive_restore.py
  tests/test_dax_process_restore.py -v` plus the full suite
  (`python -m pytest`) to confirm no regressions in
  `destroy`/`export`/`import`, since this reuses their internals.
- Manual pass with a scratch `HOME`/`~/.dax.yaml` (same pattern used earlier
  in this project for `process new`/`destroy` dry runs), confirmed live this
  session: register fake projects, `dax process archive --all` (including a
  deliberately-missing directory among the targets, to confirm the skip
  behavior), confirm the table-aligned per-target output and that only the
  missing one is skipped; `dax process archive foo --remove` with
  confirmation, confirm teardown matches `destroy`'s effect; a full
  archive → wipe/deregister → `restore foo --dry-run` → `restore foo`
  round-trip confirming byte-identical content recovery and a correct
  registry entry; `restore foo --dry-run` against a still-registered `foo`
  confirmed it reports without blocking on stdin (the hang regression,
  reproduced live before the fix and confirmed absent after). The
  interactive picker and move-restore were covered only by mocked
  `_q_select`/`_q_confirm`/`_prompt` in the test suite until this
  environment's lack of a real TTY meant the actual terminal path went
  untested — and it mattered: the user's own first live run on the real
  host crashed picking "Done" (`KeyError: 'Done'`).

  **Real bug found live, 2026-08-30: `questionary.Choice(title,
  value=None)` does not store `None`.** questionary's own default for an
  omitted `value` argument is `None`, so passing it explicitly is
  indistinguishable from not passing it, and questionary silently
  substitutes the choice's *title string* instead — confirmed directly
  (`questionary.Choice('Done', value=None).value == 'Done'`). Two spots hit
  this: the top-level picker's "Done" choice (`if picked is None: return`
  never fired, falling through to `available['Done']` → the reported
  `KeyError`), and the mode-picker's "Cancel" choice — worse than a crash,
  since `if mode is None: continue` also never fired, silently falling into
  the `else` branch and starting a move-restore the user never asked for.
  Root cause: the unit tests mocked `_q_select`'s return value directly
  (`lambda *a, **k: None`), which encoded the exact same wrong assumption
  the code made, so they could never have caught this regardless of count.
  Fixed with two module-level sentinel objects (`_RESTORE_DONE`,
  `_RESTORE_CANCEL` in `dax.py`) compared with `is` — confirmed a plain
  `object()` round-trips through `Choice` correctly, unlike `None`. Tests
  updated to drive the real sentinels instead of `None`, plus a new
  regression test asserting `_prompt` is never called when "Cancel" is
  chosen (guarding the silent-move-restore case specifically) and a
  guard test asserting a real `questionary.Choice` returns the sentinel,
  not the title — so reintroducing `value=None` anywhere in this flow
  fails a test immediately rather than waiting for another live run to
  find it. 2 new tests, suite at **600**.

  **Second real bug found live, same session, immediately after: a full
  interactive move-restore (`fabric` → `denim`) got through the entire
  prompt sequence — picker, mode choice, new name/dir/tenant, the full
  preview, the `Proceed?` confirm — and then crashed extracting the
  archive: `TypeError: extractall() got an unexpected keyword argument
  'filter'`.** `tarfile.TarFile.extractall()`'s `filter=` keyword didn't
  exist before Python 3.12 (PEP 706); the real host runs its own Python
  3.9 (`/Users/dfarrow/Library/Python/3.9/bin/dax`), and this repo's
  `pyproject.toml` declares `requires-python = ">=3.7"` — 3.9 is a real
  supported target, not an edge case. This dev/test sandbox runs a newer
  Python, so every prior test run — including `_run_process_import`'s own
  pre-existing use of the identical `filter='tar'` call, unrelated to this
  session's work — exercised only the 3.12+ path and could never have
  surfaced this. Fixed with `_extractall_permissive(tar, path)`
  (`dax.py`): uses `filter='tar'` when `hasattr(tarfile, 'tar_filter')`
  (the documented feature-detection marker for the whole PEP 706
  mechanism) and falls back to plain `extractall()` otherwise — pre-3.12
  has no filtering concept at all, so the fallback is simply the old,
  always-permissive-about-symlinks behavior every version before this fix
  effectively ran anyway. Both call sites (`_run_process_import` and
  `_extract_process_archive`) now go through it. Verified two ways: a
  direct manual check that both branches actually extract (not just parse)
  by deleting `tarfile.tar_filter` at runtime to force the fallback, and a
  matching regression test doing the same via `monkeypatch.delattr`. 1 new
  test, suite at **601**.

  **Third real bug found live, same session: a leftover keystroke silently
  "chose" the next item in the restore picker's loop.** After confirming
  "Proceed? Yes" for the `fabric` → `denim` move-restore (which now
  succeeded, following the previous fix), the loop presented the
  next-iteration picker — and it appeared to jump straight into
  `a-priest-and-an-imam`'s mode-choice screen without the user ever
  deliberately selecting it from the list. Most likely cause: a habitual
  double Enter confirming the previous prompt left a keystroke sitting in
  the terminal's input queue, which the *next* `questionary.select(...)`
  consumed the instant it started reading, resolving to its
  first/default-highlighted choice before the user ever saw the list.
  This is a known hazard of chaining several interactive prompts in a
  loop, not specific to `denim`/`fabric` or to this data.

  Fixed centrally, not just in the restore loop: `_flush_stdin()`
  (`dax_creds/init.py`) does a best-effort `termios.tcflush(..., TCIFLUSH)`
  right before asking, added to all four prompt wrappers
  (`_q_text`/`_q_select`/`_q_confirm`/`_q_checkbox`) — so every interactive
  prompt in the tool is protected, not only the ones `process restore`
  added this session. POSIX-only and deliberately swallow-everything
  (`except Exception: pass`): a non-TTY stdin (piped/scripted input,
  which several existing flows and most tests rely on to feed answers on
  purpose) must never have its input eaten, so an unavailable/inapplicable
  flush has to fail silent, not loud.

  Caught a real test-writing mistake while covering this: the first
  version of `test_flush_stdin_calls_termios_tcflush_when_available`
  passed only under `-s` (capture disabled) and silently asserted nothing
  under the suite's normal captured run — pytest's default capture
  replaces `sys.stdin` with an object whose `.fileno()` raises, which
  `_flush_stdin`'s own broad except correctly swallows in production, but
  which meant the test's mock was never reached and the assertion was
  comparing `0 == 1` against a test that had actually done nothing.
  Fixed by faking `sys.stdin` directly (a stub with a working `.fileno()`)
  rather than depending on whichever real-or-captured stdin pytest happens
  to be running under — deterministic regardless of capture mode, which
  matters since the full suite always runs captured. 6 new tests (a bare
  no-op-without-a-real-terminal check, the corrected tcflush-call check,
  and one parametrized "each wrapper flushes before asking" case per
  wrapper), suite at **607**.

  **Superseded immediately after, same session: the picker doesn't loop
  back for another pick anymore — it does one restore and returns.** The
  `_flush_stdin` fix above treated the symptom (a leftover keystroke
  bleeding into the next prompt); the user pushed back on the framing
  (attributing the trigger to a "habitual double Enter" read as blaming
  the user for a robustness gap that was dax's to close) and asked the
  sharper question directly: why does the tool loop into another restore
  at all, instead of finishing the one requested and exiting? That's a
  better fix than the defensive one — it removes the entire class of
  "chained interactive prompts" risk for this flow rather than only
  guarding against it, and it's also just the simpler, more expected
  behavior. `_flush_stdin` stays (it still protects the multi-step
  move-restore Q&A within a single restore, and every other interactive
  prompt in the tool), but `_run_process_restore_interactive` is no longer
  a `while True:` loop: pick one archive, choose in-place or move, handle
  it, return. The top-level "Done" choice is gone with the loop it existed
  for; "Cancel" (a single `_RESTORE_CANCEL` sentinel, reused at both the
  picker and the mode-choice level) is what backs out without doing
  anything, at either point. 1 new test
  (`test_interactive_restore_is_one_shot_not_looping`, asserting exactly 2
  `_q_select` calls total — a regression back to looping would be a third
  call, which the existing tests' now-untrailed `iter([...])` sequences
  would also catch on their own via `StopIteration`), suite at **608**.

  **Fourth real bug found live, same session: a move-restored process had
  no `/resume` history, even though the directory contents were correct.**
  `fabric` restored (moved) as `denim` — the file tree looked right, but
  Claude Code's own session picker showed nothing for `denim`, while
  `fabric`'s sessions were still intact. Root cause: Claude Code's
  `~/.claude/projects/<key>/` session index (what `/resume` reads) is
  keyed by the **container's absolute cwd** —
  `<container_home>/<basename-of-project-dir>`, exactly where
  `feature_workdir` mounts a project (`dax.py`'s `_container_home` +
  `workdir_name`). `_extract_process_archive` (and `_run_process_import`,
  which has the identical shape) moved the archived state tree's
  `projects/` directory into place **verbatim** — still named for
  `fabric`'s old key (`-home-<user>-fabric`) — while `/resume` looks up
  sessions by `denim`'s own *current* cwd key (`-home-<user>-denim`).
  Right data, wrong key, so it was invisible. This is the same cwd-keying
  behavior `tools/migrate_env_state.py` (a separate, pre-existing tool)
  already had to account for — restore/import just reproduced the same gap
  in a new place.

  Fixed with `_relocate_claude_project_key(dest_state, old_dir, new_dir)`
  (`dax.py`): renames any `projects/<key>/` entry whose name ends in
  `-<old-basename>` to end in `-<new-basename>` instead — a no-op when the
  basename didn't change (the common in-place-restore case, or an export/
  import to a same-named directory). Deliberately doesn't need to know the
  container's home path or username: the mangled key only ever differs in
  its trailing segment, so matching on that suffix alone and rewriting
  just that part is enough, regardless of what precedes it. Wired into
  both `_extract_process_archive` (restore — in-place, moved, batch) and
  `_run_process_import`'s own state-tree extraction, right after the
  `state/` move, guarded on `manifest.get('dir')` existing (older archives
  from before this session added the `dir` field skip the rename rather
  than erroring — no worse than before this fix). Verified two ways: a
  from-scratch reproduction of the exact `fabric`→`denim` scenario (a fake
  `projects/-home-dfarrow-fabric/` directory with a transcript file,
  archived and move-restored for real, confirming the file survives the
  rename byte-for-byte at its new `-home-dfarrow-denim` path) and 4 new
  tests — 3 unit-level on `_relocate_claude_project_key` directly (rename,
  no-op-when-unchanged, no-`projects/`-dir-at-all) plus one integration
  test through the real move-restore path end to end. Suite at **612**.

  **Known remaining gap, not yet addressed:** `.claude.json` inside a
  state tree is also keyed by project path (per the state-tree-migration
  work's own finding — "a container's `~/.claude.json` already has just
  the one project key" — trust/`allowedTools`/tips settings live under
  that key). A move-restore likely leaves that key stale the same way
  `projects/` did, meaning a restored env may re-prompt for trust it
  already had. Lower-impact than losing session history (worst case is a
  re-prompt, not lost data), and not reported as a symptom this session —
  flagged here rather than fixed, since fixing it needs to read and
  rewrite JSON rather than rename a directory, a different shape of change.
