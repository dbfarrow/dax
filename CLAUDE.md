# dax — Project Context

dax is a Docker-based development environment manager. It reads `~/.dax.yaml`
(and an optional `.dax.yaml` in the cwd) to build a `docker run` command with
features (volume mounts, port mappings, etc.) assembled from named feature
functions in `dax.py`.

## Current state — read this first (2026-10-21)

The env-lifecycle consolidation (`feature/env-lifecycle-consolidation`; design
and all 41 decisions in `docs/design/2026-10-18-env-bootstrap-consolidation.md`)
reshaped dax's CLI and env model. **Sections further down are history: their
command names and mechanisms often predate this**; trust this section and
the code over them.

- **Commands:** `build`, `run`, `features`, `backup`, `creds`
  (`add`/`list`/`update`/`login`/`remove`), `config`, `env`
  (`list`/`show`/`archive`/`restore`/`destroy`).
  - **Gone:** `dax init`, `dax process`, `dax envs`, `dax tenant`/`tenants`,
    `dax env set`, `dax env accept-shared-files`, every `--dry-run`, and
    `--include-credential`.
- **Creating an env:** `dax run` in an unregistered folder runs the
  `_bootstrap_env` wizard. The env is named after the folder. A process env
  gets `new-process.sh` run after the confirm.
- **Editing:** `dax config` (host only), which keeps `~/.dax.yaml.prev` and
  checks the result.
- **An env entry:** `dir` and `tenant` (both required), `image`, `creds`
  (non-Claude only), `features` (opt-ins), `mounts`. A missing list key
  counts as empty.
- **Claude:** always on (`feature_claude`, formerly `claude_tenant_state`),
  per env.
  - The state tree lives at `~/.local/state/dax/tenants/<tenant>/<env>/`,
    and the credential `claude-<tenant>-<env>` is implied, resolved
    centrally in `resolve_credential_names`.
  - Logins write to a temp folder under `~/.local/state/dax/login/`, never
    the host `~/.claude`.
  - Shared `CLAUDE.md`/settings are copied in once.
- **Mounts:** a non-empty `mounts:` turns them on.
- **Substrate:** one global top-level `substrate:`; a process opts in with
  `features: [substrate]`.
- **Retired names:** `claude`/`claude_tenant_state`/`auggie`/`mounts` in
  `features:`, Claude or auggie entries in `creds:`, and the per-env keys
  `substrate`, `multi_tenant` and `tenant_subdir`. All are ignored and
  warned about on every `dax run`.
- **Archive and restore:** archives never hold credentials. Restore only goes
  into an empty, unregistered folder, never overwrites anything, and is named
  after that folder.
- **Removed outright:** auggie support, and the multi-tenant model
  (`dax_creds/tenant.py`).
- **Tests:** 704 passing. Manual runbook:
  `docs/testing/2026-10-19-env-lifecycle-test-sequence.md`.

## Baseline features replace the top-level `features:` list

**Settled and built 2026-08-04**, on `feature/baseline-features` (branched
from `master` after PR #5 merged). `~/.dax.yaml`'s top-level `features:` key
is gone — it was the actual cause of the "additive-only, no opt-out" backlog
item: every env inherited whatever was in that list with no way to decline
one. `workdir`, `dotfiles`, and `webpreview` are wanted by literally every
env, so they're now an unconditional baseline in `dax.py`
(`_ALWAYS_ON_FEATURES`) rather than something read from that list at all.
Everything else that used to live there — `ssh` in particular, per the
decision to move toward `gh`-based GitHub auth instead of ambient
host-agent-forwarding — is opt-in only now: a project's own `features:`
list, a cwd-local `.dax.yaml`, or `-f`. Nobody was trying to opt *out* of
those, so per-env-additive is fine for them; the problem was only ever the
global list forcing something on every env at once.

A still-present, non-empty top-level `features:` key is never silently
dropped: `load_config()` prints what it found and that it's no longer read,
naming the three baseline features so it's clear nothing needs to move there.
The real `~/.dax.yaml` had exactly `workdir, dotfiles, ssh, webpreview` (plus
a commented-out `auggie`) — confirms the change covers the actual live
config with no surprises, and that key has been removed from it directly
(via the ruamel round-trip save, not a raw pyyaml rewrite, so comments and
ordering survive). 4 new tests for the baseline/warning behavior, plus a
rewrite of `test_dax_claude_mount_exclusivity.py`'s fixtures (they used to
put `claude`/`claude_tenant_state` in the now-gone global list — every case
moved to a project's own `features:`, since that's the only source left that
can carry them). Suite at **511** (from 507). `.dax.yaml.example` and this
file's own decision-3 mention of "four sources" for feature accumulation are
both updated to match — it's three now (project, cwd-local, `-f`), plus the
hardcoded baseline that isn't really a "source" in the old sense at all.

## Legacy Claude state: migrate the process now, recover history on demand

**Settled 2026-08-02, closing out the `migrate_env_state.py` redesign thread.**
**Still nothing committed** — this branch has been accumulating uncommitted
work across several sessions (see `git status`). **505 tests passing** (up
from 493): `tests/test_dax_migrate_env_state.py` is new (12 tests), plus the
`tools/migrate_env_state.py` rewrite itself and a doc update. Full detail on
everything else already built is in "In progress" below; this block covers
just this session.

**The decision:** don't build further auto-detection into
`migrate_env_state.py` (no cwd-based env resolution, no auto-including
discovered legacy keys). When a discernment process is split into its own repo
and registered as its own env, migrate the process's *files* — the code,
`context.md`/`inbox.md`, whatever `dax process new`/`new-process.sh` scaffold
— right away, and **don't fret about pulling its old Claude session state
along with it at the same time.** If, later, work resumes on that process and
it's worth asking whether relevant history got left behind, the recovery path
is: mount the real host `~/.claude` into that env (`dax env set <name> mounts
'~/.claude:host-claude'`, the `host:container_name` syntax built for exactly
this), restart, and have a live Claude session crawl `~/host-claude/projects/`
and `history.jsonl` at that point. A live session can *read content* and use
judgment about what's actually relevant to the process at hand — which is
precisely what `migrate_env_state.py` cannot do and deliberately refuses to
guess at (the ancestor/commingled-parent case below). That makes it strictly
better than any heuristic bolted onto the script for the one case that
actually needs a decision, and it defers the work to exactly when it's
valuable instead of speculatively building it now for processes that may never
need their old state resurrected. **Condition for this to keep working: don't
delete `~/.claude/projects/<key>/` or `history.jsonl` entries on the host in
the meantime** — they're the substrate this on-demand recovery depends on;
that discipline was already the existing runbook caution, just now load-
bearing for a second reason.

`migrate_env_state.py` itself stays exactly as built below — explicit
`--legacy-cwd`, discovery report, no auto-inclusion — since it's still the
right tool for the unambiguous case (an env that already knows its own
handful of historical cwds and just wants them copied in mechanically).

**The mounts reconfig from the prior pause landed and worked**:
`~/host-claude/projects/` is visible and confirmed to be the real host
`~/.claude`, not a guess.

**What the real data showed** — checked before designing anything, per the
prior session's own instruction: a discernment sub-process's history is
genuinely strewn across more than one `projects/` key, and not just by rename.
For `self-employment-setup` (historically `~/work/processes/self-employment-setup`,
before dax existed):

- its own nested-path key, `-home-dfarrow-work-processes-self-employment-setup`
  — 1 session, 30MB, 21 `history.jsonl` lines — is unambiguously this
  process's.
- `-home-dfarrow-work-processes` (155 lines) and `-home-dfarrow-work` (1858
  lines) are **ancestor** keys — sessions recorded with cwd at a shared parent,
  before/without `cd`-ing into the specific process directory. These commingle
  every process ever nested under them and cannot be attributed to one process
  by directory alone; reading content would be required, which the tool
  deliberately does not attempt.

**`tools/migrate_env_state.py` rewritten accordingly** (not yet run for real —
this session's testing was read-only dry runs against the mounted
`~/host-claude` via a scratch `HOME`, never against this container's own live
`~/.claude`):
- `--legacy-cwd PATH` (repeatable) supplies historical absolute cwds the tool
  cannot infer on its own (a rename/restructure is exactly what makes the old
  path differ from the new one). Each legacy cwd's `projects/<key>/` copies
  into the tree as **its own directory**, never merged into the canonical
  key's — `/resume` is scoped to the container's current cwd, so merging
  wouldn't make old sessions resumable anyway, while separate directories keep
  provenance honest.
- Every run — with or without `--legacy-cwd` — now prints a **discovery
  report** first: other `projects/` keys ending in the env's name that weren't
  passed (a forgotten legacy cwd), and ancestor keys of anything being
  migrated (the commingled-parent case above), each with a real path and
  `history.jsonl` line count pulled from actual recorded data (`path_to_key`
  can't be reversed reliably on its own — a literal dash in a directory name
  is indistinguishable from a mangled slash — so paths are cross-referenced
  from `history.jsonl`'s own `project` field instead of guessed). Ancestor data
  is reported, never copied.
- Verified against the real `~/host-claude` data via a scratch `~/.dax.yaml` +
  `HOME` override (dry run only, no writes): the discovery report's line counts
  (57 / 1858 / 155) matched the manual `history.jsonl` scan exactly, both with
  and without `--legacy-cwd` supplied.
- `docs/testing/2026-07-31-migrate-env-to-state-tree.md` gained a
  `--legacy-cwd` section with this same worked example.

**Deliberately not done, and not blocking anything — per the decision above.**
This session only dry-run-verified the tool against real data; it hasn't been
run with `--apply`, and `self-employment-setup` / `a-priest-and-an-imam` /
`augmentcode` aren't yet registered as their own dax envs (the real
`~/.dax.yaml` shown earlier in this project's history only has `dax`,
`discernment`, `fabric`, `ys-augmentcode`, `a-priest-and-an-imam`,
`symlink-disclosure`). Registering each as its own env (via `dax process new`)
migrates the process itself; running `migrate_env_state.py --apply
--legacy-cwd ...` against it — or skipping straight to the on-demand mount-
and-crawl recovery — is a later, optional step whenever that process is
actually revisited, not a prerequisite to registering it.

