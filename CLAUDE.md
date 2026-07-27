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
  Sequencing steps 1–2 (credential wrapper fix, onboarding seed) are done and
  merged (PR #4). Remaining: steps 3–7 — mount relocation, seed-template
  wiring for new trees, tenant resolution in the `claude` wrapper, `dax tenant
  set`/`dax tenants`, and the credential-span escalation check.

## Backlog

- **`save_config` destroys `~/.dax.yaml` comments and key order** — `dax_creds/init.py:26`
  loads via `yaml.safe_load` and rewrites with `yaml.dump`, so comments (which
  aren't in the parsed dict) vanish and keys get alphabetized by the default
  `sort_keys=True`. Triggered by `dax creds add`, `dax creds update`, and
  `dax init`. Fix: switch to `ruamel.yaml` round-trip mode; stopgap is
  `sort_keys=False` plus a `.dax.yaml.bak` before each write.

## Notes

- `safe.directory` is already set in `~/.gitconfig` — no need for `-c safe.directory=` flag in git commands.
- Files NOT to back up: credentials, `~/.claude/projects/`, `history.jsonl`, caches, sessions, auggie ephemeral data.
