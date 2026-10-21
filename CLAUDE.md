# dax — Project Context

dax is a Docker-based development environment manager. It reads `~/.dax.yaml`
(and an optional `.dax.yaml` in the cwd) to build a `docker run` command with
features (volume mounts, port mappings, etc.) assembled from named feature
functions in `dax.py`.

This file is the current state only. How dax got here (every decision since
2026-07, and why) is in `docs/history/claude-md-history-2026.md`, and most
commands named there no longer exist. The 3.0 design, with all 45
decisions, is `docs/design/2026-10-18-env-bootstrap-consolidation.md`.

## Current state — dax 3.0.0 (2026-10-21)

- **Commands:** `build`, `run`, `features`, `backup`, `creds`
  (`add`/`list`/`update`/`login`/`remove`), `config`, `env`
  (`list`/`show`/`archive`/`restore`/`destroy`), and `--version`.
  - **Gone:** `dax init`, `dax process`, `dax envs`, `dax tenant`/`tenants`,
    `dax env set`, `dax env accept-shared-files`, every `--dry-run`, and
    `--include-credential`.
- **Creating an env:** `dax run` in an unregistered folder runs the
  `_bootstrap_env` wizard. The env is named after the folder. A process env
  gets `new-process.sh` run after the confirm. `dax -t run` is a pure
  preview: it refuses in an unregistered folder, and never creates or seeds
  a state tree.
- **Editing:** `dax config` (host only). It keeps `~/.dax.yaml.prev`, checks
  the result, and, when an env's `tenant:` changed, offers to move its state
  tree and rename its Keychain credential (decision 44).
- **An env entry:** `dir` and `tenant` (both required), `image` (the wizard
  writes `dax:latest`; `dax-base` is an alias), `creds` (non-Claude only),
  `features` (opt-ins), `mounts`. A missing or empty list key counts as
  empty.
- **Claude:** always on (`feature_claude`), per env.
  - The state tree lives at `~/.local/state/dax/tenants/<tenant>/<env>/`,
    and the credential `claude-<tenant>-<env>` is implied, resolved
    centrally in `resolve_credential_names`.
  - Logins write to a temp folder under `~/.local/state/dax/login/`, never
    the host `~/.claude`.
  - Shared `CLAUDE.md`/settings are copied in once.
  - `dax run` reruns the login when the last one is older than
    `defaults.relogin_cooldown_hours` (24).
- **`tenants:` block** (top level, decisions 43 and 45):
  - `tenants.<t>.claude` is merged over `credential_defaults.claude` in
    `synthesized_credential`. The wizard asks only for a brand-new tenant.
  - `hidden: true` leaves the tenant out of `dax env list` unless `--all`.
  - Its key order is the order `dax env list` shows tenants in.
- **`dax env list`:**
  - columns: kind (`code`, or a process's `type:` from its `context.md`),
    dir, creds, state, used, and archived;
  - grouped by tenant;
  - `[a]` `(archive-only)` rows with `--all`.

  `dax env show` adds kind, title, and which browser the login opens.
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
- **Versioning:** semver in `VERSION`, with `dax --version` and
  `CHANGELOG.md`. `dax build` labels the image `dax.version`, and `dax run`
  warns when the label differs from the host's version (decision 42).
  Release: bump `VERSION` as the branch's last commit, then put an
  annotated `vX.Y.Z` tag on `master` after the merge.
- **Legacy Claude history** (from before per-env state trees) is recovered
  on demand, not migrated. Mount the host's real `~/.claude` into an env
  (`mounts: ['~/.claude:host-claude']`), then have a session read
  `~/host-claude/projects/` and `history.jsonl`. Don't delete those on the
  host. `tools/migrate_env_state.py` remains for the unambiguous case.
- **Tests:** 813 passing. Manual runbook:
  `docs/testing/2026-10-19-env-lifecycle-test-sequence.md`.

## Working rules

- **Tests must never write the real `$HOME`.** A dax container bind-mounts
  the host's `~/.dax.yaml` **rw**, so a test that writes `$HOME/.dax.yaml`
  destroys the live config: env definitions, and credential metadata that
  exists nowhere else. It happened on 2026-07-31. `tests/conftest.py`
  redirects `HOME` for every test via an autouse fixture; don't remove it.
  Pass an explicit `HOME=<tmpdir>` to ad-hoc scripts too.
- **Host vs container code.** On the host, `dax` is an editable install of
  this repo (host path `~/fatsec/dax`), so edits take effect immediately.
  The image's `dax`/`dax_creds` is a frozen copy, so in-container behavior
  changes only after `dax build`. Before a rebuild, test in a container with
  `python3 ~/dax/dax.py`, and remember that `import dax_creds` there is the
  installed copy.
- **Checks that must run on the host:** Keychain (keyring), Docker, the real
  terminal, OneDrive. A container can't verify credentials. `dax config`
  refuses to run in a container.
- **Never edit `~/.claude/{CLAUDE.md,settings.json,settings.local.json}`
  inside a container.** A write can silently land in a detached
  container-local copy. Draft the change and have the user apply it on the
  host.
- **Tests:** `tests/conftest.py` also stubs the implied Claude login (opt
  out with `@pytest.mark.real_claude_login`) and the image-version lookup,
  and provides `launch_without_docker` for a real (non-`-t`) `cmd_run`.
  - After a sed-and-restore mutation check, clear `__pycache__`: a
    same-size, same-second restore can leave stale bytecode.
- `safe.directory` is already set in `~/.gitconfig`, so git commands need no
  `-c safe.directory=` flag.
- **Files not to back up:** credentials, `~/.claude/projects/`,
  `history.jsonl`, caches, sessions.

## Backlog

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

- **`new-process.sh`: merge frontmatter into an existing `context.md`**
  rather than refusing when it already exists. It's a design question:
  what does "merge" mean when the existing file conflicts with the
  template? The write-if-absent fix for `CLAUDE.md`/`inbox.md` exists only
  in `docs/design/scripts/new-process.sh` here, an untracked reference copy.
  Both changes belong in the real script in the virgil/discernment repo,
  ported by hand.