**Half fixed, 2026-08-06 — `CLAUDE.md`/`inbox.md` no longer silently
overwritten.** `new-process.sh`'s scaffolding was too destructive for
migrating a directory that already has its own `context.md`/`inbox.md`
(refused on `context.md`, but silently overwrote `CLAUDE.md`/`inbox.md` with
no check at all if `context.md` happened to be absent). The `inbox.md`/
`CLAUDE.md`-like-`.gitignore` half of the sketched redesign — write only if
absent, print a notice and move on rather than overwrite — is now patched
into `docs/design/scripts/new-process.sh`. **Still needed: the other half**
(merge frontmatter into `context.md` rather than hard-refusing when it
already exists) — not done, since it's a real design question (what does
"merge" mean when the existing file's own content might conflict with the
template's frontmatter) rather than a mechanical write-if-absent change.
**And still outstanding regardless: this is a change to `new-process.sh`
itself, which lives in the virgil/discernment repo, not dax** —
`docs/design/scripts/new-process.sh` here is only an untracked reference
copy, gitignored, never picked up by `dax process new` until it's ported over
by hand to the real script in that repo.

## Completed

- **Auto port assignment for webpreview**: `_find_preview_port(cwd)` hashes the
  working directory to pick a stable port in 8000–8999, scanning forward if busy.
  Explicit `webpreview.port` in config still overrides. `DAX_PREVIEW_DIR` sets the
  server root to the container's work directory (e.g. `/home/dfarrow/work`).
- **`feature_auggie`**: added alongside `feature_claude`, mounts `auggiedir`.
- **SSH test fix + bug fix**: `feature_ssh` now checks existence of the Docker
  Desktop fallback socket too, not just the `SSH_AUTH_SOCK` env var socket.
- **`~/.dax.yaml` mounted rw**: now visible inside container at `~/.dax.yaml`.
- **`.dax.yaml.example`** added to repo root mirroring the actual config.
- **`dax backup` subcommand**: copies all `dotfiles.ro`, `dotfiles.rw`, and
  `backup` config entries into `backup/` under their `~`-relative paths.
  Byte-level comparison — only writes on change. Skips missing paths.
- **`pyyaml` added to Dockerfile.tmpl** pip install (needed by `dax backup`).

- **`dax-preview` GitHub-style UI**: directory listings now use a rounded file table with hover; README.md renders below the listing in a boxed panel with a book icon; a sticky left-side file tree with SVG icons, collapsible folders, and sessionStorage persistence appears on all pages (directory, .md, .json, .yaml). Folder names in the tree navigate; chevron toggles expand/collapse. Heavy dirs (.git, node_modules, etc.) are skipped. Page width auto-expands by 256px to keep content area at the configured width.
- **GitHub host keys baked in**: all three key types (RSA, ECDSA, ed25519) written to `/etc/ssh/ssh_known_hosts` at image build time — no more `ssh-keyscan` on first use.

