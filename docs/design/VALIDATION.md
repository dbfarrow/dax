# Manual validation run

Steps to prove the substrate wiring works end to end on a real process, before dax automates
any of it. Run in order. Each step notes what dax will eventually own.

Nothing here has been exercised against real mounts — every test so far used `HOME` overrides
against sandbox directories, which covers the logic but not grpcfuse behavior on symlinks into
a mounted state tree. That gap is the reason for this document.

Throughout: `<substrate>` is wherever this repo is mounted, `<process>` is the new process
directory.

---

## Part 1 — Provision (host / dax)

**1.1** Create an empty host directory for the process. No git, no contents.

**1.2** Define a dax env that mounts:

| Mount | Path in container | Mode |
|---|---|---|
| the process directory | `$HOME/<process>` | read-write |
| this repo | `$HOME/<substrate>` | read-write — the feedback path writes here |
| the env's state tree | `$HOME/.claude` | read-write — `wire.sh` writes here |

**1.3** Start the container. Do **not** start a Claude session yet.

> *dax owns all of 1.1–1.3. The mount names are dax's to choose; nothing in this repo
> depends on them.*

**Checkpoint:**
```sh
mountpoint -q "$HOME/<substrate>" && echo mounted
test -w "$HOME/.claude" && echo state tree writable
```
Both must print. A read-only state tree fails everything downstream.

---

## Part 2 — Scaffold and wire (in container)

**2.1** Scaffold the process contents. Choose the git answer deliberately — for this run,
use `--no-git`, since exercising the more constrained path is more informative:

```sh
$HOME/<substrate>/shared/new-process.sh \
    --dir "$HOME/<process>" \
    --title "Validation run" \
    --type ops \
    --no-git
```

Expect four files and `Git: none, by choice.` Confirm `CLAUDE.md` records the decision:

```sh
grep -A2 '^## Retention' "$HOME/<process>/CLAUDE.md"
```

**2.2** Confirm the gate has no default — this must fail and create nothing:

```sh
$HOME/<substrate>/shared/new-process.sh --dir /tmp/gate-test --title X --type ops </dev/null
test -e /tmp/gate-test && echo "FAIL: directory created" || echo "OK: nothing created"
```

**2.3** Wire the environment:

```sh
$HOME/<substrate>/shared/wire.sh
```

Expect five lines: rules, skills (11), hooks, settings, wired.

**2.4** Run it again. Output must be identical — it runs on every container start.

**2.5** Verify:

```sh
$HOME/<substrate>/shared/wire.sh --check    # exit 0
```

> *dax owns invoking 2.1 once and 2.3 on every start, and should gate startup on 2.5.*

---

## Part 3 — Verify the wiring took

**3.1** Links resolve (`-e` is false for a dangling symlink, which is the failure that would
otherwise be silent):

```sh
test -e "$HOME/.claude/rules/telos" && echo OK
ls "$HOME/.claude/rules/telos/"          # the telos files
readlink "$HOME/.claude/rules/telos"     # points into <substrate>, not a host path
ls -l "$HOME/.claude/skills/" | head     # 11 symlinks
```

`readlink` printing a host path rather than a container path means wiring ran in the wrong
place.

**3.2** The settings fragment exists and `settings.json` was **not** touched:

```sh
jq '{env, permissions, hooks: (.hooks | keys)}' "$HOME/.claude/substrate-settings.json"
cat "$HOME/.claude/substrate-root"
```

`env.SUBSTRATE_ROOT` set, `permissions.additionalDirectories` includes the substrate, three
hook events present, marker file holds the substrate path.

`~/.claude/settings.json`, `CLAUDE.md`, and `settings.local.json` must be **byte-identical** to
what dax delivered. `wire.sh` never writes them; if any changed, that's a defect.

**3.3** Start a Claude session with cwd `$HOME/<process>`, with dax delivering the fragment
(see the contract below).

**3.4** In session, run `/context`. Under **Memory files**, expect every file in `telos/` plus
the process conventions. If they're absent, nothing else in this part will pass.

Symlinks are displayed by their **resolved target**, not the link name, so the list shows
`telos/BELIEFS.md` and `shared/PROCESS-CONTEXT.md` — not `~/.claude/rules/telos/BELIEFS.md` or
`00-substrate.md`. `shared/PROCESS-CONTEXT.md` in the list *is* `00-substrate.md`; its absence
under that name is correct. Seeing any `~/.claude/rules/...` path would mean something else is
going on.

Expect roughly 13k tokens of telos plus ~1.7k of conventions.

**3.5** Confirm the hooks are actually registered, which is the same thing as confirming dax
delivered the settings fragment:

```sh
tail -3 ~/.claude/discernment-instructions.log
```

`InstructionsLoaded` fires at session start, so entries timestamped from this session prove
all three hooks are live. A missing or stale file means the fragment wasn't delivered — and
therefore that the PreToolUse guard is not running. `/hooks` in-session shows the same thing.

