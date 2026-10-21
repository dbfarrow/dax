# Changelog

dax uses [semantic versioning](https://semver.org/):

- **Major:** something has to change by hand, such as a removed or renamed command, or a `~/.dax.yaml` change that breaks or warns about existing entries.
- **Minor:** a new command, feature or config key, or any change that affects the image, with old configs still working.
- **Patch:** fixes only.

History before 3.0.0 lives in git.

## 3.0.0 — 2026-10-21

The env-lifecycle consolidation. Design and every decision behind it: `docs/design/2026-10-18-env-bootstrap-consolidation.md`.

### Removed

- `dax init` and `dax process new`. Running `dax run` in an unregistered folder now starts a wizard that creates the env, named after the folder.
- `dax process destroy`, `archive`, `restore`, `export` and `import`. They are now `dax env destroy`, `archive` and `restore`; export and import are choices inside archive and restore.
- `dax envs`, `dax tenant` and `dax tenants`. The listing is now `dax env list`.
- `dax env set` and `dax env accept-shared-files`. Use `dax config` to edit `~/.dax.yaml`.
- Every `--dry-run` flag, and `--include-credential`. `--explain` stays.
- Auggie support (the `auggie` feature, provider and wrapper).
- The multi-tenant model. Each env has exactly one tenant.
- The shared-mount `claude` feature, and the drift tracking for shared Claude files.

### Changed

- Claude is always on. Each env gets its own state tree at `~/.local/state/dax/tenants/<tenant>/<env>/`, and its credential `claude-<tenant>-<env>` is implied, so it is never listed in `creds:`.
- Every env needs a `tenant:`.
- Claude logins write to a temporary folder, never the host's `~/.claude`.
- Your global `CLAUDE.md` and settings are copied into a new state tree once, rather than kept in sync.
- A non-empty `mounts:` list turns mounting on by itself.
- The substrate path is one top-level `substrate:` key, and a process opts in with `features: [substrate]`.
- Archives never hold credentials. Restore only goes into an empty, unregistered folder, never overwrites anything, and names the env after that folder.
- A missing `~/.dax.yaml` exits with instructions; dax never creates one.

### Added

- `dax config`, which opens `~/.dax.yaml` in `$VISUAL`/`$EDITOR`, keeps the previous version as `~/.dax.yaml.prev`, and checks the result. It runs on the host only.
- Per-tenant browser profiles for Claude logins: a top-level `tenants:` block, merged over `credential_defaults`. The `dax run` wizard asks for one when it sees a new tenant.
- `dax env list` changes:
  - a `kind` column: `code`, or a process's type, read from its `context.md` (also in `dax env show`, with skill and title);
  - an `archived` column, showing how long ago each env was last archived to `backup_dir`;
  - tenants grouped in your `tenants:` order;
  - `hidden: true` on a tenant in `tenants:` leaves its envs out of the list;
  - `--all`, which lists hidden tenants and envs that exist only as archives.
- Changing an env's `tenant:` in `dax config` offers to move its Claude state tree and rename its credential in Keychain to match.
- `dax --version`. `dax build` labels the image with its version, and `dax run` warns when the image was built by a different version.

### Updating `~/.dax.yaml` from 2.x

`dax run` names each of these on every launch until it's removed:

- Give every env a `tenant:`.
- Remove `claude`, `claude_tenant_state`, `auggie` and `mounts` from `features:`, and Claude or auggie credentials from `creds:`.
- Move a per-env `substrate:` path to a single top-level `substrate:`.
- Remove `multi_tenant` and `tenant_subdir`.

Then run `dax build`: the dax code inside the image only changes on a rebuild.
