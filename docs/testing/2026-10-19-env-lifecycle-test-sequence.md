# Env Lifecycle Consolidation — Manual Test Sequence

Written 2026-10-19, before implementation, for the branch described in
`docs/design/2026-10-18-env-bootstrap-consolidation.md`. Run the matching
section after each implementation step lands. Exact prompt wording and
output don't exist yet; each section states the behavior that must hold,
and the exact expected output gets filled in as each step is built.

**Why a manual sequence at all.** The automated suite runs inside a dax
container, which can't reach the macOS **Keychain**, **Docker**, the real
**terminal** (questionary's keypress handling), the **OneDrive**
`backup_dir`, or Finder's `.DS_Store`. Every one of those is touched by this
branch. The suite proves the logic; this proves the wiring.

**Where things run.** Every command below is a **host** (macOS) command,
run in a plain terminal, not inside a dax container. A container has no
Keychain and no Docker, and it sees your files at different paths.
Keychain checks use `python3 -c "import keyring; ..."`, the same library and
service name dax uses.

**Your `dax` is the branch.** The host's `dax` is an editable install of
the repo at **`~/fatsec/dax`** (that's the host path; `/home/dfarrow/dax`
is only where the container sees it), where the branch is being built, so
plain `dax` runs whatever's there. Run a step's checks only after Claude
says that step is done, and don't `dax run` a real env mid-step. If things
go sideways: `cd ~/fatsec/dax && git stash -u && git checkout master` gets master back without losing the
work (`git checkout feature/env-lifecycle-consolidation && git stash pop`
resumes).

**Scratch envs only.** Everything here uses envs named `dxt-*`, in folders
under `~/test/` (e.g. `~/test/dxt-a`), so cleanup can find them by name.
Inside the container each one still mounts at `/home/dfarrow/<name>`:
dax mounts by folder name only, so the parent folder doesn't matter, and
the container paths in the checks below are unaffected. Real envs are only touched at the very end (a plain
restart of `fabric`).

**Answer `n` to login prompts unless the check needs Claude.** Each real
login mints a real OAuth grant. From step 4 on (the wizard writes
`claude` into `creds:`), and for every env once step 7 makes Claude
baseline, each new scratch env's first `dax run` asks "claude-dxt-<env> not in
Keychain. Run login now?". Answering `n` skips the login and the launch
continues. Only the checks marked **(login)** below need a real one.

**One login has no prompt to say `n` to.** The proactive relogin runs the
browser login unprompted for any env whose last login was over 24h ago
(`defaults.relogin_cooldown_hours`). So relaunching `dxt-b`, `dxt-b2` or
`dxt-f` on a later day than its last login opens a browser login straight
away. Completing it mints another grant; that's fine, just expected. If
the whole sequence is done within a day or two, expect about four grants;
spread over a week, a few more.

---

## Setup (once, before step 1's checks)

```bash
# 1. Confirm dax runs.
dax --help | head -3           # must work; if a traceback, stop and report

# 2. Back up the live config. Restoring this file undoes any registry damage.
cp ~/.dax.yaml ~/.dax.yaml.bak

# 3. Point backup_dir at a scratch folder so no test archive reaches OneDrive.
grep -n '^backup_dir:' ~/.dax.yaml      # note the real value
mkdir -p ~/test/dax-test-backups
#    edit ~/.dax.yaml: backup_dir: /Users/dfarrow/test/dax-test-backups

# 4. Baselines to compare against later.
ls -l ~/.claude/.credentials.json  2>/dev/null   # note mtime (or "absent")
dax creds list > ~/test/dax-test-backups/creds-before.txt
dax envs list  > ~/test/dax-test-backups/envs-before.txt   # step 8 compares against this
docker info > /dev/null && echo docker-ok
```

| Check | Pass |
|---|---|
| `dax --help` | prints usage, no traceback |
| `docker info` | `docker-ok` |
| `~/.dax.yaml.bak` | exists, same size as `~/.dax.yaml` |

**If any check below fails:** stop, copy the full output into the
conversation, and if `~/.dax.yaml` looks wrong, `cp ~/.dax.yaml.bak
~/.dax.yaml` (then re-apply the scratch `backup_dir` edit).

---

## Fixtures (once, before step 1's checks)

Two scratch envs. Until step 4 lands, `dax run` in an unregistered folder
still goes through the old `dax init` wizard; that's expected here.

```bash
# dxt-a: no credentials. Used for most checks; mints no grants.
mkdir ~/test/dxt-a && cd ~/test/dxt-a && echo hello > notes.txt
dax run        # wizard: default image, tenant "dxt", select NO credentials
                # inside the container: exit

# dxt-b: has a Claude credential and one real session, for the /resume
# check in step 3. This mints one real OAuth grant.
# Don't pick claude in the old wizard: its inline credential loop imports
# whatever token sits in the host's ~/.claude (the path decision 32
# removes). Register with no creds, then add one so dax run's own login runs.
mkdir ~/test/dxt-b && cd ~/test/dxt-b
dax run        # wizard: default image, tenant "dxt", select NO credentials; exit
                # edit ~/.dax.yaml: under dxt-b, CHANGE the existing
                #     creds: []            →  creds: [claude]
                # (don't add a second `creds:` line: a duplicate key makes
                #  ~/.dax.yaml fail to load) and add
                #     features: [claude_tenant_state]
                # (pre-step-7 code only gives an env a state tree when the
                #  feature is listed; step 7 makes both entries redundant)
dax run        # (login) "claude-dxt-dxt-b not in Keychain. Run login now?" → Y
                # complete the browser login
                # inside the container: claude, send one prompt
                # ("say hi"), /exit, exit
```

| Check | Pass |
|---|---|
| `dax env show dxt-a` (or `dax envs list` before step 8) | registered, tenant `dxt` |
| `ls ~/.local/state/dax/tenants/dxt/dxt-b/projects/` | one directory ending in `-dxt-b` |

---

## Step 1 — `destroy` moves to `dax env`; `set` dropped; unknown-key check

```bash
dax env --help
dax process --help        # process still exists until step 4; destroy must be gone from it
```

| Check | Pass |
|---|---|
| `env --help` | lists `destroy`; no `set` |
| `process --help` | no `destroy` |

**Unknown-key warning:** edit `~/.dax.yaml`, add `bogus: 1` under
`dxt-a`. Then `cd ~/test/dxt-b && dax run` (a *different* env), then `exit`.

| Check | Pass |
|---|---|
| launch output | a warning naming `dxt-a` and `bogus`; the launch proceeds |
| after removing `bogus: 1` and running again | no warning |

**Destroy:** make a throwaway third env, then destroy it.

```bash
mkdir ~/test/dxt-c && cd ~/test/dxt-c && dax run    # no creds, tenant dxt; exit
                                           # edit ~/.dax.yaml: under dxt-c add
                                           #     features: [claude_tenant_state]
dax run                                    # creates its state tree (no login: no creds); exit
ls ~/.local/state/dax/tenants/dxt/dxt-c    # exists, so the destroy check below means something
cd ~ && dax env destroy                   # from ~ (not an env): shows the picker; pick dxt-c, type DESTROY
dax env destroy dxt-c --dry-run           # flag no longer exists
```

| Check | Pass |
|---|---|
| destroy from `~` | shows the picker (doesn't exit for "not in an env"); asks for typed `DESTROY`; afterwards `~/test/dxt-c` and `~/.local/state/dax/tenants/dxt/dxt-c` are gone, and `dxt-c` isn't in `~/.dax.yaml` |
| `--dry-run` | argparse error (unrecognized argument) |
| while a `dxt-a` container is running (in another terminal), `dax env destroy dxt-a` | refuses with "stop it first"; type nothing |

---

## Step 2 — merged `dax env archive`

```bash
cd ~ && dax env archive        # pick dxt-a and dxt-b; managed; do NOT remove
ls ~/test/dax-test-backups/daily/
tar -tzf ~/test/dax-test-backups/daily/dxt-b-*.tar.gz | grep -E 'manifest|credentials'
dax env archive                # pick dxt-a; specific file: ~/test/dax-test-backups/one-off.tar.gz
dax env archive --explain | head -20
dax backup --explain | head -5
```

| Check | Pass |
|---|---|
| `daily/` listing | `dxt-a-<today>.tar.gz` and `dxt-b-<today>.tar.gz` |
| `tar` grep | exactly `dax-env-manifest.json`; **no** `.credentials.json` line |
| specific-file archive | `~/test/dax-test-backups/one-off.tar.gz` exists; no removal was offered |
| `archive --explain` | describes the new prompts; no mention of include-credential, `--dry-run` or `dax process` |
| `backup --explain` | its redirect points at `dax env archive` |
| no prompt anywhere | asks whether to include a credential |

**Verify-before-remove (decision 34):**

```bash
mkdir ~/test/dxt-c && cd ~/test/dxt-c && dax run         # throwaway again; exit
cd ~ && dax env archive                        # pick dxt-c; managed; remove = yes
```

| Check | Pass |
|---|---|
| remove run | archives, reports it read the archive back, then removes `dxt-c` (dir, tree, registry entry) |

The corrupt-archive case (removal cancelled) is covered by the automated
suite only; faking a corrupt write on the host isn't worth the setup.

---

## Step 3 — merged `dax env restore`

```bash
cd ~ && dax env restore --explain | head -20
```

| Check | Pass |
|---|---|
| `restore --explain` | states the empty-folder rule, that the env is named after the folder, and that credentials are never restored |

**Refusals first, all with nothing touched:**

```bash
mkdir ~/test/dxt-full && echo x > ~/test/dxt-full/f && cd ~/test/dxt-full && dax env restore   # non-empty
cd ~/test/dxt-a && dax env restore                                                 # registered
mkdir ~/test/dxt-a2 && cd ~/test/dxt-a2 && touch .DS_Store && dax env restore dxt-a     # .DS_Store only → allowed; Ctrl-C at confirm
```

| Check | Pass |
|---|---|
| `~/test/dxt-full` | refused, says not empty; `~/.dax.yaml` unchanged (`diff ~/.dax.yaml ~/.dax.yaml.bak` shows only your scratch edits) |
| `~/test/dxt-a` | refused as already registered as project `dxt-a` (and not empty), with the `dax env destroy` rollback hint; nothing touched |
| `~/test/dxt-a2` with only `.DS_Store` | reaches the summary (not refused); Ctrl-C at the confirm leaves nothing registered |

**Renamed restore of the Claude env (decisions 26–29, 33):**

```bash
mkdir ~/test/dxt-b2 && cd ~/test/dxt-b2 && dax env restore
#   managed → dxt-b → newest date → read the summary → confirm
```

| Check | Pass |
|---|---|
| summary | archived name `dxt-b` → new name `dxt-b2`, new dir `~/test/dxt-b2`, tenant `dxt` |
| registry | `dxt-b2` registered; `dxt-b` untouched |
| `.claude.json` key | see command below: prints `/home/dfarrow/dxt-b2` and **not** `dxt-b` |
| `history.jsonl` | see command below: every count is under `dxt-b2` |
| `projects/` | `ls ~/.local/state/dax/tenants/dxt/dxt-b2/projects/` ends in `-dxt-b2` |
| no credential | `ls ~/.local/state/dax/tenants/dxt/dxt-b2/.credentials.json` → no such file |

```bash
python3 -c "import json;print(list(json.load(open('$HOME/.local/state/dax/tenants/dxt/dxt-b2/.claude.json'))['projects']))"
python3 -c "import json,collections;print(collections.Counter(json.loads(l).get('project') for l in open('$HOME/.local/state/dax/tenants/dxt/dxt-b2/history.jsonl')))"
```

**Then launch it (login):** `cd ~/test/dxt-b2 && dax run`. This needs a fresh login
(mints a second grant). Inside: `claude`, then `/resume`.

| Check | Pass |
|---|---|
| login | the browser login runs for `claude-dxt-dxt-b2` |
| `/resume` | lists the "say hi" session from the fixture; **select it and send "what did I say?"**: it answers from the old session, with no error or path warning (settles decision 28's open question about transcripts) |
| trust prompt | does **not** ask to trust the folder again |
| up-arrow in the prompt | recalls "say hi" |

**Older archive and shortcuts (decision 27):** run `dax env archive` on
`dxt-a` once more (same day, so it overwrites). To get a second *date*,
copy the file and rename it with an earlier date:

```bash
cd ~/test/dax-test-backups/daily && cp dxt-a-*.tar.gz dxt-a-2026-01-01.tar.gz
mkdir ~/test/dxt-a3 && cd ~/test/dxt-a3 && dax env restore dxt-a   # shortcut → date list
#   pick 2026-01-01 → confirm
mkdir ~/test/dxt-a4 && cd ~/test/dxt-a4 && dax env restore ~/test/dax-test-backups/one-off.tar.gz
```

| Check | Pass |
|---|---|
| `restore dxt-a` | jumps straight to dates; shows both, newest highlighted; the older one restores |
| `restore <path>` | jumps straight to the summary |

**Backward compatibility with a real old archive (the decision 23 proof):**

```bash
ls <real backup_dir>/daily/ | head      # the OneDrive path noted in setup
mkdir ~/test/dxt-old && cd ~/test/dxt-old && dax env restore '<real backup_dir>/daily/<some-env>-<date>.tar.gz'
#   confirm → then immediately: cd ~ && dax env destroy dxt-old
```

| Check | Pass |
|---|---|
| restore | succeeds from a `dax-export-manifest.json` archive; registered as `dxt-old` |
| if that archive lists a feature that doesn't exist | the summary says kept commented out, and `~/.dax.yaml` shows `#- <name>` under `dxt-old` (`claude` and `claude_tenant_state` are still valid features at this step, so they restore active) |
| if it listed an explicit Claude credential | the summary shows the swap to bare `claude` |
| OneDrive file | unchanged (read-only use) |

---

## Step 4 — `_bootstrap_env` replaces `dax init` / `dax process new`

```bash
mkdir ~/test/dxt-d && cd ~/test/dxt-d && dax run        # tenant dxt, no creds, coding branch;
                                               # answer n to the login prompt; exit
mkdir ~/test/dxt-e && cd ~/test/dxt-e && dax run        # substrate branch (needs your substrate path)
mkdir ~/test/dxt-d/sub && cd ~/test/dxt-d/sub && dax run    # nested
mkdir -p ~/test/tmp-x/dxt-d && cd ~/test/tmp-x/dxt-d && dax run   # name clash
mkdir ~/test/dxt-g && cd ~/test/dxt-g && dax run        # at the final confirm, answer No
dax init ; dax process --help
```

For the substrate branch, the substrate path is the `substrate:` value of
any existing substrate env (`dax env show diary`). Use tenant `personal`.

| Check | Pass |
|---|---|
| `dxt-d` | wizard asks image → tenant → existing credentials → coding vs. substrate; summary table; then the container launches with no second command |
| `dxt-e` | asks type (from `process-types.tsv`), title, git; `context.md` scaffolded in `~/test/dxt-e`; launches |
| bad title/type answer | re-asks the same question, doesn't exit |
| `~/test/dxt-d/sub` | exits immediately naming `dxt-d` and its root; nothing registered |
| `~/test/tmp-x/dxt-d` | exits naming the existing `dxt-d` and its dir; `~/.dax.yaml` unchanged |
| `dxt-g`, declined | `dax run` ends: no container launches, nothing in `~/.dax.yaml` |
| `dax init`, `dax process` | both are argparse errors (gone) |
| `PYTHONUSERBASE="$HOME/Library/Python/3.9" HOME=$(mktemp -d) dax envs list` (a fake home with no `~/.dax.yaml`; `PYTHONUSERBASE` keeps Python finding the per-user `dax` install, which it otherwise looks for under `HOME`; `env list` arrives in step 8) | exits printing the minimal `~/.dax.yaml` to create; never mentions `dax init`; creates nothing (decision 37) |
| tenant | always asked and required; existing tenants offered, a new one can be typed |
| credentials | checkbox shows only existing non-Claude credentials; no option to define one; no Claude question |

---

## Step 5 — wizard keypress behavior (only reachable from a real terminal)

Use any confirm, e.g. `cd ~/test/dxt-a && dax env archive` → at "Proceed?":

| Check | Pass |
|---|---|
| press `y` without Enter | nothing happens until Enter |

---

## Step 7 — Claude baseline; old `claude` retired; copy-once seed; login temp folder

```bash
mkdir ~/test/dxt-f && cd ~/test/dxt-f && dax run     # wizard: tenant dxt, no other creds;
                                           # answer n to the login prompt; exit
grep -A6 'dxt-f:' ~/.dax.yaml              # no claude in features: or creds:
ls ~/.local/state/dax/tenants/dxt/dxt-f/CLAUDE.md

ls -l ~/.claude/.credentials.json 2>/dev/null   # note mtime (or "absent")
dax creds login claude-dxt-dxt-f                # (login) real login, mints a grant
ls -l ~/.claude/.credentials.json 2>/dev/null   # mtime must equal the line above
ls -A ~/.local/state/dax/login/                 # must be empty
dax creds login claude-dxt-dxt-f                # close the browser tab, then Ctrl-C
                                                # (claude auth login waits for a code)
ls -A ~/.local/state/dax/login/                 # still empty
```

| Check | Pass |
|---|---|
| `dxt-f`'s entry | no `claude` in `features:` or `creds:`, yet it got a state tree and `CLAUDE.md` (same size as `~/.claude/CLAUDE.md`), and was offered `claude-dxt-dxt-f` at launch |
| `dax creds login claude-dxt-dxt-f` | accepted, though `creds:` never names it (the implied credential is visible to login); "token imported to Keychain"; host `~/.claude/.credentials.json` mtime unchanged; `login/` empty |
| abandoned login | nothing imported (says so); `login/` empty |
| `dax creds list` | has a `claude-dxt-dxt-f` row |
| `cd ~/test/dxt-f && dax run`, then `claude` | starts logged in (the wrapper injected the new grant from Keychain) |
| an existing real env's tree, e.g. `ls -l ~/.local/state/dax/tenants/personal/fabric/CLAUDE.md` | mtime unchanged by any of this |
| `dax features` | `claude` marked `(always on)` alongside `workdir`/`dotfiles`/`webpreview`; no `claude_tenant_state`; `auto_claude` still listed |
| `dax -t run` in `~/test/dxt-f` | prints the docker command with the state-tree mount; never opens a browser or asks to log in |
| `cd ~/test/dxt-b && dax run` (`dxt-b` still lists `claude_tenant_state` and `creds: [claude]`) | a warning listing every env with old Claude entries: `dxt-b`; `dxt-b2`, `dxt-d` and `dxt-e`, which were written before step 7; and all 14 of your real envs (`discernment`'s old `claude` included) until you clean them up. Claude applied once (no duplicate-mount error) |
| remove those two lines from `dxt-b`, `dax run` again | `dxt-b` no longer in the warning (the real envs still are); still starts with Claude logged in |
| edit `dxt-a` to remove its `tenant:`, `dax run` there | refused before launch with "env 'dxt-a' has no tenant…" and how to add one; put it back |

**Destroy removes the implied credential from Keychain:**

```bash
cd ~ && dax env destroy dxt-f                                  # type DESTROY
python3 -c "import keyring; print(keyring.get_password('dax-creds', 'claude-dxt-dxt-f'))"   # must print None
```

---

## Step 8 — `envs` / `tenant` / `tenants` removed

```bash
dax env list
cat ~/test/dax-test-backups/envs-before.txt
dax envs ; dax tenant ; dax tenants
```

| Check | Pass |
|---|---|
| `env list` vs. `envs-before.txt` | the same real envs with the same NAME, TENANT and DIR columns; extra `dxt-*` rows are expected. STATE/USED sizes and ages, and running markers, will legitimately differ, so don't diff the whole output |
| `envs` / `tenant` / `tenants` | argparse errors (gone) |

---

## Step 8b — `dax config`

```bash
dax config                     # change nothing, quit the editor
ls -l ~/.dax.yaml ~/.dax.yaml.prev
dax config                     # add a line `  bogus: 1` under dxt-a, save, quit
dax config                     # break the YAML (e.g. delete a colon), save, quit
```

| Check | Pass |
|---|---|
| first run, no change | opens your editor on `~/.dax.yaml`; says "no changes" |
| `bogus: 1` | after quitting: "saved", `~/.dax.yaml.prev` now holds the version without it, and a warning names `dxt-a` and `bogus`; then remove the line with `dax config` again |
| broken YAML | after quitting: the parse error is printed and you're offered the editor again; fix it there. `~/.dax.yaml.prev` holds the version from before that edit if needed |
| `dax config` again, quit without changing anything | "no changes"; `~/.dax.yaml.prev` is untouched (opening the file just to look never costs the undo copy) |
| `dax config` from a shell **inside** a dax container | refuses immediately with "only runs on the host"; no editor opens. The container's own `dax` is the image's frozen copy, so before the end-of-branch rebuild run the branch's code directly: `python3 ~/dax/dax.py config` |

## Step 8c — auggie removed

```bash
dax features | grep -i auggie            # no output
dax creds add                            # the provider list has no auggie; Ctrl-C out
cd <ys-augmentcode's folder> && dax -t run
```

| Check | Pass |
|---|---|
| `dax features` | no `auggie` |
| `dax creds add` | auggie isn't offered as a provider |
| `dax -t run` in `ys-augmentcode` | no "unknown feature: auggie" error; no `~/.augment` mount in the docker command; a warning names `ys-augmentcode` and `auggie` (features and creds) |
| after removing `auggie` from its `features:` and `creds:` (`dax config`) | the auggie warning is gone |
| `dax creds remove auggie` | clears the Keychain secret and registry entry (optional; your call) |

## Step 8d — `mounts:` switches its feature on; one global substrate

**Before migrating**, see what today's config gets:

```bash
cd ~/test/dxt-e && dax -t run
```

| Check | Pass |
|---|---|
| `dax -t run` in `dxt-e` (it has a per-env `substrate:`, no top-level one) | refused before launch; names its per-env path and says to move it to a top-level `substrate:` |
| the launch warning | calls `substrate` a retired key and says to *move* its path to a top-level `substrate:`, not just remove it |

Then migrate with `dax config`:
- add a top-level line: `substrate: /Users/dfarrow/fatsec/discernment`;
- remove the per-env `substrate:` lines (leave one for now, to see the
  warning below);
- under `dxt-a`, add `mounts: [~/test]` and remove `mounts` from its
  `features:` if present;

```bash
cd ~/test/dxt-a && dax -t run
cd ~/test/dxt-e && dax -t run
dax env show dxt-e
```

| Check | Pass |
|---|---|
| `dax -t run` in `dxt-a` | the docker command mounts `~/test`; no `mounts` in `features:` needed |
| `mounts` left in some env's `features:` | each `dax run` warns it's no longer used and to remove it; nothing is mounted twice |
| `dax -t run` in `dxt-e` (`features: [substrate]`, no per-env path) | mounts `/Users/dfarrow/fatsec/discernment` and sets `SUBSTRATE_ROOT` |
| an env still carrying a per-env `substrate:` line | the launch warning still names it; it launches with the global path; remove the line |
| `dax env show dxt-e` | shows the global substrate path |
| temporarily remove the top-level `substrate:`, `dax -t run` in `dxt-e` | refused before launch, saying to add `substrate: <path>` via `dax config`; put it back |
| new substrate env: `mkdir ~/test/dxt-i && cd ~/test/dxt-i && dax run` (process branch) | doesn't ask for a substrate path (the global one is used); decline at the confirm, then `rm -r ~/test/dxt-i` |

## Step 6 — docs sweep (run last, after step 8)

Step 6 was implemented after 8d (execution order 1, 2, 3, 4, 5, 7, 8, 8b, 8c, 8d, 6, 9),
so its checks come here.

```bash
dax --help ; dax env --help ; dax backup --explain | head
```

| Check | Pass |
|---|---|
| top level | exactly `build run features backup creds env config` |
| `backup --explain` | points at `dax env archive`, not `dax process archive` |
| any error or hint message you saw during the sequence | none named a removed command (`dax env set`, `dax process`, `dax init`, `dax envs`, `dax tenant`) |
| `README.md` | skim: no removed command names, and Claude described as always-on |

---

## Step 9 — versioning

Run these before the end-of-branch rebuild: the current image has no
version label, which is what the warning check needs.

```bash
dax --version
cd ~/test/dxt-a && dax -t run | head
```

| Check | Pass |
|---|---|
| `dax --version` | prints `dax 3.0.0` |
| `dax -t run` against the old image | prints `image dax:latest predates dax versioning; this is dax 3.0.0. Run \`dax build\` to update it.` |

The rest of step 9 is checked after the rebuild, in the end-of-branch
table below.

---

## End of branch

```bash
cd ~/fatsec/dax && dax build
cd <fabric's dir> && dax run           # a real env, plain restart; exit
```

| Check | Pass |
|---|---|
| build | succeeds |
| `docker image inspect --format '{{ index .Config.Labels "dax.version" }}' dax:latest` | prints `3.0.0` |
| `cd ~/test/dxt-a && dax -t run` | no `predates dax versioning` or `was built by` warning |
| in a freshly started container, `dax --version` | prints `dax 3.0.0` |
| in a freshly started container, `dax config` | refuses with "only runs on the host" (the rebuilt image's own `dax` now has the command) |
| `fabric` | launches, `claude` starts logged in, `/resume` shows its history |
| full pass on a fresh scratch env | `mkdir ~/test/dxt-z && cd ~/test/dxt-z && dax run` → `dax env archive` → `mkdir ~/test/dxt-z2 && cd ~/test/dxt-z2 && dax env restore dxt-z` → `dax env destroy dxt-z dxt-z2` |

---

## Cleanup (after the last step, or after abandoning)

```bash
cd ~ && dax env list | grep dxt                 # every scratch env
dax env destroy <each dxt-* name>               # dirs, trees, derived creds, registry
rm -rf ~/.local/state/dax/tenants/dxt            # safety net for any orphaned tree
ls -A ~/.local/state/dax/login/                  # must be empty
for n in $(dax creds list | awk '/dxt/{print $1}'); do echo "left: $n"; done   # no output
for n in claude-dxt-dxt-a claude-dxt-dxt-b claude-dxt-dxt-b2 claude-dxt-dxt-c claude-dxt-dxt-d \
         claude-dxt-dxt-f claude-personal-dxt-e claude-personal-dxt-old; do
  python3 -c "import keyring,sys; v=keyring.get_password('dax-creds', sys.argv[1]); v and print('left:', sys.argv[1])" $n
done                                                                                   # no output: no dxt Keychain entries left
#    edit ~/.dax.yaml: restore backup_dir to the real value from setup
diff ~/.dax.yaml ~/.dax.yaml.bak                 # no output: config back to its pre-test state
rm -rf ~/test/dax-test-backups ~/test/tmp-x ~/test/dxt-full ~/test/dxt-a2 ~/test/dxt-g   # folders never registered
rm -f ~/.local/state/dax/creds-login/claude-dxt-*.json         # relogin timestamps
ls -d ~/test/dxt-* 2>/dev/null                                       # no output
```

`dxt-e` used tenant `personal`; `destroy` removes its tree. Destroyed
grants are removed from Keychain but not revoked with Anthropic. That's
expected, and the reason fixtures mint as few as possible (see the login
note at the top).

---

## What this sequence can't reach

- **Concurrent-grant limits.** Whether Anthropic caps live grants per
  account is still untested (see the relogin entry in `CLAUDE.md`). This
  sequence mints a handful; if a login fails with a limit-shaped error,
  that's a finding, not a regression.
- **Type-ahead between prompts** (keys typed while dax is busy must not
  answer the next prompt). Too fast to hit by hand; covered by unit tests
  showing every prompt helper flushes stdin first and that the flush calls
  `termios.tcflush`. Not proven on a real macOS terminal, by choice.
- **The corrupt-archive path of decision 34**, and the "archive contained
  a credential" warning of decision 29 (it needs a hand-built old-style
  archive), are automated-suite only.
- **Long-run behavior** (refresh rotation over days) is out of scope; see
  the cred-management sequence's soak step.