- **`~/.dax.yaml` round-trip preservation**: `save_config` no longer destroys
  comments or alphabetizes keys. `dax_creds/config.py` gained
  `yaml_round_trip()` (ruamel, `preserve_quotes`, `width=4096` so long `dir:`
  paths aren't wrapped); `load_dax_config` uses it but **falls back to pyyaml**
  when ruamel is missing so reads never break, while `save_config` **refuses**
  rather than falling back, since a pyyaml rewrite is exactly what destroys the
  file. It also serializes fully before opening the file, so a failed dump
  can't truncate the config, and deliberately stays an in-place write rather
  than write-temp-plus-rename — `~/.dax.yaml` is a single-file bind mount in a
  container, and single-file grpcfuse mounts are where atomic rename breaks.
  Verified byte-stable against the real `~/.dax.yaml`, including the
  commented-out `#- claude_tenant_state` toggle. `ruamel.yaml` added to
  `pyproject.toml` and `Dockerfile.tmpl`. **Requires `pip install -e .` on the
  host to take effect** — the host is an editable install, the container is not.

- **Per-env Claude credentials by convention**: a bare `claude` in an env's
  `creds:` list resolves to `claude-<tenant>-<project>` — dax enforces the
  naming instead of the user hand-maintaining it (a typo'd `claud-fre` sat
  unused in `~/.dax.yaml` for weeks). `BARE_PROVIDER_CREDS` in
  `dax_creds/config.py` holds the providers this applies to — claude only,
  since github/gmail/ssh are genuinely user-level. An explicitly named
  credential always wins, keeping old configs working and leaving an escape
  hatch for deliberately sharing one across two envs. A bare token with no
  `tenant` set is **refused**, not silently shared — `cmd_run` exits. Derived
  names that aren't registered yet are synthesized in memory rather than
  written, so resolution stays a pure read and the login flow is what registers
  them. `find_named_project_by_dir` is new (returns `(name, project)`) because
  derived names need the registry name.

- **An abandoned login no longer imports the credential already on disk**:
  `_post_login_import` ran unconditionally after `subprocess.run()`, and its
  claude branch reads `~/.claude/.credentials.json` — so cancelling a login
  stored the *existing shared* credential under the new per-env name. Two
  Keychain entries, one OAuth grant, no isolation, and it authenticated fine so
  nothing looked wrong. Found in live testing 2026-07-30. `cmd_creds_login` now
  snapshots the on-disk token before launching the flow (`_disk_token_for`,
  covering github/claude/auggie) and refuses to import when it is unchanged.
  **Still open:** `dax creds add`'s claude path has no such guard, so per-env
  credentials must be minted with `dax creds login`, not `add`.

- **Derived credentials inherit `credential_defaults`**: a synthesized
  definition had no `browser`/`chrome_profile`, so every per-env login opened
  the default browser instead of the right Chrome profile. A top-level
  `credential_defaults: {claude: {browser:, chrome_profile:}}` block is merged
  into each — the right shape, since browser choice belongs to the human
  authenticating rather than the env. Defaults never override `provider`; an
  explicitly registered credential ignores them.

- **`dax creds list` shows derived credentials** (tagged `(derived)`), so the
  host stops disagreeing with `dax-creds list` in the container. Its "used by"
  column now resolves rather than matching literally — an env listing a bare
  `claude` matched nothing before, and a superseded explicit credential wrongly
  looked in use. `dax creds remove` can also clear a derived credential now
  (Keychain only, no config write); previously it rejected them as unknown, so
  a bad one could not be removed at all.

- **`dax creds add` overwrite actually overwrites**: every `_setup_*_credential`
  now takes `replace=`, set only when the user confirms "Overwrite it?". The
  old `if provider.check(...): return` early-exit made that confirmation a lie —
  YAML metadata was rewritten while the Keychain secret was left untouched, so
  a rotated credential couldn't be re-imported without `dax creds remove` first.
  When a confirmed replace finds nothing new to store, `_report_no_token` now
  says explicitly that the old secret survived and how to clear it.

- **`dax creds remove` removes from both stores**: previously it deleted only
  the Keychain secret, leaving the definition in `~/.dax.yaml` so the credential
  kept appearing in `dax creds list` and a fat-fingered name could never be got
  rid of. Now also drops the config entry and saves — and **refuses** if any
  project still references it, checked before anything is deleted so a refusal
  leaves both stores untouched.

- **`dax envs list` merged with the tenant view**: outer-joins `~/.dax.yaml`'s
  projects against a filesystem walk of `~/.local/state/dax/tenants/`, so an env
  orphaned from *either* direction shows up — a registry entry with no state
  tree, or a state tree with no registry entry (deregistered or renamed, with
  transcripts and memory still on disk). The latter was invisible to every
  previous command and is what the deletion-grouping rationale needs. TENANT,
  STATE, and USED columns added — tree size plus a compact relative age
  (`now`/`3d`/`5w`/`1y`), both from a single walk, since size and recency answer
  different halves of "is this worth keeping". USED is the newest *file* mtime,
  not the directory's, so an in-place edit doesn't read as ancient. Markers are `*` running,
  `!` directory missing, `?` orphan, with a legend for whichever appear. A
  registry row claims its matching tree, so only unclaimed trees are reported —
  and only a *declared* tenant can claim one, since without it there's no path
  to look under. `state_trees()`/`state_tree_path()` live in
  `dax_creds/config.py`. Supersedes `dax tenants`.

- **`dax env show` / `dax env set`**: inspect or edit one environment without
  walking `dax init`'s full wizard. Singular `env` acts on one environment,
  matching the existing `tenant`/`tenants` split. `show` prints the registry
  entry plus resolved container name and running state, the state-tree path and
  whether it exists, and a warning when the entry still carries deprecated
  `multi_tenant`/`tenant_subdir` keys. `set` takes one field —
  `tenant`/`dir`/`image`/`creds` — with `creds` comma-separated and validated
  against defined credentials before writing; `--help` and the unknown-field
  error both print the full field table. `name` is optional in both, defaulting
  to the env containing cwd and falling back to the enclosing project so it
  works from a subdirectory. `ENV_FIELDS`/`env_field_help()` live in
  `dax_creds/config.py`, not `init.py`, so parser construction doesn't import
  keyring on every `dax` invocation.

- **`dax creds login` browser callback works for Claude**: `claude auth login`
  binds a callback listener *inside* the container while the browser opens on
  the *host*, so the localhost redirect could never resolve.
  `_force_claude_device_flow_redirect` (`dax_creds/daemon.py`) rewrites
  `redirect_uri` to Claude's device-flow callback before the URL reaches the
  host browser. Paired with `_post_login_import`'s claude branch, which stores
  the resulting token to Keychain the way the github/auggie branches already
  did. Confirmed working.

## Where this stands — 2026-07-31

> *Historical. Its next-actions list predates the env-lifecycle
> consolidation. `dax env set`, `accept-shared-files` and the shared `claude`
> mount it refers to are gone; see "Current state" at the top.*

Nine commits on `feature/tenant-isolation` since `4c8fb6c`, plus uncommitted
work from today (shared-files sync + locale fix, below). **423 tests passing.**
Read this block first; the sections below are the detail.

**Done and verified live:** per-env Claude credentials (C2-C8, all four grants
independent and audited), decision B steps 1-3 — an env's state tree now mounts
at `~/.claude` with `CLAUDE_CONFIG_DIR` a constant — and the shared-files sync
(`sync_claude_shared_files`) that replaced the abandoned nested-mount attempt.
`fabric` is running on all of it, with state confirmed surviving a full
container destroy/recreate and its `CLAUDE.md`/`settings.json` confirmed
byte-identical to host. `dax`, `discernment`, and `ys-augmentcode` are still on
the shared mount, which is the intended fallback.

**Immediate next actions, in order:**

1. **Finish migrating `dax`** — `tools/migrate_env_state.py dax` (dry run, then
   `--apply`), then `dax env set dax features claude_tenant_state`. The
   `.claude.json` to seed its tree with is already on the host at
   `~/fatsec/dax/doot` (34 keys, scoped to `/home/dfarrow/dax`, and it carries
   `oauthAccount`/`userID`, so it also covers the wrapper's un-built account
   seeding for this env). **Do not commit that file** — it holds account identity.
   Copy it to `~/.local/state/dax/tenants/personal/dax/.claude.json`, then delete
   it and the scratch `doit.txt` from the repo.
2. ~~Restart `fabric`~~ — **done and verified 2026-07-31.** `sync_claude_shared_files`
   correctly flagged fabric's leftover 0-byte `CLAUDE.md` as drift on first run;
   `dax env accept-shared-files fabric` resolved it, and `wc` confirmed a
   byte-perfect match between host and container.
3. **Rebuild the image and restart running containers** (`dax build`, then
   `dax run` for `fabric`/`dax`/whatever else is up) to pick up the
   `LANG`/`LC_ALL` fix found while chasing a `less` false alarm during step 2 —
   the image generated `en_US.UTF-8` but never activated it, so every shell
   defaulted to POSIX/C. Not urgent, but do it opportunistically rather than
   let it linger.
4. **Delete the credential in `hci/hydraulic-controls-it`'s tree** — a live grant,
   valid to 2026-08-24, for an engagement that ended and an env that no longer
   exists. Then decide about the 393K of transcripts beside it.
5. **`dax creds remove claude-fre`** if it is genuinely dead — registered, used by
   nothing, and holding a fourth live grant.
6. **Step 7 of the cred test sequence**: relaunch `claude` in a migrated env and
   confirm `.credentials.json`'s mtime does not change. Cheap, and now meaningful.
7. ~~Roll `discernment` over~~ — **resolved 2026-10-15, not needed.** `discernment`
   the registered env isn't operated as a main dax session anymore — it's kept
   registered purely as the mount source other substrate envs (`fabric`, etc.)
   pull `wire.sh`/`process-types.tsv` from. No live Claude session there to
   protect, so `claude_tenant_state` buys nothing; it can stay on the shared
   `claude` mount indefinitely.
8. **Step 5 of decision B — the E deletions.** Still the largest item, still pure
   deletion, and still reading as current code to anyone who looks.

## In progress

- **`dax env` lifecycle consolidation** — full design in
  `docs/design/2026-10-18-env-bootstrap-consolidation.md`, branch
  `feature/env-lifecycle-consolidation` (off `master`; steps 1–5 built,
  41 decisions; see "Build progress" below).
  Decisions 25–37 came from a 2026-10-19 pre-implementation risk review:
  env-name collisions are fatal; restore only targets an empty,
  unregistered cwd and never overwrites anything (rollback = destroy, then
  restore); an env→date archive picker that reaches older archives;
  renamed restores rewrite `.claude.json` and `history.jsonl` too;
  credentials are never archived (`backup_dir` is OneDrive); `--explain`
  stays, `--dry-run` goes; unknown archived features are restored
  commented out; Claude logins write to a temp folder, never the host
  `~/.claude` (decision 32); a renamed restore swaps explicit Claude creds
  for bare `claude`; `archive --remove` reads each tarball back first; Claude is an always-on
  baseline feature with an implied `claude-<tenant>-<env>` credential and a
  required tenant for every env (35); redundant old Claude entries are
  ignored and warned about until hand-removed (36); a missing `~/.dax.yaml`
  exits with instructions, never auto-created (37). Two independent
  reviews found and fixed ~55 issues on 2026-10-19; execution order is
  1, 2, 3, 4, 5, 7, 8, 6. Manual runbook:
  `docs/testing/2026-10-19-env-lifecycle-test-sequence.md`.
  **Build progress (uncommitted, on the branch):**
  - **Step 1 built 2026-10-19, suite 687 (from 671):**
    - `dax env destroy` replaces `dax process destroy`, with no `--dry-run`.
    - `cmd_env` now resolves the env name per subcommand, so `destroy`
      works outside an env.
    - `dax env set` is removed; the `run_env_set` helper stays until step 4.
    - "Run `dax env set`" hints now name the `~/.dax.yaml` field instead.
    - `unknown_env_keys` flags non-`ENV_FIELDS` keys in `env show`, and
      quietly on every `dax run` across all envs. Typos are reported as
      "ignored"; the retired `multi_tenant`/`tenant_subdir` keys as
      "retired, remove them", since their code still reads them until step 8
      (a `/code-review` finding).
    - Routing `--explain` before the config load is deferred to step 2,
      since no `dax env` subcommand has `--explain` yet.
    - Manual checks passed; committed as `4602df1`.
  - **Step 2 built 2026-10-19, suite 711 (from 687):**
    - `dax env archive` replaces `process archive` and `process export`.
      It's a wizard (`_run_env_archive`) over a non-interactive core
      (`_archive_envs`); the restore and import tests use the core too.
    - Credentials are never archived (`_state_tree_tar_filter` always
      excludes `.credentials.json`).
    - Removal requires every tarball to read back first (`_verify_archive`).
    - The manifest is written as `dax-env-manifest.json`. Both readers fall
      back to `dax-export-manifest.json` via `_archive_manifest_member`,
      covered by a legacy-name restore test.
    - `--explain` is routed in `cmd_env` before the missing-config exit,
      and `dax backup --explain` now points at `dax env archive`.
    - New `_q_path` prompt helper in `dax_creds/init.py`.
    - The specific-file prompt puts the archive inside a directory if
      given one (a `/code-review` finding: it used to crash) and asks
      before overwriting an existing file.
    - Manual checks passed; committed as `468792a`.
  - **Step 3 built 2026-10-20, suite 733 (from 711):**
    - `dax env restore [name|path]` (`_run_env_restore`) replaces
      `process restore` and `process import`.
    - It only restores into an empty (`.DS_Store` aside), unregistered cwd,
      names the env after the folder, and refuses on a registered name or a
      leftover state tree. `_extract_env_archive` refuses rather than
      `rmtree`s, and moves the project's contents into cwd.
    - Pickers: env then date, with cwd's name and the newest date
      pre-selected (`_available_archives` now returns every date).
    - A renamed restore re-keys `projects/`, `.claude.json` and
      `history.jsonl` (exact `/home/<user>/<name>` paths only,
      `_rename_container_path`). It swaps explicit Claude credentials for
      bare `claude`, but only when there's a tenant (otherwise it warns that
      the grant is shared). The `projects/` key match accepts the folder
      name verbatim or with non-alphanumerics mangled to `-`. Both were
      `/code-review` findings.
    - Archived credentials are stripped and reported.
    - Unknown features are written as `#- <name>` via ruamel comment tokens.
      The entry is built in memory and saved once.
    - `_check_process_dir`/`_process_dir_conflict` renamed to
      `_check_bootstrap_dir`/`_bootstrap_dir_conflict`.
    - `_q_select` gained `default=`.
    - From inside a registered env, restore reports "already registered (and
      not empty)" with the destroy hint, not just "not empty" (a manual-pass
      request).
    - Manual checks passed; committed as `de8181e`.
  - **Step 4 built 2026-10-20, suite 709 (from 733; 56 deliberate deletions,
    32 new):**
    - `_bootstrap_env` (in `dax.py`) is what `dax run` now starts in an
      unregistered folder. It asks for image, tenant (always; blank
      re-asks), then a checkbox of existing non-Claude credentials (no
      define-new loop), then coding project vs substrate-backed process
      (substrate path, title, type, git; bad answers re-ask). Then a
      `_print_table` summary and a confirm.
    - `new-process.sh` runs only after the confirm, and the entry is saved
      once. Until step 7 it still writes `creds: [..., claude]` and
      `claude_tenant_state`.
    - Fatal cases: a cwd conflict, a name registered elsewhere, or leftover
      state.
    - Declining or Ctrl-C makes `cmd_run` `sys.exit(0)`, so nothing
      launches (that was the swallowed-`KeyError` bug).
    - Deleted: `dax init` (`cmd_init`, `run_init`/`_run_init`), `dax process`
      (`cmd_process`, `_run_process_new`), `run_env_set`, `env_field_help`,
      `register_project`, `_setup_claude_credential`.
    - A missing `~/.dax.yaml` gives one shared message
      (`missing_config_message()` / `_exit_missing_config()`) from
      `load_config`, `cmd_env`, `cmd_creds`, `cmd_envs` and `cmd_backup`;
      dax never creates the file.
    - The Gate 0 test is kept.
    - `/code-review` fixes:
      - `_known_tenant_names` now also reads each entry's `tenant:` (the
        wizard writes only that); step 8 drops the legacy `.dax-tenant` half.
      - A failed or interrupted `new-process.sh` lists the files it left and
        says to remove them before retrying (never auto-deleted).
    - Manual checks passed; committed as `f70ea2c`.
  - **Step 5 built 2026-10-20, suite 716 (from 709):**
    - `_q_confirm` passes `auto_enter=False` (decision 9, reapplied after
      the earlier revert), after `_flush_stdin()`: the flush discards keys
      typed before the prompt, and auto_enter=False stops one typed at it
      from committing on its own.
    - The bootstrap wizard opens by saying the env will be named after its
      folder.
    - Archive's summary is now a `_print_table`, like restore's and
      bootstrap's.
    - Manual checks passed; committed as `82c9543`. (The double-Enter
      check was dropped from the runbook as not hand-testable; unit tests
      cover it.)
  - **Step 7 built 2026-10-20, suite 737 (from 716):**
    - Claude is a baseline feature. `feature_claude_tenant_state` became
      `feature_claude` in `_ALWAYS_ON_FEATURES`; the old shared-mount
      `feature_claude`, the mutual-exclusion check and `claudedir` are gone.
    - The implied `claude-<tenant>-<env>` credential is resolved centrally
      in `resolve_credential_names(project, name, config)`, which now
      raises when an env has no tenant. Every caller sees it: login,
      destroy, `creds list`, grant collision and `env show`.
    - `cmd_run` reports an undefined credential by name rather than failing
      later with a misleading "needs a tenant".
    - `_merge_features` drops `claude`/`claude_tenant_state` from every
      feature source and de-duplicates.
    - `legacy_claude_entries` drives the `dax run` warning and an
      `env show` note. `dax creds add` no longer edits `creds:`.
    - `sync_claude_shared_files` is now copy-once.
      `accept-shared-files` is gone; teardown still deletes the old sidecar
      manifest.
    - Claude logins use a 0700 folder under `~/.local/state/dax/login/`,
      swept after an hour and removed in a `finally`.
    - Teardown sync refuses and clears a tree grant that collides with
      another name.
    - Restore drops Claude entries from archives and asks for a tenant when
      an archive has none. The bootstrap wizard writes neither entry.
    - New test-wide fixture `_implied_claude_credential_is_logged_in`
      (opt out with `@pytest.mark.real_claude_login`).
    - The high-effort `/code-review` found ten issues, all fixed:
      - `-t` and keyring-less runs never log in;
      - the tree path uses the registry name (`config['env_name']`);
      - an empty seeded file is re-seeded (delete a tree copy to refresh it);
      - an unregistered `claude-...` name counts as Claude;
      - a tenantless env's explicit creds still count as in use;
      - the login folder is made before the daemon and cleaned on its
        failure;
      - the meaningless "key on disk" fallback is gone;
      - plus dead code and stale text.
    - Manual checks passed; committed as `22c7cf1`.
  - **Step 8 built 2026-10-20, suite 675 (from 737; 76 multi-tenant tests
    deleted):**
    - `dax env list` replaces `dax envs list` (`run_envs_list` is now
      `run_env_list`).
    - Deleted: `dax tenant`/`dax tenants` (`cmd_tenant`, `cmd_tenants`,
      `_ensure_tenants_classified`), `dax_creds/tenant.py`,
      `dax-creds resolve-tenant`, and `cmd_run`'s
      `multi_tenant`/`tenant_subdir` promotion.
    - `_known_tenant_names` reads only the registry's `tenant:` fields. Its
      tests and `_prompt_tenant`'s moved to `tests/test_dax_prompt_tenant.py`.
    - Retired keys are now reported as "ignored; remove them".
    - The tenant-isolation doc's pre-redesign half is marked historical
      (decision 24).
    - Manual checks passed; committed as `764f2b6`.
  - **Step 8b built 2026-10-20, suite 695 (from 675):**
    - `dax config` (`cmd_config`) opens `$VISUAL`/`$EDITOR`/`vi` on
      `~/.dax.yaml`, split with `shlex`, after copying it to
      `~/.dax.yaml.prev`.
    - After saving it parses via `load_dax_config` (duplicate keys
      included) and offers to reopen on an error; declining exits 1 and
      points at `.prev`.
    - It then prints the unknown-key and old-Claude-entry warnings.
    - It **refuses** inside a dax container (the user's call, 2026-10-21):
      a single-file bind mount can silently detach on a write-then-rename
      save. `_in_dax_container` checks for `/usr/local/bin/dax-entrypoint.sh`.
    - `/code-review` fixes:
      - a parsed-but-wrong-shape file (empty, list `projects:`, scalar env)
        is a problem like a syntax error;
      - `.prev` is written only when an edit changes the file;
      - a missing editor gets a message, not a traceback.
    - A missing config is decision 37's message.
    - Manual checks passed (the in-container refusal check is deferred to
      the end-of-branch rebuild); committed as `1fa3d68`.
  - **Decision 39 added 2026-10-21:** remove auggie support entirely, as
    step 8c before step 6. `auggie` in `features:` and auggie-provider creds
    are ignored and warned about until hand-removed (live: `ys-augmentcode`,
    plus the registered `auggie` credential).
  - **Step 8c built 2026-10-21, suite 694 (from 695; 11 auggie tests
    deleted, 10 new):**
    - Deleted: `feature_auggie`, `providers/auggie.py`, `wrappers/auggie`,
      `_cmd_creds_login_auggie`, `_clear_auggie_session`, and auggie's place
      in creds add/list/update and the credential checks.
    - The image's auggie npm install and wrapper are gone (takes effect on
      the end-of-branch rebuild); `auggiedir` is gone from the example.
    - Step 7's old-Claude filter is generalized: `RETIRED_FEATURES` (adds
      `auggie`), `is_retired_credential`, `retired_entries`, and
      `_warn_retired_entries`, whose message now fits both.
    - Manual checks passed 2026-10-21.
  - **Decision 40 added 2026-10-21:** a non-empty `mounts:` / a `substrate:`
    path switch their features on, with no `features:` entry. `mounts` and
    `substrate` left in `features:` are warned about like retired names.
    Built as step 8d.
  - **Decision 41 added 2026-10-21, replacing decision 40's substrate
    half:**
    - one global top-level `substrate:` path; each process opts in with
      `features: [substrate]`;
    - a per-env `substrate:` is a retired key;
    - the wizard asks once and saves the global path when it's missing.

    Live migration: add `substrate: /Users/dfarrow/fatsec/discernment` and
    remove the 8 identical per-env lines.
  - **Step 8d built 2026-10-21, suite 702 (from 694):**
    - Mounts: `cmd_run` adds `mounts` when the entry's `mounts:` is
      non-empty; `mounts` joins `RETIRED_FEATURES` for every other source.
    - Substrate:
      - `feature_substrate` reads the top-level `substrate:` and refuses
        when it's unset;
      - `substrate` is in `RETIRED_ENV_KEYS` and out of `ENV_FIELDS`;
      - the wizard uses the global path, or asks once and saves it as a
        top-level key in the same single save;
      - archives stop writing the path and restore ignores one;
      - `env show` shows the global path or NOT SET, and the two
        "feature not active" notes are gone.
    - `/code-review` fixes:
      - `cmd_run` takes the global substrate from the just-saved registry, so
        a first process's first launch works;
      - the retired-`substrate` warning, and the refusal, say to *move* the
        per-env path to the top level, not just remove it.
    - Manual checks passed; committed as `d014236`.
  - **Step 6 built 2026-10-21, suite 704 (from 702), uncommitted:**
    - Docs: README rewritten; `.dax.yaml.example` updated; superseded notes
      added to four dated docs; a "Current state" section at the top of
      this file.
    - Help text and stale comments fixed.
    - Review fixes:
      - an empty `creds:` (null) no longer crashes `env list`/`creds list`;
      - `env show` says "never used" for an empty state tree;
      - the README documents top-level `image` and `githubdir`/`awsdir`/
        `optdir`/`msfdir`;
      - the example's `.settings.json` backup typo is fixed.
  - **Decision 38 added 2026-10-20:** `dax config` (editor on
    `~/.dax.yaml`, `.prev` backup, parse and unknown-key checks after
    saving), built as step 8b, before step 6's docs sweep.
  **Read that doc before touching `dax process`/`dax init`/`dax env`/
  `dax tenant`/`dax tenants` — large parts of all five are being deleted,
  not extended.**

  Headline: six top-level commands survive (`build`, `run`, `features`,
  `backup`, `creds`, `env`), down from eleven. `dax init`, `dax process new`,
  and `dax run`'s own auto-registration fallback collapse into one
  `_bootstrap_env` wizard triggered implicitly by `dax run` — no separate
  command for creating an env at all. `dax process destroy`/`archive`/
  `restore`/`export`/`import` move to `dax env`, with `export`/`import`
  absorbed as prompts inside `archive`/`restore` rather than surviving as
  separate verbs. `dax env set`/`accept-shared-files` are dropped (confirmed
  dead against real usage); `dax envs`, `dax tenant`, `dax tenants` are
  dropped too (folded into `env list`, or confirmed dead code from the
  already-abandoned multi-tenant model). The old shared `claude` mount
  feature and its drift-tracking shared-files sync are also decommissioned,
  replaced by a one-time seed. The doc's own "Implementation plan" section
  (8 steps) is the build order; nothing here has started.