**Do not use a conversational question as the delivery test.** Asking "what substrate are you
working from?" looks like it probes the SessionStart output, but Claude can answer it by
running `git` itself, so a correct answer proves nothing. The tell that the hook was *not* the
source is detail the hook never prints — a full-length SHA, a commit subject, a file-level
breakdown — or the absence of detail it always prints, like the skill count.

As a secondary check, the hook's exact two lines should appear in context: the mount path with
a **short** hash, and `N skills linked`.

**3.6** **Content probe — the real test.** File listings only prove files loaded. Ask something
answerable *only* from telos:

> What does my telos say about on-call work?

A correct answer names on-call as incompatible with the margin goal and structurally
pre-compressing margin before any incident. A vague or generic answer means telos is present
as filenames but not as grounding.

**3.7** Skills are discoverable — type `/` and confirm the five type skills and five thinking
packs appear.

**3.8** Auto-memory is isolated:

```sh
ls "$HOME/.claude/projects/"
```
Should key on the process directory, not on the substrate. Nothing from another process should
appear.

---

## Part 4 — Verify the failure modes

The whole architecture rests on these failing loudly rather than silently. Test them.

> **Restart a Claude session, not a dax session.** `wire.sh` runs on every container start, so
> restarting the container repairs whatever you just broke and the test silently passes for the
> wrong reason. Self-healing is a feature everywhere except in Part 4. Break the wiring, then
> either stay in the session you're in or exit and re-run `claude` — without letting `wire.sh`
> run in between.

**4.1 Dangling rules link.**

```sh
mv "$HOME/.claude/rules/telos" "$HOME/.claude/rules/telos.bak"
```

The PreToolUse guard stats the filesystem on every tool call, so this should block tool use
**immediately, in the session already running** — no restart required. Expect every tool call
refused, with Claude reporting the substrate is missing and stopping rather than working
around it. Restarting `claude` additionally surfaces the SessionStart warning in context.

Probe with two tools at once, so a single blocked call isn't mistaken for a tool-specific rule:

> Read /etc/hostname and run `echo hi`

Both must be refused. Note that the UI renders **attempted** calls the same way it renders
completed ones — a line reading `Read 1 file, ran 1 shell command` above a block report means
both were attempted and both were denied, not that either ran. Judge by whether output came
back, not by the tool-call line.

Blocking is deliberately blanket rather than scoped to "risky" tools. `Read` on `/etc/hostname`
and `Read` on counterparty material are the same tool, so any carve-out for diagnostics is a
carve-out for ungrounded process work — and a partly working session invites continuing rather
than repairing. Repair happens from a container shell, not from inside the session, so nothing
needed is lost.

Restore with `wire.sh`.

**4.2 Unresolvable substrate root.** The hooks resolve `$SUBSTRATE_ROOT` first and fall back to
`~/.claude/substrate-root`, so breaking this needs both gone at once — and the env var is fixed
in a running session, so deleting only the marker proves nothing there. To test the fallback
chain live, delete the marker *and* start a fresh `claude` with the fragment's `env` block
removed.

This one is lower value than it looks: the fail-closed behavior is deterministic and already
verified outside the container. If short on time, skip it and spend the effort on 4.3.

Restore with `wire.sh`.

**4.3 Substrate unmounted.** The exception to the note above — this one *requires* a dax
restart, since changing mounts is what's being tested, and `wire.sh` running is part of the
expected behavior rather than something to avoid.

Stop the container, remove the substrate mount from the env, restart.

**`wire.sh` cannot report this failure — it lives on the mount, so it is gone too.** Invoking
it fails at the shell with "no such file or directory" (exit 127), not with a message from the
script. That is a distinct case from a mounted-but-broken substrate, where the script does run
and refuses with `no telos/ at <path> — not a substrate repo` (exit 1). Both are non-zero, so a
dax gate on non-zero catches both, but they need different error messages.

If a session starts anyway, the guard must block every tool call: the state-tree symlinks
survive the unmount as **dangling** links, which still look present in a directory listing
while resolving to nothing and reporting nothing on their own.

**This is the failure mode the split introduces and the single most important test here** —
the old single-repo layout could not produce it, because the substrate and the process were
the same directory.

**4.4 Confirm SessionStart alone is not enough.** With the substrate broken, note that the
session still *starts* — SessionStart cannot abort. The guard is what makes it inert. If tool
calls succeed while the substrate is broken, the design has failed.

---

## Part 5 — Feedback path

**5.1** From the process session, edit something in the substrate — add a line to
`telos/WISDOM.md` or a note to a skill.

**5.2** Commit it *in the substrate repo*, generalized: no proper names, no situational detail.

**5.3** Confirm the process directory is untouched by that commit — separate repos, separate
histories.

**5.4** Start a new session and confirm `substrate-check.sh` reports the new commit, and
reports uncommitted changes when the substrate is left dirty.

