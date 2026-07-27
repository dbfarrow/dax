# Tenant Isolation for Claude Code State

**Status:** Sequencing steps 1–2 implemented and merged (PR #4, 2026-07-27);
steps 3–7 designed, not implemented
**Date:** 2026-07-27
**Motivation:** hygiene today; anticipated contractual obligation later; existing
obligation to delete customer data when a relationship ends

## Problem

`~/.dax.yaml` sets `workdir.container: work`, so every project — whatever its host
path — mounts at `/home/dfarrow/work` inside the container. Claude Code keys its
per-project state on the *container* cwd. Every project therefore resolves to one
identity: `-home-dfarrow-work`.

Combined with `~/.claude` and `~/.claude.json` being bind-mounted read-write into
every container, all projects for all customers share one pool of state.

Observed on 2026-07-27:

- `~/.claude/projects/-home-dfarrow-work/` held 31 session transcripts and 27
  memory files spanning at least six distinct engagements — `dax`,
  `project_ysecurity_comp_model`, `project_hydraulic_controls_it`,
  `project_seans_new_gig_context`, `project_symlink_disclosure`,
  `project_bedrock_decision`.
- `~/.claude/history.jsonl` held 2066 prompt lines from all of them.
- A session opened to discuss a git branch in the `dax` repo was preloaded with
  another customer's candidate slate and a third party's compensation model. That
  is the system working as designed, on a directory that describes six
  engagements as one project.

Nothing crosses at the model level. Concurrent sessions do not share a context
window and there is no server-side state joining them. Every bleed path is
on-disk state read at session start, which means the fix is entirely a
filesystem and mount concern.

### Partial isolation that already worked

`projects/` also contained `-home-dfarrow-work-processes-augmentcode`,
`-processes-self-employment-setup`, and `-processes-a-priest-and-an-imam`.
Separate slugs, separate memory — because those sessions were started from a
subdirectory. The `cd` convention adopted for ccusage attribution was
incidentally the only working isolation mechanism in the system.

## Verified findings

Established by inspection on 2026-07-27, recorded because the design depends on
them.

**`CLAUDE_CONFIG_DIR` relocates the entire config root, including
`~/.claude.json`.** Run with a fake `HOME` and `CLAUDE_CONFIG_DIR` set, Claude
Code wrote `$CLAUDE_CONFIG_DIR/.claude.json` and
`$CLAUDE_CONFIG_DIR/backups/`; the fake `HOME` received nothing. One environment
variable is sufficient to relocate all state. Corollary: a shared `.claude.json`
alongside per-tenant state trees is not achievable through this mechanism.

**dax-creds' Claude injection is a no-op and has been since at least
2026-06-27.** The wrapper at `/usr/bin/claude` writes
`~/.claude/credentials.json`. Claude Code reads `~/.claude/.credentials.json`,
with a leading dot. On disk: `.credentials.json` was 463 bytes of valid
`claudeAiOauth` envelope; `credentials.json` was 108 bytes of invalid JSON,
apparently a bare token. Two faults — wrong filename, wrong content shape.

Consequence: container Claude auth currently rides the `~/.claude` bind mount,
which is the mount this design removes. See Sequencing.

**`~/.claude.json` is mounted separately** via `dotfiles.rw`, not via
`claudedir`. It is rewritten wholesale and frequently — five 43K backups
appeared in a twenty-minute window.

**Multi-tenancy is already declared in the config.** `projects.discernment.creds`
lists `gmail-personal`, `gmail-ysecurity`, and `gmail-augment`. The credential
list broadcast the repo's multi-tenancy before it was stated.

## Decisions

### 1. Terminology: `tenant`

Not `scope` — `.dax.yaml` already uses `scopes:` for OAuth scopes under
`credentials`. Values are customer names plus `personal`.

### 2. Mount the host repo at `~/<hostdirname>`

Replaces `workdir.container: work`. Solves the ergonomic problem of not knowing
which project a container is serving, and gives distinct Claude project
identities as a side effect — which is what makes `--continue` and `/resume`
safe to use again.

### 3. State lives at `~/.local/state/dax/tenants/<tenant>/<project>/`

Selected per session via `CLAUDE_CONFIG_DIR`. The tenant directory is the
deletion boundary; the project directory beneath it is the isolation and
concurrency unit.

Deliberately not `~/.config/dax/`. What is stored is state, not configuration —
transcripts, memory, history, paste cache — and it grows without bound.
`~/.config` is also what dotfile managers and `dax backup` sweep, and customer
transcripts landing in a synced repo is a bad interaction with a deletion
obligation. `~/.config/dax/` remains available for genuine dax configuration.

### 4. Tenant is the outer key, not project

Deletion is the deciding constraint. Ending an engagement must be one verifiable
operation on one tree, not a hunt across N project directories where correctness
depends on remembering which projects belonged to whom.

Per-project separation still happens automatically inside a tenant tree, via
Claude Code's own cwd keying, at no cost.

### 5. Cross-project recall within a tenant does not happen

Memory stays keyed by cwd, so two repos for the same customer have separate
memory directories. This is intended. The problem being solved is context
arriving unbidden; re-introducing that within a tenant would recreate the same
failure at smaller blast radius.

This rule is why state is per tenant/project rather than per tenant. Arrow-up
history is recall too, and a tenant-wide `history.jsonl` would have contradicted
this decision — one customer project surfacing another's prompts.

Where standing customer context is genuinely wanted, it belongs in a curated
tenant-level `CLAUDE.md` — explicit and reviewable — not emergent recall.

### 6. Two gates, only one of which refuses

| | Gate A: `dax run` | Gate B: `claude` launch |
| --- | --- | --- |
| Where | host, container start | in container, `/usr/bin/claude` wrapper |
| Sees | the repo | the **cwd** |
| Job | mount repo at `~/<reponame>` | resolve cwd → tenant → `CLAUDE_CONFIG_DIR` |
| Refuses | never | when resolution yields no tenant |

Gate A always succeeds. A multi-tenant repo's root is a legitimate work
directory — git, builds, and moving files between processes all need it.

Gate B refusal is not a bolted-on check. The wrapper must resolve cwd → tenant
regardless, because that is how it learns which `CLAUDE_CONFIG_DIR` to export.
Refusal is the empty-resolution branch of work it performs anyway.

### 7. Resolution table

| Situation | Result |
| --- | --- |
| single-tenant repo | starts anywhere in tree |
| multi-tenant repo, mapped subdir | starts, state → that tenant |
| multi-tenant repo, root or unmapped subdir | refuse |
| undeclared repo | `unattributed/<reponame>/`, warn, proceed |
| project creds span multiple tenant labels | refuse regardless |

Guiding principle: **fail toward over-isolation.** Where tenancy is ambiguous,
pick the narrower tenant. Over-isolation costs convenience; under-isolation
costs confidentiality. Those are not symmetric, and the 27-file pile is what
under-isolation looks like after a few months.

### 8. Undeclared repos go to `unattributed/<reponame>/`

Not to a tenant derived from the repo name. Repo-name derivation partitions
correctly — tested against `dax`, `fabric`, `iandidit`, and `ys-augmentcode`, it
never merges two tenants, only over-isolates. But as an *identifier* it is a
guess, and a tree labelled `ys-augmentcode` does not announce itself as
unverified at deletion time. Confident-wrong is worse than visibly-unknown.

`unattributed/<reponame>/` gives per-repo separation (nothing pools), does not
block work, is enumerable via `dax tenants`, and makes later attribution a
directory move rather than archaeology.

### 9. Declaration: gitignored `.dax-tenant` per subdir

One tenant name per file, with `.dax-tenant` in `.gitignore`.

Chosen over a central map in `~/.dax.yaml`, which does not follow a subdirectory
through a rename, and over a committed repo-root map, which would write the full
customer roster into the history of the repo containing all of their work.

Requires `dax tenant set <subdir> <tenant>`, and refusal messages that print
that command with arguments filled in.

Accepted cost: cloning a multi-tenant repo on a new machine leaves every subdir
unlabelled until relabelled. Arguably correct — forced re-attribution is the
right default when the point is knowing whose data is where.

Accepted cost: the file puts a customer name in plaintext inside a subdirectory
whose process name deliberately does not reveal one. It stays out of git
history, but it is new plaintext on disk.

### 10. Global `CLAUDE.md` stays global and general

With an enforced rule that no project- or customer-specific content ever lands
in it.

The same rule extends to discernment's telos and process skills. That layer is a
deliberate shared spine: every process subdir reads it, and working a customer's
process refines it, so there is an intentional write path from a tenant-scoped
session into material that reaches every other tenant. **The telos is a bleed
channel by design.** Isolation is the wrong control there; hygiene rules are the
right one.

This discipline is already being practised by hand —
`feedback_interview_grounding_discipline` and `feedback_billing_lumpiness` both
generalised out of specific engagements with no customer detail retained. It is
unenforced convention, not an enforced rule.

### 11. Shared vs isolated

Shared into every tenant: `plugins/`, `skills/`, `commands/`, update and
experiment caches.

Isolated per tenant/project: `projects/` (transcripts and memory),
`history.jsonl`, `paste-cache/`, `file-history/`, `backups/`, `sessions/`,
`session-env/`, `tasks/`, and `.claude.json`.

History and paste cache are **persistent, not per dax session** — they survive
container restarts — but they are scoped to the project, not shared across a
tenant's projects. The requirement was durability across restarts, not sharing
between projects; those are separable and only durability was wanted.

Practical effect: arrow-up history contains only this project's prompts, and
`/resume` lists only this project's sessions.

Caveat on `skills/`: it is shared because skills are tooling rather than content.
The moment a customer-specific skill is written, that stops being true, and rule
10 must cover `skills/` too.

### 12. Credential files are never mounted

Credentials come from the dax-creds daemon. Requires the auth bug fixed first.

## Accepted costs of per-tenant/project `.claude.json`

Forced by finding 1 rather than chosen — `CLAUDE_CONFIG_DIR` relocates
`.claude.json` along with everything else, so its granularity is whatever the
config dir's granularity is. Every loss is re-consent or cosmetic friction on
first launch in a new tenant/project; nothing functional is lost. The costs
multiply by project rather than by tenant, which the seed template below is what
makes tolerable.

| Lost | Effect |
| --- | --- |
| `hasCompletedOnboarding`, `hasTrustDialogAccepted`, `hasClaudeMdExternalIncludesApproved` | dialogs re-run per tenant/project |
| `tipsHistory` (40), `tipLifetimeShownCounts` (36), upsell counters, `lastReleaseNotesSeen` | tips and notices replay |
| `cachedGrowthBookFeatures` (441 entries) | re-fetched per tree; slight first-run latency |
| `numStartups` (113), `skillUsage`, `promptQueueUseCount` | counters fragment; beginner tips may reappear |
| `enabledMcpjsonServers`, `claudeAiMcpEverConnected` | MCP approvals per tree — arguably correct |
| `migrationVersion`, `opusProMigrationComplete` | may re-evaluate per tree; assumed idempotent, **unverified** |
| `allowedTools` | fragments per tree |

Mitigation, and it is load-bearing at this granularity: seed each new tree from a
template `.claude.json` holding only UX, onboarding, and trust flags and no
`projects{}` block. That reduces most of the above to zero. Without it, every new
project starts with a round of dialogs.

Gained: `exampleFiles` caches project filenames, so customer filenames never
pool. Deletion takes the file with the tree. And no concurrent writers in the
common case — see below.

## Concurrency

Per tenant/project, the ordinary case has **no contention at all**. Two
containers working two projects for the same customer get two config dirs, so
nothing is shared: not `.claude.json`, not `history.jsonl`, not the caches.

Contention returns only when two containers run against the *same* project for
the same tenant — two terminals on one repo. There:

| State | Keyed by | Risk |
| --- | --- | --- |
| `projects/<slug>/*.jsonl` transcripts | session id | none |
| `projects/<slug>/memory/` | filename per fact | last writer wins on the same file |
| `paste-cache/`, `backups/` | per-item filenames | none |
| `sessions/`, `session-env/`, `tasks/`, `file-history/` | per session id | none *(assumed, not verified)* |
| `history.jsonl` | appended | interleaving; grpcfuse append atomicity unguaranteed, so torn lines possible |
| `.claude.json` | single file, full rewrite | lost updates, last writer wins |

That residual case is the one where sharing is *wanted* — same project, same
work, so a shared history is a feature rather than a leak. The tradeoff lines up
with the grain of the work instead of against it.

`.claude.json` lost updates mean a permission granted in one session can be
silently dropped when the other writes, and tip counters bounce. Annoying, not
corrupting, recoverable by re-granting, with rolling backups as a floor.

**Strictly better than the status quo**, which is every container across every
tenant writing one `.claude.json` and one `history.jsonl`. This narrows the
concurrent-writer set from "all containers" to "containers on the same project."
The defect is pre-existing and upstream; its blast radius shrinks to near
nothing.

Also improved: `.claude.json` stops being a single-file grpcfuse bind mount and
becomes a file inside a directory mount, which is the configuration where
write-temp-plus-rename works. Single-file grpcfuse mounts are what break git's
atomic rename on this setup.

## Sequencing

1. **Fix the dax-creds Claude wrapper** — correct filename `.credentials.json`
   and the `{"claudeAiOauth": {...}}` envelope.
2. **Verify auth works with `~/.claude` unmounted.** Non-negotiable before step 3.
   Reversed, every container loses Claude simultaneously. Then keep watching for
   several days — see the refresh-token rotation gap under Known limits. A pass
   on day one does not clear that risk.
3. Change `workdir.container` to the host directory basename.
4. **Seed template for new trees.** A template `.claude.json` holding only UX,
   onboarding, and trust flags — no `projects{}` block — copied into each tree on
   first use. Must land before step 5, not after: at tenant/project granularity
   every new project would otherwise open with a round of onboarding and trust
   dialogs, which is the difference between the cutover being unnoticeable and
   being irritating enough to abandon.
5. Replace the wholesale `~/.claude` mount and the `~/.claude.json` dotfile mount
   with per-tenant/project trees under `~/.local/state/dax/tenants/`, plus shared
   mounts for `plugins/`, `skills/`, `commands/`. For a multi-tenant repo, mount
   only `tenants/*/<project>/` — the repo's own subtree from each tenant the
   subdir map references, never a whole tenant tree.
6. Tenant resolution and refusal in the `claude` wrapper; `dax tenant set`;
   `dax tenants` enumeration.
7. Credential-span escalation check.

## Verification log — paused 2026-07-27

### Done

Steps 1 and 2 of Sequencing, plus the code they require. Merged via PR #4 into
`master` on 2026-07-27 (commit `1c54050`).

- `dax_creds/providers/claude.py` — `.credentials.json` default, legacy name as
  import fallback, `is_oauth_envelope()` requiring a `refreshToken`, `store()`
  refuses anything else.
- `dax_creds/wrappers/claude` — writes `.credentials.json` under `umask 077`,
  validates shape, reports failures on stderr instead of swallowing them, still
  execs Claude so a bad credential cannot lock you out. `DAX_CLAUDE_REAL`
  override added for testability.
- `dax_creds/init.py` — corrected the path in the import message.
- `tests/test_dax_creds_claude.py` — tests that exercise *defaults*, which is the
  gap that let the original bug ship.
- `tests/test_dax_creds_claude_wrapper.py` — new; the wrapper had no coverage.

Added after the onboarding diagnosis below:

- `dax_creds/wrappers/claude` — seeds `{"hasCompletedOnboarding": true}` into
  `.claude.json` when absent, create-if-absent only, and now honours
  `CLAUDE_CONFIG_DIR` for both the credential and the state file. Folder trust is
  deliberately **not** seeded.
- `tests/test_dax_creds_claude_wrapper.py` — 9 more tests covering the seed
  (created, never overwritten, not overwritten when zero-byte, written even with
  no credential configured, owner-only, no trust key) and the `CLAUDE_CONFIG_DIR`
  layout split.
- `tests/manual/interactive_launch_probe.py` — new; drives the interactive launch
  path under a pty, which is the blind spot that made this bug hard to see.
- 149 tests pass.

Confirmed working in a container: the wrapper correctly rejected the 108-byte
bare token with the expected message, and `dax creds remove` + `dax creds add`
re-imported a full envelope from `~/.claude/.credentials.json` into Keychain.

### Resolved 2026-07-27 — the login prompt was onboarding, not auth

**Root cause: Claude Code's interactive first-run wizard is gated on
`.claude.json`, and it runs *ahead of* the credential check.** With
`~/.claude.json` unmounted, a launch shows the theme picker, then **"Select login
method"**, then opens a browser — while a perfectly valid `.credentials.json` sits
unread beside it. The credential was landing correctly the whole time.

Reproduced deterministically: a config dir containing *only* a valid
`.credentials.json`, launched interactively under a pty, walks theme picker →
`Select login method` → `Opening browser to sign in…`. The same directory in
headless `-p` mode authenticates and answers normally.

**Why the earlier experiment ruled this out incorrectly.** `claude auth status`
does not exercise the launch path — it reads the credential directly. So does
`-p`. Both report `loggedIn: true` against a config dir that would prompt for
login the moment it is started interactively. The prior test could not observe
the failure mode it was aiming at. Any future gate on this must be an
**interactive** launch.

**Both prior hypotheses are disproven for this incident:**

- *Refresh-token rotation* — no. `dax-creds get claude-fre` and the container's
  `~/.claude/.credentials.json` hash identical on `accessToken`, `refreshToken`,
  and `expiresAt`. Nothing rotated. (The rotation gap under Known limits remains
  real and still unverified; it just was not what happened here.)
- *Daemon cache serving the stale bare token* — no. The daemon returns a
  well-formed `{"claudeAiOauth": {...}}` envelope with a `refreshToken`, and no
  `dax:` error lines were produced.

Also confirmed healthy during diagnosis, so none of these need re-testing:
`DAX_CREDS_SOCK` and `DAX_CREDS_CLAUDE` are exported, the socket is live, the
installed `/usr/bin/claude` is the fixed wrapper, and it wrote a valid
dot-prefixed `.credentials.json` at container start.

**Not a factor: dropping `claude` from `features`.** `feature_claude` (dax.py:119)
only mounts `~/.claude`. The credential daemon wiring (dax.py:452-467) keys off
the project's `creds` list and is independent of `features`. Removing the feature
disables the mount without touching credential delivery — which is exactly what
the mount change needs.

### Consequence for the design: the wrapper must seed `.claude.json`

Sequencing step 2 is not "stop mounting `~/.claude`". It is "stop mounting
`~/.claude` **and** seed a minimal `.claude.json` into the per-tenant config
dir" — otherwise every newly created tenant/project tree drops the user into
onboarding and a browser OAuth round trip.

One key is sufficient to suppress the login prompt:

```json
{"hasCompletedOnboarding": true}
```

Verified: with that plus `.credentials.json`, Claude boots to `Welcome back
Dave!` and is authenticated. For a zero-prompt launch, seed:

| Key | Effect if absent |
| --- | --- |
| `hasCompletedOnboarding` | theme picker, then **login prompt**, then browser OAuth |
| `projects.<cwd>.hasTrustDialogAccepted` | trust dialog; `settings.local.json` permissions silently ignored |
| `theme` | theme picker on first launch |
| `oauthAccount`, `userID` | cosmetic only — banner omits org name; triggers a profile fetch |

`oauthAccount` is **not** required for auth. It is copied host state, so whether
to seed it is a tenant-isolation question in its own right: it carries
`emailAddress`, `organizationName`, and `accountUuid` into every tenant tree.
Since the account is the same across tenants either way, seeding it leaks nothing
new — but it should be a deliberate choice, not a copy-everything default.

The trust key is the one most likely to be missed. Its failure is silent: the
session starts fine and simply ignores `.claude/settings.local.json` with a
single line of warning text.

#### Verified mechanics of the seed

All observed against Claude Code **v2.1.220** — this is launch-flow behaviour and
is not contractual, so re-check it after major version bumps.

- **`CLAUDE_CONFIG_DIR` relocates `.claude.json` too.** It lands at
  `$CLAUDE_CONFIG_DIR/.claude.json`, not `~/.claude.json`. One variable moves both
  the credential and the config, which is what makes the per-tenant state tree in
  decision 3 workable without additional plumbing.
- **The seed must be create-if-absent, never rewritten.** A 33-byte seed becomes
  ~32 KB and 23 top-level keys after a *single* run. Claude Code treats this file
  as its own mutable state; a wrapper that rewrites it each launch would destroy
  per-tenant history, permissions, and project entries on every start.
- **`projects` is keyed by absolute path**, e.g. `/home/dfarrow/work`. Under
  decision 2 the repo mounts at `~/<hostdirname>`, so the seeded trust key must be
  built from that mount path and differs per project. A hardcoded path silently
  produces the ignored-permissions warning.
- **`oauthAccount` is fetched over the network if absent.** It appeared in the
  file after one run without being seeded. Seeding it saves a profile fetch;
  omitting it costs nothing but a round trip and the org name in the first banner.

### Environment state

`~/.dax.yaml` is **deliberately left unrestored**: `claude` stays out of
`features`, `~/.claude.json` stays out of `dotfiles.rw`. These were originally
just the diagnostic session's test edits, but since the fix merged with them
still in place, every container started since — including the one this design
work is happening in — has been running the merged wrapper with `~/.claude`
genuinely unmounted. That's an unplanned, ongoing run of Sequencing step 2's
gate; see Confirmed below. Restore only if a dax session is needed before steps
3–7 land — the intent is to finish the feature first and never need to.

The stale `~/.claude/credentials.json` / `~/.claude/credentials.json-` bare
tokens noted earlier are gone from this container's `~/.claude` — no longer
worth tracking as cleanup.

### Confirmed 2026-07-27 — step 2 passed, live and unplanned

Checked directly in a running session: no `claude` entry in `mount`;
`/usr/bin/claude` is the fixed wrapper; `DAX_CREDS_CLAUDE=claude-fre` and
`DAX_CREDS_SOCK` are set; `~/.claude.json` shows `hasCompletedOnboarding: true`
with exactly one project key (`/home/dfarrow/work`) — clean, no cross-tenant
data; `~/.claude` holds only a fresh `.credentials.json` written by the
wrapper, no bind-mounted host files. The session launched and ran normally
throughout — no theme picker, no login prompt.

This is the same pass condition `tests/manual/interactive_launch_probe.py`
checks for (`Welcome back`, no login screen), observed directly rather than via
the probe:

```
python3 tests/manual/interactive_launch_probe.py          # bare credential
python3 tests/manual/interactive_launch_probe.py --seed   # with onboarding flag
```

It probes an isolated temp config dir rather than the live one, so it remains
safe to run against a working session if a second opinion is wanted; verified
to produce FAIL without `--seed` and PASS with it.

**Still open: rotation.** This confirmation is hours old, not the "several
days" Known Limits calls for before trusting refresh-token rotation under an
unmounted `~/.claude`. Leaving `~/.dax.yaml` unrestored (above) keeps extending
this observation window for as long as steps 3–7 take to build.

## Deferred

- **Migrating the existing pile** — 27 memory files and 31 transcripts to correct
  tenants. Cheaper than it first appears: the naming convention already encodes
  the split, with `feedback_*` and `user_*` as tenant-neutral spine material and
  `project_*` as tenant-specific. Deferred, not skipped; it is deletion-readiness
  debt that grows.
- **Splitting multi-tenant repos.** Deleting a subdirectory does not remove it
  from git history. Should deletion become contractual, multi-tenant repos are
  what blocks an attestation, and the fix is per-tenant repos — submodules or
  siblings — not anything in dax. Submodules give independent history with one
  checkout at the cost of the usual friction; sibling repos give the same history
  independence without it.
- **Per-tenant permissions** beyond what per-tenant `.claude.json` provides
  incidentally.
- **Credential/tenant consistency check** as a warning: flag `tenant: personal`
  sitting next to `gmail-ysecurity`. The data already exists.

## Known limits

**This isolates state, not filesystem reach.** In a multi-tenant repo the whole
repo is mounted. Gate B controls where a session *starts*; it does not prevent an
agent from reading a sibling tenant's subdirectory mid-session. Fixing that means
per-tenant mounts or separate containers — a substantially larger change.

**A multi-tenant repo's container necessarily holds more than one tenant's state
tree.** Because the user moves between process subdirectories within one dax
session, the container must have every tenant tree the subdir map references.
Per-tenant/project granularity bounds this usefully: mount
`tenants/ysecurity/discernment/` rather than `tenants/ysecurity/`, so the
container sees only that tenant's *discernment* state and not their other
projects. Narrower than per-tenant granularity would allow, but still more than
one tenant per container.

**Deletion of Claude state is not deletion of customer data.** The deletable
surface also includes Keychain entries via dax-creds, host repository
directories, `dax backup` output, and git history. A per-tenant state tree makes
one part of that enumerable; it does not make the whole obligation
self-executing.

### Open gap: Claude refresh-token rotation

Removing the shared `~/.claude` mount breaks the mechanism that currently keeps
Claude credentials fresh, and nothing in this design replaces it.

Today every container reads *and writes* the same
`~/.claude/.credentials.json`. When Claude Code refreshes, the new credential is
immediately visible to every other container and to the host. Observed
2026-07-27: that file's mtime tracks recent activity, so in-place refresh is
demonstrably happening.

Under per-tenant/project state, that stops:

1. The daemon hands out a **snapshot** of what is in Keychain.
2. Each container refreshes into its own per-tenant `.credentials.json`.
3. Nothing propagates back to Keychain.

If the refresh token rotates on use, the Keychain copy dies at the first
rotation and every subsequent container starts from a dead credential. **Whether
Anthropic rotates Claude Code refresh tokens on use is unverified** — that is
the crux, and it determines whether this is a latent break or a non-issue.

Symptom to watch for: auth works immediately after cutover and fails days later
across all containers at once.

Two remedies, in preference order:

- **Daemon-side refresh**, mirroring `GoogleTokenExchanger`, which already holds
  a refresh token and exchanges it for access tokens for `gmail` and `drive`.
  Requires Anthropic's token endpoint and client_id. This is the pattern the
  codebase already establishes, and it keeps Keychain authoritative.
- **A `dax-creds put` action** letting a container push a rotated credential back
  to Keychain. Simpler, but inverts the trust direction — the container becomes a
  writer of credential material rather than only a reader.

Related smaller gap: there is no command to re-import an existing credential from
disk. `dax creds add` prompts for the name, warns the credential already exists,
and on overwrite re-runs `_define_credential`, so recovering a credential means
re-answering the provider and browser prompts. A `dax creds import <name>` would
be the natural home for both this and any rotation recovery.
