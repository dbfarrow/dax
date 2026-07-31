# Migrating an Env's Claude State Into Its Own Tree

Runbook for moving one env off `feature_claude`'s shared `~/.claude` mount and onto
`claude_tenant_state` (decision B) **without losing its accumulated session
state**. Written 2026-07-31 while migrating `dax`; `fabric` was switched over
without a migration and started from an empty tree, which is the other valid
choice.

Every step runs on the **host**. The destination tree is not mounted into any
container except the one that owns it, so a container cannot migrate itself.

**Steps 1-5 are scripted** — `tools/migrate_env_state.py <env>` does the
preconditions, the transcript copy, the history filter, and the credential and
`.claude.json` checks, dry-run by default:

```bash
python3 tools/migrate_env_state.py dax            # shows the plan
python3 tools/migrate_env_state.py dax --apply
```

It deliberately does **not** touch `~/.dax.yaml` (step 6 stays a manual
`dax env set`) and does not handle `.claude.json` (step 4), which has to be copied
out of the running container before shutdown. The prose below is the reference for
what it does and why, and for the steps it leaves to you.

---

## What is actually per-env, and how it separates

Checked against the real shared directory rather than assumed. The good news is
that the two big items carry their own scoping key:

| Item | Size | Per-env? | How |
| --- | --- | --- | --- |
| `projects/<mangled-cwd>/` | 6.3M for dax | **yes** | Directory name *is* the container cwd (`/home/dfarrow/dax` → `-home-dfarrow-dax`). Holds the session transcripts and the auto-memory dir. |
| `history.jsonl` | 756K total, 92 of 2185 lines were dax | **yes, filterable** | Every line carries a `project` field holding the container cwd. |
| `~/.claude.json` | 35K | **already** | Its `projects{}` block had exactly one key, `/home/dfarrow/dax`. Copy wholesale. |
| `file-history/` | 9.6M, 27 dirs | mappable | Keyed by session UUID, and the env's UUIDs are the `.jsonl` filenames in its `projects/` dir. Low value (edit-undo history) — skip unless you want it. |
| `paste-cache/`, `shell-snapshots/`, `backups/`, `cache/`, `sessions/`, `tasks/` | small | no | Regenerable or not worth mapping. |
| `CLAUDE.md`, `settings.json`, `settings.local.json` | 2K | **no — user-level** | Mounted read-only into every tree by the feature. Do not copy. |

The mangled name is stable across the migration: `workdir_name` doesn't change, so
the container cwd stays `/home/dfarrow/<env>` and Claude Code keeps using the same
`projects/` key.

## 0. Prerequisite

The env must already have a `tenant`, and the feature must mount the user-level
files. Confirm both:

```bash
dax env show <env> | grep -E 'tenant|state'
grep -n "_CLAUDE_HOST_SHARED_FILES" ~/fatsec/dax/dax.py
```

Without the second, the tree mount hides your global `CLAUDE.md` and settings —
silently. `fabric` ran a full day that way before it was noticed.

## 1. Stop the env's container

The transcript is being appended to while a session is live, so a copy taken
mid-session is torn.

```bash
docker ps --filter name=<container> --format '{{.Names}}'   # expect nothing
```

## 2. Copy the transcripts and memory

```bash
ENV=dax
KEY=-home-dfarrow-$ENV
TREE=~/.local/state/dax/tenants/personal/$ENV

mkdir -p "$TREE/projects"
cp -a ~/.claude/projects/$KEY "$TREE/projects/"
du -sh "$TREE/projects/$KEY"        # expect to match the source
```

`cp -a`, not `mv`: the original stays until the migrated env has run cleanly. It
costs disk and nothing else, and it is the only rollback.

## 3. Filter this env's shell history into the tree

