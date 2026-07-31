# dax — Project Context

dax is a Docker-based development environment manager. It reads `~/.dax.yaml`
(and an optional `.dax.yaml` in the cwd) to build a `docker run` command with
features (volume mounts, port mappings, etc.) assembled from named feature
functions in `dax.py`.

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

## In progress

- **Tenant isolation for Claude Code state** — design and status in
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

  `claude_tenant_state` stays opt-in for now — the 2026-07-28/29 outage forced
  a fallback to the old shared-mount model, and that escape hatch is worth
  keeping. Still outstanding: the shared `plugins`/`commands`/`cache`
  migration (now a nested mount inside the `~/.claude` tree mount), the
  multi-day refresh-token rotation soak, executing the deletions above, and a
  re-test of the real fabric/discernment containers — which needs an image
  rebuild (`dax_creds` is a frozen, non-editable install) plus a fresh manual
  pass.

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

  **Watch for orphan grants in state trees.** `fabric`'s tree held a
  `.credentials.json` from 2026-07-28 whose grant matched no Keychain entry.
  Inject-if-absent correctly left it alone (non-empty file), so the env would have
  refreshed and used an untracked grant indefinitely.
  `tests/manual/check_claude_grants.py` cannot see it — it reads Keychain only,
  never the trees, which is where credentials now live. Extending it to scan
  `~/.local/state/dax/tenants/*/*/.credentials.json` and flag grants that match no
  Keychain entry (or duplicate another tree's) is **not yet built**.

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

## Backlog

- **Per-env features are additive only — no way to opt *out*.** Features
  accumulate from four sources (global `features:`, the env entry, a `.dax.yaml`
  in cwd, `-f`) and every one only adds. So an env cannot decline a globally
  inherited feature, which is why switching one env to `claude_tenant_state`
  takes four writes: a hand-edit to strip `claude` from the global list, plus
  three `dax env set … features claude` calls to give it back to the envs that
  still want it.

  Fit-and-polish fix, deferred: make `claude_tenant_state` **supersede an
  inherited `claude`** (it was designed as the replacement for that mount, not a
  peer), printing `claude_tenant_state supersedes claude for <env>` so it is
  never silent — while still *refusing* when both are listed explicitly on the
  same env, where the intent is genuinely ambiguous. That reduces the four writes
  to one and leaves the global list alone. The general form — a `-claude` removal
  syntax any env could use — is a bigger mechanism and not worth building until
  something needs it.

- **`dax init` should prompt for `tenant`** at registration time — editing an
  existing env is now covered by `dax env set`.

- **Retire `dax tenants` and `dax tenant classify`** — superseded by the merged
  `dax envs list`.

- **`_run_init` matches projects by exact `dir == cwd`** (`dax_creds/init.py`)
  — the same blind spot `find_enclosing_project` closed for `cmd_run`, so
  `dax init` from inside a registered project's subdirectory still registers a
  duplicate project. Worth fixing in the same pass as wiring `tenant` into
  init.

- **`claude` wrapper doesn't seed `oauthAccount`/`userID`** — cosmetic only
  (omitting it just costs a profile-fetch round trip and the org name in the
  first banner, per `docs/design/2026-07-27-tenant-isolation.md`'s seed-key
  table), but seeding it correctly needs real account data (`emailAddress`,
  `organizationName`, `accountUuid`), and nothing in `dax_creds/` currently
  sources or relays that from the host into a container. Would need a new
  daemon action (or a new credential-envelope field) reading it out of the
  host's own `~/.claude.json`. Not attempted as part of the tenant-isolation
  seed-template work — out of scope for a wrapper-only change.

- **Container shell lands in `$HOME`, not the project mount** — every dax
  session starts the user at `$HOME`, requiring a manual `cd <project>`
  before anything useful (including `claude`, once tenant isolation refuses
  to start there anyway). No `-w`/`--workdir` docker flag exists in `dax.py`
  today — `Dockerfile.tmpl`'s static `WORKDIR` can't know a per-project mount
  name at build time. Fix would be a small `cmd_run()` addition using stage
  3's already-computed `workdir_name`. Landing the shell at the mount root
  is a strict improvement in both single- and multi-tenant cases (see
  discussion in `docs/design/2026-07-27-tenant-isolation.md`'s history) but
  is an ergonomic change orthogonal to the tenant-isolation bug fix itself —
  deferred, not part of any current stage.

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