> *5.1–5.4 must work identically for a `--no-git` process. That's the point: version control
> in the process directory is independent of write access to the substrate.*

---

## Part 6 — Teardown

Not yet built, and the half that matters most for deletability.

**6.1** Destroy the process directory, the dax env, and the state tree.

**6.2** Confirm the auto-memory pool is gone:

```sh
ls "$HOME/.claude/projects/"   # from a different env — the process's namespace must be absent
```

**6.3** Confirm nothing about the process reached the substrate repo:

```sh
git -C "$HOME/<substrate>" log --all -p | grep -i "<process name or counterparty>"
```
No hits. If there are, the write-back rule was not followed and the history is not scrubbable.

> *dax owns `destroy-process`. Auto-memory is the piece most easily left behind: it lives in
> the state tree, outside git, and accumulates situational detail nobody explicitly wrote.*

---

## The dax contract

Everything dax must do. This is the authoritative list — `README.md` explains *why* each
piece exists, but implement from here.

### 1. Mounts

| Mount | Container path | Mode | Why |
|---|---|---|---|
| process directory | `$HOME/<process>` | rw | the working directory |
| substrate repo | `$HOME/<substrate>` | rw | read-only breaks the feedback path |
| env state tree | `$HOME/.claude` | rw | `wire.sh` writes `rules/`, `skills/`, `hooks/` here |

dax chooses all three names. **Nothing in the substrate depends on them** — the scripts
self-locate via `dirname $0`, so renaming or relocating the substrate is a one-line change
here and nowhere else.

### 2. `new-process` — once, at creation

```sh
$HOME/<substrate>/shared/new-process.sh \
    --dir "$HOME/<process>" --title "..." --type <type> (--git | --no-git)
```

- Prompt the user for title, type, **and the git decision**.
- Read valid types from `$HOME/<substrate>/shared/process-types.tsv`. Do not hardcode them —
  adding a process type must not require a dax release.
- **The git decision has no default.** Passing neither flag non-interactively is an error, by
  design: choosing git when you shouldn't have is unfixable, since history can't be
  selectively scrubbed. Surface it as a real question, not a checkbox with a preselected value.
- Under `--git` the script inits the repo and makes the first commit. Under `--no-git` it
  writes files only. **Do not create a repository yourself**, and do not add a remote — a
  remote reproduces the deletability problem one layer out.

### 3. `wire.sh` — every container start

```sh
$HOME/<substrate>/shared/wire.sh --quiet
```

Idempotent, and intended to run on **every** start rather than once at creation, so wiring
drift self-heals when the substrate changes. Must run **after mounts are up and before the
session starts.**

### 4. Deliver the settings fragment — **required**

`wire.sh` writes `$HOME/.claude/substrate-settings.json`. It deliberately does **not** write
`~/.claude/settings.json`, `CLAUDE.md`, or `settings.local.json`, because dax delivers those
from a synced global file — a write there would be clobbered on the next sync or silently
detached from what the session reads.

dax must deliver the fragment. Either route works:

```sh
claude --settings "$HOME/.claude/substrate-settings.json"     # loads additional settings
```
or merge the fragment into the settings dax already syncs.

The fragment carries three things: `env.SUBSTRATE_ROOT`, `permissions.additionalDirectories`
(write access for the feedback path), and the three hooks.

**If delivery is skipped, the session still grounds correctly** — rules and skills are
directory-based and load with no settings at all. What's lost is the verification layer: no
substrate report at session start, and **no PreToolUse guard**, which is the thing that makes
a broken mount fail loudly instead of silently. Treat delivery as mandatory.

### 5. Gate startup

```sh
$HOME/<substrate>/shared/wire.sh --check      # exit 0 = wired
```

Refuse to launch on non-zero, and distinguish the two failures — they have different causes and
different fixes:

| Exit | Meaning | Fix |
|---|---|---|
| `127` / command not found | **the substrate isn't mounted** — the script is on the mount, so it's gone with it | repair the mount in the env definition |
| `1` | substrate mounted but incomplete, wrong, or not wired | run `wire.sh`; if it still fails, the mount points at the wrong repo |
| `0` | wired | — |

A missing script cannot report its own absence, so dax must treat "command not found" as a
first-class failure rather than assuming a non-zero exit came from the script.

This verifies the substrate *side* only. Whether dax actually delivered the settings fragment
can't be checked from inside the substrate — step 3.5 is the end-to-end test for that.

### 6. `destroy-process`

Remove **all three**: the process directory, the env, and the state tree. Auto-memory lives at
`~/.claude/projects/<repo>/memory/`, outside git, and accumulates situational detail nobody
explicitly wrote. It is the piece most easily left behind, and leaving it behind means the
deletion was not real.

### What dax must not do

- Don't hardcode the substrate's name or path — it moves, and the scripts don't care.
- Don't hardcode the process types — read the TSV.
- Don't create the process repo or a remote — `new-process.sh` owns that, gated on `--git`.
- Don't default the git decision.
