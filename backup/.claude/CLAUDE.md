# Global Claude Instructions

## Context Saving — Project vs. Global

When the user asks to "save what we're doing", "save context", "save our progress", or similar:

- **Default: save locally** — write to the project's `CLAUDE.md` in the current working directory, or to project-local files within the repo. Do NOT write project-specific state to `~/.claude/CLAUDE.md`.
- **Exception: global save** — only write to `~/.claude/CLAUDE.md` or global memory when the user explicitly says "save globally", "add to global context", or similar.

**Why:** Multiple containers share `~/.claude`. Each runs a different project. Global writes cause context from one project to bleed into unrelated sessions.

Note: The auto-memory system (`~/.claude/projects/<path>/memory/`) is already scoped by working directory path and is safe to use normally — it won't bleed between projects with different paths.

---

## SSH / GitHub Known Hosts

Container restarts wipe `~/.ssh`, so `known_hosts` is often empty or missing. When a git or SSH operation fails due to an unknown GitHub host (or when `~/.ssh/known_hosts` is empty or absent):

1. Automatically run: `ssh-keyscan -H github.com >> ~/.ssh/known_hosts`
2. Retry the original operation.

Do this without prompting the user — do not get bogged down asking for confirmation on this specific issue.

---

## Git Config — Container Limitations

`~/.gitconfig` is bind-mounted as a single file via grpcfuse (Docker Desktop). It is readable but **not writable by git** — git's atomic write (write to temp + rename) fails with "Device or resource busy".

- **Never run `git config --global`** — it will always fail in this environment.
- Use `-c key=value` inline flags for one-off overrides.
- Use `git config --local` (writes to `.git/config`) for repo-scoped settings.
- For `safe.directory`: this should already be set in `~/.gitconfig` from the host. If git complains about dubious ownership on a fresh container, use `-c safe.directory=<path>` as a fallback.