- **Tenant isolation for Claude Code state** *(largely complete: the
  consolidation deleted the multi-tenant code this item said was "slated
  for deletion" (step 8), made `claude_tenant_state` the always-on `claude`
  feature (step 7), and replaced the drift-tracking shared-files sync
  described below with a copy-once seed (decision 13). The narrative is
  kept as history.)* — design and status in
  `docs/design/2026-07-27-tenant-isolation.md`. **Read that doc's
  "Redesign 2026-07-29 — multi-tenant abandoned" section before touching
  anything tenant-related.**

  **The multi-tenant model is abandoned.** Each project is single-tenant;
  `tenant` survives only as a declared grouping label on the project's
  `~/.dax.yaml` entry, kept because it makes "end the engagement" one
  `rm -rf` of `~/.local/state/dax/tenants/<tenant>/`. The state tree now
  mounts directly at `~/.claude`, with `CLAUDE_CONFIG_DIR=$HOME/.claude`
  as a **constant** — no cwd resolution — kept solely because it is what
  relocates `.claude.json` inside the directory mount (unset, that file sits
  at `~/.claude.json`, outside the tree, and is discarded on teardown).

  **A large body of implemented, passing code is slated for deletion, not
  extension** — `_ensure_tenants_classified`, `_prompt_tenant`,
  `dax tenant classify`, `resolve_for_cwd`, `dax-creds resolve-tenant`,
  Gate B in the `claude` wrapper, `.dax-tenant` files,
  `DAX_MULTI_TENANT`/`DAX_TENANT_SUBDIR`, and ~68 tests. It still reads as
  current. Don't build on it. `find_enclosing_project`/Gate 0 stays — that
  is a separate bug, not tenant machinery.

  Trigger: discernment is being reworked so each process becomes its own
  repository (history split, not `git mv`), removing the only multi-tenant
  project. Discernment then mounts as a **sidecar** — wholesale at
  `~/discernment` (rw), with its `skills/` mounted a second time at
  `~/.claude/skills/` because Claude Code only discovers skills in the
  directories it scans; a CLAUDE.md pointer registers nothing.

  **Generalized and built, 2026-07-31 (stage 1 of 2): `feature_mounts`.**
  Rather than a discernment-specific sidecar mount, any env can now list extra
  host paths in a `mounts:` field (`dax env set <name> mounts <path>[,<path>...]`,
  comma-split the same way `creds`/`features` are), opted into via
  `features: [mounts]` — the same "list the data, then opt in" shape as the
  existing `feature_ports`. Each path mounts read-write as a **sibling under
  $HOME**, named by `dir_basename`, deliberately *not* nested inside the
  project's own workdir mount: nesting a mount inside a mount is exactly the
  class of bug that broke the CLAUDE.md/settings.json file mounts (see
  `sync_claude_shared_files` above), and siblings under $HOME sidestep it
  rather than leaning on directory-nesting (which does work) staying that way.
  `dax env show` lists configured mounts and flags them if `mounts` isn't in
  that env's active features. Deliberately does not yet cover the `skills/`
  second-mount or a common process-init script — **stage 2**, backlogged
  below, since these processes share a common initiation sequence worth
  scripting once stage 1 is in use.

  **Found live testing this against a real migrated process, same day:**
  `dax env show`/`dax env set` worked — they read `mounts` straight off the
  project dict — but `dax run` printed "no mounts defined" and mounted
  nothing. `cmd_run` promotes `project['tenant']` and `project['features']`
  into the flat `config` dict feature functions read, but `mounts` was never
  added to that promotion, so `feature_mounts` saw an empty list regardless of
  what was configured. One-line fix (`config['mounts'] = project.get('mounts')
  or []`), plus an integration test *through `cmd_run`* — the earlier
  unit tests of `feature_mounts` alone couldn't have caught this, since the
  bug was entirely in the wiring between the two. 12 new tests total, suite at
  **435**.

  `claude_tenant_state` stays opt-in for now — the 2026-07-28/29 outage forced
  a fallback to the old shared-mount model, and that escape hatch is worth
  keeping. Still outstanding: the shared `plugins`/`commands`/`cache`
  migration (now a nested mount inside the `~/.claude` tree mount), the
  multi-day refresh-token rotation soak, executing the deletions above, and a
  re-test of the real fabric/discernment containers — which needs an image
  rebuild (`dax_creds` is a frozen, non-editable install) plus a fresh manual
  pass.

  **Discernment (renaming to "virgil") authored its own dax contract,
  2026-08-01: `docs/design/VALIDATION.md`** — dropped in-repo, untracked
  (`docs/design/scripts/` is gitignored and holds `new-process.sh`, `wire.sh`,
  `process-types.tsv`; never check those in). The user manually validated the
  substrate's own parts (mounts, wiring, the settings fragment, the
  PreToolUse guard, the feedback path) against a real process directory
  before any of this was built — teardown (`destroy-process`) is deferred to
  a later phase. **Parts 1–5 of the contract are now built and unit-tested**
  (469 tests, up from 435), superseding stage 2 of the `feature_mounts` note
  above and the matching backlog item:

  - **`feature_substrate`** (`dax.py`) — a new single-path project field
    `substrate` (distinct from the generic `mounts:` list: dax needs to know
    specifically which mount holds `shared/wire.sh`), mounted rw as a sibling
    under `$HOME` the same way `feature_mounts` does, plus
    `-e SUBSTRATE_ROOT=<container-path>` — the value both the substrate's own
    hooks and the `claude` wrapper's gate read. `cmd_run` promotes
    `project['substrate']` into `config['substrate']` right alongside the
    `mounts` promotion, learning from the mounts wiring bug rather than
    repeating it. `dax env show` displays it with the same "feature not
    active" warning `mounts` gets.
  - **`dax_creds/wrappers/entrypoint.sh`** (new) — runs `wire.sh --quiet`
    once per **container boot** when `SUBSTRATE_ROOT` is set, wired in via a
    new `Dockerfile.tmpl` `ENTRYPOINT` (the image had none before). This is
    load-bearing, not stylistic: VALIDATION.md's Part 4 failure-mode tests
    restart a *Claude session* without restarting the *container* specifically
    to prove the PreToolUse guard fires on its own — if wire.sh's self-heal
    ran on every `claude` launch instead, a bare session restart would
    silently repair the very breakage the test exists to catch. Never exits
    on failure, so the container shell still starts even when substrate
    wiring is broken (repair has to be possible from inside it). A complete
    no-op for every env not opted into `substrate` — needs an image rebuild
    (`dax build`) to take effect, same as the wrapper change below.
  - **`dax_creds/wrappers/claude`** gate + settings delivery — right before
    the existing final exec, if `SUBSTRATE_ROOT` is set, runs
    `wire.sh --check` **only** (never a bare `wire.sh` — see above) and
    branches: `0` → prepend `--settings .../substrate-settings.json` (unless
    the caller already passed one) and proceed; `127` (the script itself is
    gone — the mount is down) and `1` (mounted but wired wrong) each refuse
    with a distinct message, matching VALIDATION.md's exit-code table.
  - **`dax process new <name>`** (`dax.py`, alongside `_prompt_tenant`/
    `_known_tenant_names` — reused directly, which is why this lives in
    `dax.py` rather than a new `dax_creds` module and avoid a circular
    import) — every field (`--dir`/`--tenant`/`--substrate`/`--title`/
    `--type`/`--git`|`--no-git`) is either given on the command line or
    interactively prompted, wizard-style like `dax init`; dax never lets
    `new-process.sh`'s own basic prompting fire, since everything is
    fully resolved before invoking it. `--type` is validated (or, if
    omitted, offered as a picklist) against the substrate's own
    `process-types.tsv` — never hardcoded. Runs `new-process.sh`
    **host-side**, before any container exists, a deliberate flagged
    deviation from the script's own "runs inside the container" comment:
    mechanically safe, since the script never touches `$HOME` or any
    container-only state (confirmed by reading it), and dax has no
    docker-exec-into-running-container mechanism worth building for one
    call. Always registers `creds: [claude]` and, unconditionally,
    `features: [substrate, claude_tenant_state]` — every substrate-backed
    process needs both, no conditionality.

  **Not yet done: the manual verification pass** (image rebuild + a real
  `dax process new` / `dax run` / `claude` session, per the plan's
  verification section) — this session had no Docker access to run it.
  Confirmed instead via a scripted dry run against the real dropped scripts
  (fake substrate + fake `~/.dax.yaml`, `dax process new` end-to-end, then
  `dax -t run` to confirm the assembled `docker run` command carries both
  `--volume=.../virgil:/home/dfarrow/virgil` + `SUBSTRATE_ROOT` and the
  `claude_tenant_state` mount) — real, but short of an actual container.

  **`dax process new` hardened, same day: absolute-path guardrails, a
  full-path confirmation summary, and `--dry-run`.** Found live-testing the
  first cut: `--dir`/`--substrate` were never resolved to absolute paths (a
  relative or unresolved value stored verbatim in `~/.dax.yaml`'s `dir:`
  would silently break every cwd-based env lookup later), there was no check
  against nesting inside or colliding with an already-registered project
  (the same mistake `find_enclosing_project`/Gate 0 catches for `dax run`,
  just not here), and no check that the directory was even under `$HOME`
  (where `dax run` would refuse it anyway, just much later). `_run_process_new`
  now `.resolve()`s both paths immediately, and a new `_check_process_dir`
  refuses all three before anything is created. Every run also prints a
  full-absolute-paths summary — directory, substrate, tenant, title,
  type/skill, git decision, image, creds, features, and the exact Claude
  state-tree path — before doing anything, gated on typing `Y` (or
  `--dry-run`, which stops right after the summary). 15 new tests, suite at
  **478**.

  **`dax process destroy` built, same day — the deferred item 6 of the
  contract, generalized.** Tears down a registered process's directory,
  Claude state tree (plus its sibling `sync_claude_shared_files` manifest,
  which lives *beside* the tree and would otherwise survive as orphaned
  cruft), any per-env **derived** credential (never an explicitly-named/
  shared one, just because one env's `creds:` listed it), and the
  `~/.dax.yaml` entry itself. Deliberately not restricted to
  substrate-tagged envs — the same cleanup applies to any tenant-isolated
  project, and narrowing it would only get in the way of burning down other
  test cruft. `dax process destroy [name ...] [--dry-run]`: named envs, or an
  interactive `_q_checkbox` picker (same widget `dax init`'s credential
  selection already uses) over every registered env when none are given,
  nothing pre-checked. Every candidate is checked *before* any deletion
  begins — refuses if its container is currently running
  (`docker stop <name>` first), warns (doesn't block) on uncommitted git
  changes. The confirmation is a typed literal `DESTROY`, not a Y/n — a
  deliberately higher bar than creation's, since destroying isn't
  recoverable the way creating is. Filesystem deletion happens before the
  `~/.dax.yaml` edit, so a mid-batch crash leaves a stale-but-visible
  registry entry (`dax envs list`'s existing `!` "directory missing" marker)
  rather than silent loss. 13 new tests, suite at **491**. Verified live with
  a scripted fixture (fake project + fake state tree, no real Keychain): dry
  run touched nothing, then a real run with `DESTROY` removed the directory
  and state tree and cleared the registry entry, degrading gracefully (a
  warning, not a crash) on the missing-keyring case this sandbox always
  hits.

- **Manual credential test sequence** —
  `docs/testing/2026-07-30-cred-management-test-sequence.md`. Follow it after
  any image rebuild or credential change. It exists because the three places the
  real bugs lived are unreachable from the suite: the macOS Keychain, the Chrome
  profile a login opens, and whether two credentials are the same OAuth grant.
  Nearly every step is a **host** command — keyring is unreachable in a
  container, so `dax creds list` there always reports `stored: no`, and
  `~/.dax.yaml`'s host paths mean `dax env show` can't match cwd to an env.

  **Container rebuild verified 2026-07-30** (step 0): 342 tests pass, installed
  `dax_creds` byte-identical to the repo, `/usr/bin/claude` identical to
  `dax_creds/wrappers/claude` (inject-if-absent shipped), `ruamel.yaml`
  importable, and `DAX_CREDS_CLAUDE=claude-personal-dax` in a live container —
  so C2's derived naming resolves end to end, from `~/.dax.yaml` through
  `dax run` into the container env. **Host step 1 passed 2026-07-30**: editable
  reinstall picked up ruamel 0.19.1, and `dax env set` rewrote `~/.dax.yaml`
  byte-identically (empty diff). That is a real round-trip proof, not a
  short-circuit — `run_env_set` saves unconditionally (`init.py:649`), so a
  same-value set still serializes the whole file. **Step 2 passed 2026-07-30**
  with findings — full results in the design doc's "Confirmed 2026-07-30"
  entry. Headline: all three derived Claude credentials are already in Keychain,
  minted *before* the C3 fix. **Step 3 passed** — 4 distinct grants, no sharing,
  refresh expiries staggered across the days the logins happened. Note the
  check's limit, now in its docstring: the refresh token only *proxies* grant
  identity, so a copy is detectable until either side refreshes — run step 3
  right after minting, where the signal is strongest.
  Also surfaced 8 orphaned state trees, all discernment processes left by the
  abandoned multi-tenant classification.

  **Step 4 found a blocking bug (C5 in the design doc): `dax creds login`
  refuses derived names.** `cmd_creds_login` (`dax.py:698`) resolves against
  `config['credentials']` only; `derived_credentials()` was wired into `creds
  list`, `creds remove`, and `env show` but not `login`. Since `add`'s claude
  path is the unguarded one C3 warns against, **no sanctioned way to mint a
  per-env Claude credential currently exists**, and C3's guard is unreachable
  for the case it was written for. Steps 4–5 blocked until fixed. Related:
  `_post_login_import` never writes a `credentials:` entry, so a derived name
  stays derived permanently and `env show`'s `[not registered yet]` is wrong
  rather than transient.

  **C5 fixed and confirmed live 2026-07-30.** `_login_credential_def()` resolves
  registered-then-derived; 5 tests added, suite at **347**. A real
  `dax creds login claude-personal-fabric` opened Chrome Profile 2, used the
  device-flow redirect, and minted a new grant — hash changed, which is direct
  proof of a fresh grant rather than a copy. Step 3 still PASS, 4 distinct
  grants. **C3's refusal path confirmed live** on a second, cancelled run — the
  "predates this login" refusal printed and Keychain was untouched.

  **C6 built 2026-07-30 — one grant per name, enforced at import time.** C3's
  guard compares the on-disk *file*, not the grant, so a Claude Code token
  refresh landing inside the login window made an abandoned login look
  successful. Rather than tightening that proxy, the invariant is now enforced
  directly: `grant_id()` hashes the refresh token, and
  `ClaudeProvider.grant_collision()` refuses to store a token whose grant already
  sits under another name (skipping the credential's own name, since re-importing
  its own token is a no-op). `credential_names_for_provider()` supplies
  registered **and derived** names — enumerating only the registry would let the
  check pass by seeing nothing. Wired into both doors, which **closes the
  outstanding `dax creds add` gap**: that path never had the before/after guard
  at all. Claude-only by design; github/gmail/ssh are user-level, where one name
  per credential is the intent. Inert in a container (no Keychain to compare
  against), so `check_claude_grants.py` remains the audit for sharing that
  already exists. 20 tests, suite at **367**.

  **C8 built 2026-07-30 — `dax creds add` asks the provider first and never asks
  for a per-env name.** Naming is dax's job (C2), so once the provider is one of
  `BARE_PROVIDER_CREDS` the name is derived, not typed — which also removes the
  way a derived credential got silently converted to an explicitly registered one
  (no "Overwrite it?" fires, since there is no registry entry to collide with, and
  it then stops inheriting `credential_defaults`). Flow: provider → env (cwd's env
  offered first) → tenant *only if that env has none* → derived name printed →
  offer to add the bare token to the env's `creds:` → write
  `credential_defaults` only if missing → **hand off to `dax creds login`, never
  importing a token from disk**. `_run_creds_add` returns
  `(config, pending_login)` and `cmd_creds` runs the login when accepted. Every
  `_setup_*` now returns whether a secret was stored, so `Credential "X" saved.`
  stops appearing when nothing was. **`dax init`'s inline creds loop still has
  the old shape** — safe (C6's guard holds) but still hand-named; fold it in with
  the backlogged `dax init` tenant prompt. 18 tests, suite at **385**.

  **Superseded: C7 — `dax creds add` rebuilds the definition and drops unprompted
  fields.** `_define_credential` starts from `{'provider': ...}` (`init.py:113`)
  and `_run_creds_add` assigns it wholesale (`init.py:369`), so re-running `add`
  on an existing credential silently loses anything its prompts don't ask for;
  `dax creds update` is the non-lossy path. Because of this, manual step 6 was
  downgraded to optional — its three `replace=` behaviors are already unit-tested
  (`test_dax_creds_init.py:281–320`), and `claude-fre` must never be used as the
  target since claude's replace path would import the shared credential and
  manufacture a shared grant.

  **Any Claude login rewrites the shared credential.**
  `_LOGIN_PROVIDERS['claude']` mounts the host's `~/.claude`
  (`dax.py:644`), so while `feature_claude`'s wholesale mount is in use, logging
  in for one env repoints *every* container — inject-if-absent skips a non-empty
  file, so each authenticates on whichever env logged in last regardless of its
  own `DAX_CREDS_CLAUDE`. Per-env credentials stay inert at runtime until
  decision B lands.

  Also corrected: `refreshTokenExpiresAt` does **not** reliably encode mint date
  (a grant minted 07-30 reported 08-27, earlier than older grants), so staggered
  expiries cannot corroborate separate mints. A grant hash changing across a
  known-good login is the evidence that does.

  **Superseded 2026-07-31: decision B steps 1-3 are built and live-verified on
  `fabric`.** The feature now mounts the env's state tree **at** `~/.claude` with
  `CLAUDE_CONFIG_DIR` a constant, Gate B is gone from the wrapper, and
  `claude`/`claude_tenant_state` are mutually exclusive (`dax run` refuses). No
  image rebuild was needed: `dax run` deliberately stops setting
  `DAX_TENANT_STATE`, so a wrapper still carrying Gate B leaves it dormant.
  Verified live — mount shape, credential bootstrap from Keychain, and **state
  surviving a full container destroy/recreate**, which closes the regression where
  `.claude.json` was discarded on every teardown. Step 5 (the E deletions) is
  still outstanding, so the old tenant machinery remains present but unreachable
  from the wrapper.

  **B2's shared list was incomplete — fixed 2026-07-31.** A tree mount replaces
  `~/.claude` wholesale, so the *user-level* `CLAUDE.md`, `settings.json`, and
  `settings.local.json` vanished with it. `fabric` ran a full day without your
  global instructions, silently. `_CLAUDE_HOST_SHARED_FILES` now mounts all three
  **read-only** — writable single-file bind mounts are exactly where
  write-temp-plus-rename breaks on grpcfuse, and a silent write failure is worse
  than `/config` visibly not persisting inside a container.

  **Correction, same day: the `ro` mount doesn't fail loudly.** The comment above
  assumed a write attempt would error and make the loss visible. Tested from
  inside a `claude_tenant_state` container: a plain `touch` + append against the
  mounted `CLAUDE.md` returned no error at all. The write landed in a
  container-local copy that silently detached from the host-mounted file — host
  untouched, but the container's own view of the file is now wrong with nothing
  to signal it. Quieter than assumed, not louder. Practical upshot for anyone
  (Claude included) working inside one of these containers: never edit
  `CLAUDE.md`/`settings.json`/`settings.local.json` in place, even to test. Draft
  the intended change in conversation and have the user apply it on the host —
  a successful-looking in-container write is not evidence the host file changed.

  **Superseded, same day: the read side was broken too — mounting abandoned in
  favor of a copy.** Confirmed live: mounting these three files read-only,
  nested inside the tree mount, doesn't deliver the host's content at all — the
  container sees an empty file, not the real `CLAUDE.md`. Unlike
  `_CLAUDE_HOST_SHARED_DIRS` (`commands`/`plugins`), which nests fine, a
  single-*file* bind mount nested inside another mount didn't take.

  **`--mount` is not a fix — tested and ruled out, 2026-07-31.** A stripped-down
  `docker run --mount type=bind,source=~/.claude/CLAUDE.md,target=...` (no
  `dax`, no `--volume`, run straight from a Mac terminal to rule out every other
  variable) reproduced the identical empty-file result: 54 lines on the host,
  0 bytes in the container. `-v`/`--mount` are two CLI spellings of the same OCI
  bind-mount spec, so this was never a `--volume`-syntax quirk — nesting a
  single-file bind mount inside a directory that's also a bind mount doesn't
  work under either flag. Don't revisit `--mount` for this.

  **Built, same day: `sync_claude_shared_files` (`dax_creds/config.py`) copies
  these three files into the tree at `dax run` time, with drift detection.** A
  plain host-vs-tree diff can't tell "host moved on since last sync" (safe to
  overwrite) apart from "something changed the tree's copy independently" (a
  container editing it directly, now that it's a plain file, not a mount —
  the exact risk of dropping the mount). A small manifest beside the tree
  (`<tenant>/<project>.shared-files.json`, deliberately *not* inside the tree
  itself, so it never shows up in the container's `~/.claude`) records the
  hash of what was last synced in per file:

  - tree copy missing → copy host, record hash (first-run seed)
  - tree hash matches host → no-op
  - tree hash matches the manifest but not host → host moved on → copy, update
  - tree hash matches neither → drift → **warn and skip**, never silently
    overwrite (`dax run` prints the file/env and the command to resolve it)

  **Confirmed live against `fabric`, 2026-07-31.** `dax run` correctly flagged
  drift on the first run — fabric's tree already held a 0-byte `CLAUDE.md`, a
  leftover of the `--mount` repro above having targeted the same tree path —
  and `dax env accept-shared-files fabric` resolved it.
  `wc ~/.claude/CLAUDE.md` matched exactly (54 lines / 442 words / 2921 bytes)
  on the host and inside the container. (A `hexdump`-without-`-C` byte-swap and
  a locale-driven `less` "binary file" warning both looked alarming and were
  both false alarms — the content is plain UTF-8 text.)

  **Found chasing that `less` warning: the image never activates its own
  locale.** `Dockerfile.tmpl` runs `locale-gen` for `en_US.UTF-8` but never
  sets `LANG`/`LC_ALL`, so every shell in the container defaults to POSIX/C —
  confirmed via `locale` inside `fabric`. `less` treats a `CLAUDE.md` full of
  ordinary em-dashes as binary under that default. Fixed by adding
  `ENV LANG=en_US.UTF-8` / `ENV LANGUAGE=en_US:en` / `ENV LC_ALL=en_US.UTF-8`
  right after the `locale-gen` step. One new test
  (`test_render_dockerfile_activates_the_generated_locale`), suite at **423**.
  **Needs an image rebuild** (`dax build`) — unlike the sync fix above, this is
  baked in at build time, not computed per `dax run`.

  `dax env accept-shared-files <name>` force-adopts the host version and resets
  the manifest — resolving drift is always an explicit act, never automatic.
  `_CLAUDE_HOST_SHARED_FILES` in `dax.py` is now just
  `dax_creds.config.CLAUDE_SHARED_FILES` re-exported for the existing tests.
  15 new tests (`test_dax_creds_shared_files_sync.py`, the rewritten
  "user-level files" section of `test_dax_claude_tenant_state_mount.py`, and
  `env accept-shared-files` cases in `test_dax_env_cmds.py`); suite at **422**.
  Manual pass, since none of this is reachable from unit tests (real
  `~/.claude`, real state trees, a real container):
  `docs/testing/2026-07-31-shared-files-sync-test.md`. No image rebuild
  needed — the sync runs host-side while `dax run` assembles the docker
  command.

  **Migration: `tools/migrate_env_state.py <env>`** (dry run by default) does the
  transcript copy, the history filter, and the credential/`.claude.json` checks.
  It deliberately does **not** write `~/.dax.yaml` — switching the env over stays a
  manual `dax env set` — and does not handle `.claude.json`, which has to be copied
  out of the env's running container before shutdown since it dies with it. Runbook
  with the reasoning: `docs/testing/2026-07-31-migrate-env-to-state-tree.md` —
  how to move an env's accumulated state into its tree rather than starting empty.
  The separability was checked, not assumed: `projects/<mangled-cwd>/` is keyed by
  container cwd so it is already per-env (transcripts + auto-memory);
  `history.jsonl` carries a `project` field per line so it filters cleanly (92 of
  2185 lines were dax); and a container's `~/.claude.json` already has just the one
  project key, so it seeds the tree wholesale and preserves trust/`allowedTools`/
  tips. `file-history/` is keyed by session UUID and mappable but low value.

  **Watch for orphan grants in state trees.** `fabric`'s tree held a
  `.credentials.json` from 2026-07-28 whose grant matched no Keychain entry.
  Inject-if-absent correctly left it alone (non-empty file), so the env would have
  refreshed and used an untracked grant indefinitely.
  `tests/manual/check_claude_grants.py` cannot see it — it reads Keychain only,
  never the trees, which is where credentials now live. Extending it to scan
  `~/.local/state/dax/tenants/*/*/.credentials.json` and flag grants that match no
  Keychain entry (or duplicate another tree's) is **not yet built**.

  **Found live, 2026-10: a well-formed Keychain credential can still be a
  dead link in the refresh-token rotation chain, with no local way to tell.**
  `atticus`'s state-tree session died before any teardown ever synced a
  fresher token back to Keychain. The next `dax run` launched clean —
  `is_oauth_envelope` only checks shape, not whether Anthropic still accepts
  the refresh token — and the container came up already logged out,
  discovered only once Claude Code's own `/status` said so. Checked and
  ruled out as a fix: `expiresAt` is the short-lived access token and says
  nothing about the refresh token's health; `refreshTokenExpiresAt` was
  already found (2026-07-30, above) not to correlate with actual mint date,
  so it can't be trusted as a staleness signal either. There is no local
  field worth building a heuristic on.

  **Fix: `dax run` now reruns the login flow unconditionally before every
  launch of a `claude_tenant_state` env**, rather than trusting whatever's
  already in Keychain — gated by a cooldown (`relogin_due`,
  `dax_creds/config.py`) so restarting the same env several times in a day
  (e.g. picking up a `dax build`) doesn't pop a browser window on every
  single restart. `record_login(cred_name)` writes a small sidecar timestamp
  at `~/.local/state/dax/creds-login/<name>.json` — a sibling of the state
  trees, never inside one, matching the shared-files-manifest convention
  above — every time `cmd_creds_login` actually runs the flow, whether
  triggered by this proactive check, the existing "not in Keychain, log in
  now?" prompt, or a manual `dax creds login`. Default cooldown 24h,
  overridable via `defaults.relogin_cooldown_hours` in `~/.dax.yaml`.
  Deliberate design call, made explicitly with the user: given his actual
  usage pattern (sessions run for weeks, restarts are rare, and every
  restart this past week needed a fresh login anyway), the marginal cost of
  "always relogin" is near zero and a local freshness heuristic would have
  been false confidence regardless. The one real risk considered — Anthropic
  capping concurrent OAuth grants per account, and a proactive relogin
  policy repeated across many projects pushing other, unrelated, healthy
  projects' credentials over that cap — was judged low for this usage
  pattern specifically (low absolute launch frequency keeps total mint
  volume low) but remains **untested**; worth an empirical check before this
  policy is ever made more aggressive (e.g. dropping the cooldown). 16 new
  tests (`test_dax_creds_login_state.py`, `test_dax_cmd_run_relogin.py`),
  suite at **651**.

- **Claude credentials are per tenant/project** — named
  `claude-<tenant>-<project>`, each minted by its **own fresh login**. That is
  load-bearing: independent OAuth grants have independent refresh-token chains,
  so rotation in one project cannot invalidate another's credential even when
  every entry authenticates the same account. Minting a credential by *copying*
  an existing blob (which is what `ClaudeProvider.import_from_disk()` does, via
  `_setup_claude_credential` and `_post_login_import`) shares one grant between
  two entries and breaks that property.

  **Done 2026-07-29:** the `claude` wrapper now injects from Keychain only when
  the state tree has no credential (`-s`, so a zero-byte file counts as absent),
  and skips the daemon fetch entirely when one exists. Previously it overwrote
  `.credentials.json` on every launch — harmless under a shared `~/.claude`,
  but under a persistent per-project tree it would clobber a refreshed token
  with the stale Keychain snapshot on the next start. Keychain is now a
  bootstrap; Claude Code owns rotation from first launch.

  **Not built:** teardown write-back to Keychain. Deliberately deferred — see
  decision C of the design doc. The gap is softened by inject-if-absent: a
  missed write-back leaves the tree's working credential in place rather than
  forcing a stale one over it.
- **Fixed: `dax run` from inside a registered project's subdirectory silently
  mis-scoped the container.** `cmd_run`'s project lookup (`find_project_by_dir`)
  only matches by exact `dir:` equality; a `cd` into `discernment/processes/<name>/`
  on the host followed by `dax run` there fell through to `run_init`,
  auto-registering the subdirectory as a brand-new project and mounting only
  it — repo-root content (`.git`, `.claude/skills/`) silently absent, no
  error, and (worse) the pre-existing `.dax-tenant` there made it resolve to
  the same tenant/state dir the correct flow would have, masking the bug
  entirely. Fixed via `find_enclosing_project` (`dax_creds/config.py`):
  `cmd_run` now refuses with the enclosing project's name/root instead of
  auto-registering, whenever cwd is nested inside — not equal to — an
  already-registered project. See `docs/design/2026-07-27-tenant-isolation.md`
  decision 6's "Gate 0" note. 259 tests pass.
- **~~Shared plugins/skills/commands/cache migration~~ — deleted, not built
  (decision B2, 2026-07-30).** No `shared/` tree and no seeding copy. Instead
  mount the host's own `~/.claude/commands` and `~/.claude/plugins` **rw**,
  nested inside the tree mount at `~/.claude` (Docker orders mounts by
  destination depth, so the nesting resolves).

  Checked against the real directories rather than assumed: `commands` holds 11
  live commands and is the only genuine user state; `plugins` has **nothing
  installed** — just the official marketplace catalog that Claude Code
  auto-installs and refreshes itself, so mounting it is an optimization against
  four redundant 6.3M clones, not state preservation. `skills` drops out
  (discernment content, arriving via decision D's sidecar mount) and `cache`
  drops out (derived, generic, cheap to refetch, and the only one of the four
  where fetched content can turn account-specific).

  With the list down to two, the copy step isn't worth its cost: direct mounts
  need no seeding code, keep one source of truth, remove the divergence class
  where a host-authored command is invisible in containers, and are *strictly
  less* container write access than today's wholesale rw `~/.claude`.

  **`feature_mounts` gained an explicit container-name syntax, 2026-08-01:**
  `host:container_name`, defaulting to `dir_basename(host)` as before when no
  colon is present — a fully backward-compatible extension, not a format
  change. Immediate driver: `tools/migrate_env_state.py` needs rethinking
  (process state can be strewn across more than one `~/.claude/projects/<key>`
  directory — cwd variations, renames — not just the single top-level key it
  currently assumes), and designing that properly needs the real host
  `~/.claude/projects/` visible from inside a container to look at. Mounting
  the host's actual `~/.claude` a second time under its own basename would
  collide with whatever `claude_tenant_state` already owns at `~/.claude`
  itself — the explicit name (e.g. `~/.claude:host-claude`) is what avoids
  that. 2 new tests, suite at **493**. **Not yet applied**: this needs
  `dax env set dax mounts '~/.claude:host-claude'` (plus `mounts` added to
  `dax`'s features) and a container restart to take effect — and since this
  session runs inside that very container, restarting it will interrupt the
  session, so it's a deliberate step for the user to take when ready, not
  something to script automatically.

## Backlog

- **~~`dax creds login` leaves the newest Claude grant in plaintext at the
  host's `~/.claude/.credentials.json`~~ — fixed in the consolidation's step
  7 (decision 32).** Found 2026-10-19 during the
  env-lifecycle review. The login container mounts the real host
  `~/.claude` rw, `claude auth login` writes the token there, and
  `_post_login_import` copies it into Keychain but never deletes the file.
  Auggie's branch does clean up (`_clear_auggie_session`); Claude's
  doesn't. So the file always holds a live copy of whichever env logged in
  last. Possible fix: point the login container at a fresh temp directory
  instead of the real `~/.claude`, and pass it to `import_from_disk(...,
  credentials_file=...)`, which already exists, so nothing ever lands in
  the host `~/.claude`. **Decided and folded into the consolidation
  branch, 2026-10-19:** it's decision 32 there, built in step 7 (a temp
  folder under `~/.local/state/dax/login/`). The leftover token already
  sitting in the host's `~/.claude/.credentials.json` is the user's to
  delete by hand.

- **Per-tenant browser profiles for Claude logins.** Raised 2026-10-19
  (consolidation decision 35): the user wants Claude logins steered to
  different Chrome profiles by tenant (e.g. `ysecurity` → its own profile).
  Today `credential_defaults.claude` applies to every env regardless of
  tenant. Likely shape: per-tenant overrides of `credential_defaults`,
  resolved when the derived `claude-<tenant>-<env>` credential is
  synthesized. Not built.

- **A pass that scans existing backups for credentials.** Raised
  2026-10-19 with decision 29 of the env-lifecycle consolidation, which
  stops archiving `.credentials.json` from now on. Older archives in
  `backup_dir` (OneDrive) made with `--include-credential` could still hold
  one. The user thinks none do and is fine leaving them for now, but wants a
  definitive check eventually: walk `backup_dir/*/*.tar.gz` and report any
  archive with a `.credentials.json` member, read-only, never modifying
  archives.

- **Global `CLAUDE.md` edits don't reach already-seeded state trees.**
  Deferred 2026-10-19: the consolidation's decision 13 (copy shared files
  once) leaves this gap on purpose. Edits are rare, and substrate envs'
  `CLAUDE.md` is wired by `wire.sh`, which rules out the simple sync
  options. See decision 13's "Known gap" note in
  `docs/design/2026-10-18-env-bootstrap-consolidation.md`.

- **~~Collapse `dax process` into `dax env`~~ — done by the env-lifecycle
  consolidation (all of `dax process` is gone).** "process" isn't a distinct
  concept, it's an env with `substrate` in its `features`. Raised
  2026-08-31, during the discernment-processes migration (deregistering the
  discernment monorepo env to register its sub-processes individually —
  which is what surfaced the question in the first place: why do processes
  and envs need to be different things at all). There is no `is_process`
  flag anywhere in the data model; it's 100% derived from `substrate` being
  in `features`. Checked against the actual implementations, not assumed:
  `destroy`/`archive`/`restore`/`export`/`import` (all currently parented
  under `dax process`) already don't branch on substrate anywhere —
  `_run_process_destroy`'s own docstring says as much ("generalized past
  substrate processes specifically... restricting this to substrate-tagged
  ones would only get in the way of burning down other test cruft"). The
  only command that's genuinely process-specific is `new` — it runs
  `new-process.sh`, validates `--type` against the substrate's own
  `process-types.tsv`, and wizards through prompts nothing else does.
  Current namespacing is actively misleading, not just inelegant: parking
  generic-env commands under `process` implies they're substrate-only, when
  they work on any registered env (`dax`, `fabric`, anything) — a real
  discoverability gap, found live when the user needed `archive`/`restore`
  for ordinary non-substrate envs this same session and they were sitting
  under a name that implied they wouldn't apply. Proposed fix, not yet
  built: move `destroy`/`archive`/`restore`/`export`/`import` under
  `dax env` (matching what they actually do), leave `dax process new` as
  the one legitimately distinct command. Deferred — a real CLI restructure
  (renamed subcommands, help text, doc/test churn), not a quick fix.

- **~~Discernment process migration, stage 2: a helper script for the common
  process-init sequence~~ — superseded 2026-08-01 by `dax process new` and
  the rest of the virgil substrate contract** (see "In progress" above).
  `dax process new` is that helper script: it wizards through `--dir`/
  `--tenant`/`--substrate`/`--title`/`--type`/`--git`, scaffolds via
  `new-process.sh`, and registers the env with `features: [substrate,
  claude_tenant_state]` unconditionally, in one command. Mounting the
  substrate's `skills/` a second time at `~/.claude/skills/` is handled
  differently than originally scoped here (decision D) — `wire.sh` itself
  symlinks each skill directory into the state tree at container boot, not a
  second dax-level mount.

- **~~Per-env features are additive only — no way to opt *out*~~ — resolved
  2026-08-04** by removing the global `features:` list entirely (see "Baseline
  features" above), rather than the supersede-`claude` fit-and-polish fix
  originally sketched here. The problem was the global list forcing something
  on every env with no way to decline it; with that list gone (workdir/
  dotfiles/webpreview baked in as a baseline instead), every remaining feature
  is purely per-env opt-in, so there is nothing left to opt *out* of.

- **~~`dax init` should prompt for `tenant`~~ / ~~`_run_init` matches projects
  by exact `dir == cwd`~~ — both resolved 2026-08-04, same pass.** `_run_init`
  now prompts for an optional tenant (blank leaves it unset, same as before)
  and writes it via `run_env_set` after registering. It also calls
  `find_enclosing_project` before falling through to registration, refusing
  with the enclosing project's name/root instead of silently registering a
  duplicate — the exact blind spot `find_enclosing_project`/Gate 0 already
  closed for `cmd_run`. 4 new tests (`tests/test_dax_run_init.py`), following
  `test_dax_creds_add_flow.py`'s scripted-`Answers` pattern rather than
  mocking questionary directly.

- **~~Retire `dax tenants` and `dax tenant classify`~~ — done in the
  consolidation's step 8** (now `dax env list`).

- **~~Container shell lands in `$HOME`, not the project mount~~ — resolved
  2026-08-04.** `cmd_run` now passes `-w <container_home>/<workdir_name>` —
  exactly where `feature_workdir` mounts the project, computed the same way,
  safe unconditionally since `workdir` is one of the always-on baseline
  features now (see "Baseline features" above) and so is always mounted
  there. `-w` added to `_DOCKER_TWO_TOKEN_FLAGS` too, so dry-run output pairs
  it with its path instead of splitting it across lines.

  (The `claude` wrapper's missing `oauthAccount`/`userID` seeding, previously
  listed here too, will never be fixed — decided 2026-08-04. Cosmetic only,
  and correctly seeding it would need new plumbing to source real account
  data from the host that doesn't exist yet; not worth building.)

## Notes

- **Tests must never write the real `$HOME`.** A dax container bind-mounts the
  host's `~/.dax.yaml` **rw**, so a test that writes `$HOME/.dax.yaml` destroys
  the live config — env definitions, credential metadata, and gmail
  `client_id`/`client_secret` values that exist nowhere else. This happened on
  2026-07-31: four tests called `run_env_set` (which calls `save_config`
  unconditionally) with no `HOME` isolation, and pytest overwrote the real config
  with fixture data. `tests/conftest.py` now redirects `HOME` for every test via
  an autouse fixture; per-module `home` fixtures still override it. Don't remove
  it, and pass an explicit `HOME=<tmpdir>` to ad-hoc verification scripts too.

- `safe.directory` is already set in `~/.gitconfig` — no need for `-c safe.directory=` flag in git commands.
- Files NOT to back up: credentials, `~/.claude/projects/`, `history.jsonl`, caches, sessions, auggie ephemeral data.