```bash
python3 - <<PY
import json, pathlib
env_cwd = '/home/dfarrow/$ENV'
src = pathlib.Path.home() / '.claude' / 'history.jsonl'
dst = pathlib.Path("$TREE") / 'history.jsonl'
kept = [l for l in src.read_text().splitlines()
        if l.strip() and json.loads(l).get('project') == env_cwd]
dst.write_text('\n'.join(kept) + '\n')
print(f'{len(kept)} lines -> {dst}')
PY
```

Filtered rather than copied: the shared file holds every env's prompts, and moving
it wholesale would pool other envs' history into this tree — the exact thing the
isolation is for. Arrow-up history then contains only this env's prompts, which is
decision 11's stated intent.

## 4. Seed `.claude.json` from the live container copy

This is the file that would otherwise cost you the trust dialog, `allowedTools`,
and replayed tips (see the design doc's "Accepted costs" table). Take it from the
env's most recent container if you still have it — its `projects{}` block is
already scoped to that one env:

```bash
# from inside the env's container, before shutting it down:
cp ~/.claude.json /tmp/claude-json-$ENV
# then on the host:
docker cp <container>:/tmp/claude-json-$ENV "$TREE/.claude.json"
```

If the container is already gone, skip this — the wrapper seeds
`{"hasCompletedOnboarding": true}` on first launch, which is enough to reach a
normal prompt. You lose only re-consent and tips.

**Check before copying** that `projects{}` holds only this env's path:

```bash
python3 -c "
import json; d=json.load(open('$TREE/.claude.json'))
print(list((d.get('projects') or {}).keys()))"
```

More than one key means it came from a shared-mount host rather than a container,
and copying it would carry other projects' state in.

## 5. Confirm no stale credential

```bash
ls -la "$TREE/.credentials.json" 2>/dev/null && echo "DELETE THIS" || echo ok
```

A credentials file in the tree **blocks** the Keychain bootstrap: the wrapper
injects only when the file is absent or empty (`-s`), never when it is merely
invalid or stale. Delete it and let the env bootstrap from its own Keychain entry.

## 6. Switch the env over

```bash
dax env set $ENV features claude_tenant_state
dax env show $ENV                       # features: claude_tenant_state
cd ~/fatsec/$ENV && dax -t run          # inspect before launching
```

The dry run must show `adding claude_tenant_state` (not `adding claude`), plus:

```
-e CLAUDE_CONFIG_DIR=/home/dfarrow/.claude
--volume=…/state/dax/tenants/personal/<env>:/home/dfarrow/.claude
--volume=/Users/dfarrow/.claude/commands:/home/dfarrow/.claude/commands
--volume=/Users/dfarrow/.claude/plugins:/home/dfarrow/.claude/plugins
--volume=/Users/dfarrow/.claude/CLAUDE.md:/home/dfarrow/.claude/CLAUDE.md:ro
--volume=/Users/dfarrow/.claude/settings.json:/home/dfarrow/.claude/settings.json:ro
--volume=/Users/dfarrow/.claude/settings.local.json:…:ro
```

Because per-env features are additive only, `claude` must not be in the global
`features:` list — otherwise both target `~/.claude` and `dax run` refuses. Push
`claude` down onto the envs still using the shared mount.

## 7. Verify inside

```bash
echo $CLAUDE_CONFIG_DIR            # /home/dfarrow/.claude
ls ~/.claude.json                  # must NOT exist — it belongs inside the tree
ls ~/.claude/.claude.json          # here instead
ls ~/.claude/projects/             # only this env's key
wc -l ~/.claude/history.jsonl      # only this env's lines
head -3 ~/.claude/CLAUDE.md        # your global instructions, via the ro mount
claude                             # then /resume — the migrated sessions
```

`/resume` listing the migrated sessions is the real proof: transcripts, memory,
and history all landed where Claude Code looks for them.

## 8. Then, and only then, clean up the source

After a successful session **and** a container restart that shows the state
persisting:

```bash
rm -rf ~/.claude/projects/$KEY
```

Leave the shared `history.jsonl` alone — the other envs still on the shared mount
are using it.
