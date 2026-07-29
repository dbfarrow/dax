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

## In progress

- **Tenant isolation for Claude Code state** — design and status in
  `docs/design/2026-07-27-tenant-isolation.md`. Replaces the shared `~/.claude`
  mount with per-tenant/project state trees selected via `CLAUDE_CONFIG_DIR`.
  Steps 1–2 merged in PR #4. Steps 3–6 implemented, unit-tested, and
  **confirmed on the real repos this design exists for** — `fabric`
  (single-tenant) and `discernment` (multi-tenant, `processes/` subdirs) —
  not just scratch projects. Discernment specifically: two concurrent `claude`
  sessions in different tmux windows, different labeled processes, same
  container — each isolated, each persisted its own history correctly across
  quit/restart, zero cross-tenant leakage. That's the actual bug from this
  doc's Problem section, fixed and observed, not just theorized. Step 7
  dropped — see the design doc's Sequencing section for why. Nothing is live
  by default — `claude_tenant_state` is opt-in per project via its own
  `features:` list.
  **Redesigned 2026-07-28**: the `unattributed` fallback is gone entirely —
  every undeclared location now either resolves, gets interactively
  classified before the container launches (`dax run` walks the repo root
  and, for multi-tenant projects, every undeclared child under
  `tenant_subdir`, refusing to launch until all are assigned), or hard-refuses
  (only for a subdirectory discovered mid-session, since Docker can't add a
  mount to an already-running container). `dax tenant classify` re-runs that
  walk on demand, re-prompting even already-declared entries. A multi-tenant
  repo's root can now also resolve as its own project/tenant (needed for
  working on discernment's own tooling independent of any one customer
  process). 253 tests pass. Still outstanding before trusting this as the
  default: the shared `plugins`/`skills`/`commands`/`cache` migration from
  `~/.claude` (scoped, not yet built — see below, and now caveated by a
  skills-symlink discovery: discernment's own project-specific skills should
  move to Claude Code's native project-scoped `.claude/skills/` instead of
  being blanket-shared to every tenant), and the multi-day refresh-token
  rotation soak (still unconfirmed; nothing tested so far ran long enough to
  hit it). The real fabric/discernment containers have not been re-tested
  against this latest classification-flow change yet — that needs an image
  rebuild (`dax_creds` is a frozen, non-editable install) plus a fresh manual
  pass.
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
- **Shared plugins/skills/commands/cache migration** — scoped but not yet
  built. Plan: one-time, host-wide copy (not a symlink) from
  `~/.claude/{plugins,skills,commands}` into
  `~/.local/state/dax/shared/{plugins,skills,commands,cache}` the first time
  `claude_tenant_state` runs and the shared dir doesn't exist yet, gated on
  `claude_tenant_state` actually being active, living in `cmd_run()`'s
  orchestration rather than inside `feature_claude_tenant_state` (which stays
  a pure argv-builder like every other `feature_*` function). Without this,
  a project opting in loses access to whatever plugins/commands are already
  installed under the old `~/.claude`.

## Backlog

- **`dax creds add` doesn't actually overwrite Keychain on re-add** —
  `_setup_claude_credential`'s `if provider.check(...): return` early-exit
  means confirming "Overwrite it?" only rewrites YAML metadata, not the
  Keychain secret itself. Found during tenant-isolation testing when a
  rotated credential wouldn't take; workaround is `dax creds remove` before
  `dax creds add`. Fix: the overwrite-confirmed path needs to actually call
  through to the provider's store, not early-return before it.
- **`dax creds login`'s browser callback doesn't work for Claude** — same
  class of bug as Auggie's already-known limitation. Workaround is the
  device-flow login instead. Found during the same tenant-isolation testing
  pass as the Keychain-overwrite bug above.

- **`save_config` destroys `~/.dax.yaml` comments and key order** — `dax_creds/init.py:26`
  loads via `yaml.safe_load` and rewrites with `yaml.dump`, so comments (which
  aren't in the parsed dict) vanish and keys get alphabetized by the default
  `sort_keys=True`. Triggered by `dax creds add`, `dax creds update`, and
  `dax init`. Fix: switch to `ruamel.yaml` round-trip mode; stopgap is
  `sort_keys=False` plus a `.dax.yaml.bak` before each write.

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

- `safe.directory` is already set in `~/.gitconfig` — no need for `-c safe.directory=` flag in git commands.
- Files NOT to back up: credentials, `~/.claude/projects/`, `history.jsonl`, caches, sessions, auggie ephemeral data.
