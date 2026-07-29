# Tenant Isolation for Claude Code State

**Status:** Sequencing steps 1–2 implemented and merged (PR #4, 2026-07-27);
steps 3–6 implemented, unit-tested (259 tests), and confirmed on the two real
target repos (2026-07-28) — see Verification log; step 7 dropped (see
Sequencing). Outstanding before this is trusted as the default: the shared
`plugins`/`skills`/`commands`/`cache` migration and the multi-day
refresh-token rotation soak.
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
| Sees | the repo | the **cwd**, plus `multi_tenant` and `project_name` handed down from Gate A |
| Job | mount repo at `~/<reponame>` | resolve cwd → tenant → `CLAUDE_CONFIG_DIR` |
| Refuses | never | when resolution yields no tenant |

Gate A always succeeds — *once it has correctly identified which registered
project it's mounting.* A multi-tenant repo's root is a legitimate work
directory — git, builds, and moving files between processes all need it.

Gate B refusal is not a bolted-on check. The wrapper must resolve cwd → tenant
regardless, because that is how it learns which `CLAUDE_CONFIG_DIR` to export.
Refusal is the empty-resolution branch of work it performs anyway.

**Gate 0, added 2026-07-28: `cmd_run`'s project lookup, ahead of Gate A.**
Found via a real usage mistake on `discernment`: its intended flow is
`dax run` once from the repo root, then `cd` to a `processes/<name>/`
subdirectory *inside* the running container for Gate B to resolve. Nothing
stopped the same `cd` happening **on the host** instead, followed by
`dax run` from there directly. `find_project_by_dir` (`dax_creds/config.py`)
only matches by exact equality against a registered `dir:`, so a subdirectory
cwd matches nothing, and `cmd_run`'s `except KeyError` branch fell through to
`run_init`, silently auto-registering the *subdirectory* as a brand-new,
unrelated project. `feature_workdir` then mounted only that subdirectory —
the true repo root, and everything that exists only there (`.git`,
`.claude/skills/`, repo-root config), was never mounted, with no error to
signal why. Worse, because the subdirectory already had its own
`.dax-tenant` (written earlier under the correct flow), the bogus new
project resolved to the *same* tenant and state directory the correct flow
would have used — credentials and history looked continuous, so the failure
had no visible symptom beyond silently-missing repo-root content.

Same principle as decisions 7/8 (no silent fallback — refuse rather than
guess), applied one layer earlier: `find_enclosing_project`
(`dax_creds/config.py`) checks whether cwd is nested *inside* — not equal
to — any already-registered project's `dir` before falling through to
`run_init`. If so, `cmd_run` refuses with the enclosing project's name and
root, and the exact command to run instead, rather than auto-registering.
`run_init` still fires exactly as before for a cwd that isn't nested under
any known project at all — the genuinely-new-project case. This generalizes
past `multi_tenant`/`tenant_subdir` repos: no single- or multi-tenant dax
project has a legitimate reason for `cmd_run` to see a cwd that's a strict
subdirectory of an already-registered project's `dir`. Unaffected: the actual
in-container multi-tenant flow (`cd` to `processes/<name>/` inside an
already-running container, Gate B resolves it) never goes through `cmd_run`
at all.

### 7. Resolution table

**Revised 2026-07-28 — the `unattributed` fallback is gone.** See decision 8
below for why, and Sequencing step 6 for the classification mechanism that
replaces it.

| Situation | Result |
| --- | --- |
| single-tenant repo, root declared | starts anywhere in tree, state → that tenant |
| single-tenant repo, root undeclared | refuse — `dax tenant set`/`dax tenant classify` |
| multi-tenant repo, root itself | its own project/tenant if declared; refuse if not (see decision 8) |
| multi-tenant repo, mapped immediate child (or child of `tenant_subdir`) | starts, state → that tenant |
| multi-tenant repo, unmapped child, or more than one level deep | refuse |
| project creds span multiple tenant labels | refuse regardless |

Guiding principle: **fail toward over-isolation.** Where tenancy is ambiguous,
pick the narrower tenant. Over-isolation costs convenience; under-isolation
costs confidentiality. Those are not symmetric, and the 27-file pile is what
under-isolation looks like after a few months.

**"tree" means at most one level deep.** Claude may only ever be started at a
project's mount root or exactly one directory below it — never deeper. That
outer bound is enforced first, ahead of any `.dax-tenant` lookup and ahead of
either mode's own rule below: a cwd more than one level below the root
refuses outright regardless of what the repo declares. Implemented in
`dax_creds/tenant.py` as a plain root-plus-immediate-children check — no
arbitrary-depth directory walk, and so no walk-depth/symlink concerns to
design around either.

**`multi_tenant` is an explicit flag, not inferred.** Whether a repo has more
than one tenant is never determined by counting how many distinct
`.dax-tenant` labels happen to exist in it. It is declared explicitly, once,
as `multi_tenant: true` on the project's own entry in `~/.dax.yaml`
(`projects.<name>.multi_tenant`) — the same file that already holds that
project's `dir` and `creds`. This does not conflict with decision 9's
rejection of a central `~/.dax.yaml` tenant *map*: a project-level boolean
carries no customer name and is not tied to a specific subdirectory that
could be renamed out from under it, so none of that decision's reasoning
applies here.

The flag draws a hard line on where claude may even be started, not just on
where a tenant may be declared — a strict, non-overlapping split:

- `multi_tenant: false` (default) — the repo root **is** the sandbox: claude
  may only start there, full stop. A child directory refuses outright, even
  one level down, even if it happens to carry its own `.dax-tenant` file —
  that grants nothing in this mode. The tenant comes from the root's own
  `.dax-tenant`; if the root has none, `dax run`'s classification step (see
  decision 8 and Sequencing step 6) prompts for one before the container ever
  launches, so a wrapper invocation never finds the root genuinely
  undeclared in ordinary use. This is the point of how a project gets set up
  in the first place: `cd` to the project root, `dax run`, configure the
  environment — sandboxed claude, nothing more to decide.
- `multi_tenant: true` — claude may start either at the repo root itself or
  at an immediate child (or a child of `tenant_subdir`) that carries its own
  `.dax-tenant` file. **Revised 2026-07-28:** the root is no longer a
  permanent refusal — a multi-tenant repo's own top-level tooling is
  sometimes worked on independent of any one engagement subdirectory, so the
  root resolves as its own project/tenant when declared, exactly like any
  other node in the tree. What doesn't change: there is still no silent
  pooling anywhere. `dax run`'s classification step walks the root and every
  undeclared immediate child (see decision 8), so an unmapped node is a
  transient state to be resolved before launch, not a standing fallback
  destination.

Resolved: Gate B gets `multi_tenant` via an env var, not by reading
`~/.dax.yaml` itself. `~/.dax.yaml` is bind-mounted into every container
today (`dotfiles.rw`), so the wrapper technically *could* read it — but doing
so would mean re-deriving which `projects.<name>` entry it's looking at,
matching the container-side repo root's basename back against the (host-path)
`dir` field, duplicating logic Gate A already ran to get there. Gate A already
knows the answer with certainty at the moment it decides to mount the
container at all (`find_project_by_dir` already found the exact entry, and
already handles the unregistered-project case by falling into `dax init`), so
it hands that one bit down directly: `-e DAX_MULTI_TENANT=1` (absent/unset
for false), the same mechanism `cmd_run()` already uses for
`DAX_CREDS_CLAUDE` and friends (`dax.py:453-476`) and for `DAX_PREVIEW_PORT`/
`DAX_PREVIEW_DIR`. One value, handed down once, no second implementation of
the lookup to drift from the first.

**A second value is required alongside it: `DAX_PROJECT_NAME`.** Found while
building Gate B (`resolve_for_cwd` in `dax_creds/tenant.py`): `$HOME`
legitimately has other directory-level mounts sitting right alongside a
project's own — `~/.claude`, `~/.augment`, `~/.aws` (see
`.dax.yaml.example`'s `claudedir`/`auggiedir`/`awsdir`). Without knowing the
*actual* project name, Gate B can only infer "the repo root" structurally, as
whatever cwd's top-level directory under `$HOME` happens to be — so a user who
`cd`s into `~/.claude` and runs `claude` there would have it treated as if
`~/.claude` *were* the project root, found undeclared, and silently resolved
to `unattributed/.claude` instead of refusing. `DAX_PROJECT_NAME=<workdir_name>`
closes that: Gate A already computed this value in stage 3's `load_config()`
work, so handing it down is free, and Gate B refuses immediately if cwd's
top-level directory doesn't match it, before any `.dax-tenant` lookup happens
at all.

**A third value, `DAX_TENANT_SUBDIR`, exists for exactly one project.** Found
while actually assigning discernment's real tenants: its tenant subdirectories
don't sit directly under the repo root — they're one level further in, under
`processes/`. Discovered too late to design around cleanly; the two structural
alternatives (register `processes/` itself as a separate project; restructure
discernment's actual layout to remove the `processes/` indirection) were both
rejected — the agent needs everything in the repo visible from one project,
and the layout isn't something to reshuffle for this. So `resolve_tenant`
and `all_tenant_projects` (`dax_creds/tenant.py`) both take an optional
`tenant_subdir` parameter: when set, labeled subdirectories are looked for
under `repo_root/tenant_subdir` instead of `repo_root` directly, and
everything else about the resolution table — root/subdir-base never
resolves, unmapped children refuse, no `unattributed/` fallback — is
unchanged. Configured via `projects.discernment.tenant_subdir: processes` in
`~/.dax.yaml` (read into `config['tenant_subdir']` in `cmd_run()`, same place
`multi_tenant` is read), handed down to Gate B as `DAX_TENANT_SUBDIR`, same
mechanism as the two env vars above. Deliberately not generalized further than
one fixed subdirectory name — there is exactly one project shaped like this
today, and YAGNI argues against building for a second that may never exist.

### 8. Undeclared locations are classified before launch, never pooled

**Superseded 2026-07-28.** The original design routed anything undeclared to
`unattributed/<reponame>/` — a real tenant slot, warn, proceed. Rejected once
real usage made the cost concrete: *"i always want the tenant defined and
cleaning up later is a pain. so if the tenant isn't defined, lead the user
through defining it then continue."* Cleaning up an `unattributed/` tree
later is exactly the kind of archaeology decision 8 originally tried to avoid
turning into — it just moved the guesswork from "which tenant" to "did
anyone ever go back and fix this," which in practice nobody does.

**Replacement: classify at `dax run` time, refuse to launch until done.**
Before the container starts, `dax run` walks exactly the same nodes the
resolution table (decision 7) recognizes as legitimate start points — the
repo root always, plus (for a multi-tenant project) every immediate child
under `tenant_subdir` — and interactively prompts for a tenant name wherever
`.dax-tenant` is missing, offering the known-tenant list plus a "new tenant"
escape hatch. Anything already declared is left untouched. The container
does not launch until every node classifies. Implemented as
`_ensure_tenants_classified()` in `dax.py`, invoked from `cmd_run()` whenever
`claude_tenant_state` is in the project's `features` list.

`dax tenant classify` re-runs the same walk on demand, but with every node
re-prompted (including already-declared ones, offered as the current value
so accepting is one keystroke) — for correcting a mislabeled tenant without
having to `rm` the `.dax-tenant` file by hand first.

**Deliberately not extended to a directory discovered mid-session.** Docker
cannot add a bind mount to an already-running container, so a brand-new
subdirectory that appears after `dax run` has already launched cannot be
folded into the running container's mounts regardless of how it's
classified. That case still hard-refuses via the wrapper's existing
`dax tenant set <subdir> <tenant>` message — no interactive prompting
in-container, since satisfying the prompt wouldn't actually unblock the
session anyway. Accepted as small, livable friction: `dax run` again after
declaring picks it up on the next launch.

**Prior implementation note, retained for history:** the original
`unattributed` mechanism kept `tenant` and `project` as two separate fields
(`'unattributed'` and `<reponame>`), not a combined string — a real bug
(doubled reponame in the composed path) was caught this way during manual
testing before the mechanism was removed outright. The two-separate-fields
lesson still applies wherever `tenant`/`project` pairs are built today.

**`dax tenants` enumerability was originally accidental, not designed —
fixed 2026-07-28.** As first built, `dax tenants` only listed directories
under `~/.local/state/dax/tenants/` that already existed — which meant only
tenants that had already had a real `dax run` session, since nothing else
ever created that path. A tenant declared with `dax tenant set` but never
launched was invisible. First fix attempt was to have `dax tenant set` also
`mkdir -p` the state directory at declaration time; reverted almost
immediately in favor of the real fix — `dax tenants` re-derives the live
(tenant, project) set directly from `~/.dax.yaml`'s `projects` plus whatever
`.dax-tenant` files actually say right now (the same inputs `dax run` itself
uses), rather than reading anything off the state tree for enumeration at
all. That makes the pre-create workaround redundant — every declared tenant
shows up immediately regardless of whether anything's ever mounted it — and
is also more correct: a stale declaration change can never continue to show
something that no longer resolves that way, which reading the state tree
would risk.

Output is a table (`TENANT`/`PROJECT`/`SESSION`/`PATH`, fixed-width columns,
sorted so same-tenant rows are contiguous), including each project's real
host path — for a single-tenant project that's the repo root; for a
multi-tenant one it's `repo_root/[tenant_subdir/]<labeled subdir>`. `SESSION`
is `yes`/`no` based on whether `~/.local/state/dax/tenants/<tenant>/<project>/`
has a `.claude.json` or `.credentials.json` in it — the two things only a
real launch ever writes — so declared-but-unused and actually-active tenants
are distinguishable at a glance. Projects whose registered `dir` doesn't
exist on the current machine are silently skipped.

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

The "two containers on the same project" case this section originally analyzed
**cannot happen under current dax behavior**, verified 2026-07-27. Container
names are derived deterministically from the launch directory (`dax.py:75`,
`envname` = cwd relative to `$HOME` with `/` → `-`) and passed explicitly as
`docker run --name` (`dax.py:393-398`), with `--rm` and no collision handling
anywhere in the file. A second `dax run` from the same project directory
computes the same name and fails outright with Docker's "name already in use"
error — it does not attach, reuse, or race the first container. So per-project
state never actually has two containers writing to it concurrently; not a
Docker mount restriction (plain bind mounts don't have one), a dax one.

Residual, not currently in play: dax has no `attach`/`exec` subcommand — the
only `docker exec` reference in the codebase is a debug hint for port
forwarding, not a workflow. Manually running `docker exec -it <container> bash`
against an already-running container would sidestep the naming guard entirely
(same container, two shells) and reproduce the contention below. Worth knowing
if that pattern ever gets adopted; not a reason to design against it today.

Recorded for that hypothetical, since the analysis is already done:

| State | Keyed by | Risk |
| --- | --- | --- |
| `projects/<slug>/*.jsonl` transcripts | session id | none |
| `projects/<slug>/memory/` | filename per fact | last writer wins on the same file |
| `paste-cache/`, `backups/` | per-item filenames | none |
| `sessions/`, `session-env/`, `tasks/`, `file-history/` | per session id | none *(assumed, not verified)* |
| `history.jsonl` | appended | interleaving; grpcfuse append atomicity unguaranteed, so torn lines possible |
| `.claude.json` | single file, full rewrite | lost updates, last writer wins |

`.claude.json` lost updates mean a permission granted in one session can be
silently dropped when the other writes, and tip counters bounce. Annoying, not
corrupting, recoverable by re-granting, with rolling backups as a floor.

**Strictly better than the status quo regardless**, which is every container
across every tenant writing one shared `.claude.json` and one `history.jsonl`.
Even in the residual `docker exec` case, this narrows the concurrent-writer set
from "all containers, all tenants" to "two shells in one container on one
project." The defect is pre-existing and upstream; its blast radius shrinks to
near nothing.

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
5. **Wrapper-side tenant resolution** (`claude` wrapper, `dax_creds/tenant.py`,
   `dax-creds resolve-tenant`). Reordered ahead of the mount replacement below
   and decoupled from it via a master switch, `DAX_TENANT_STATE`, which
   `dax run` sets only for projects that have opted into step 6's feature.
   Unset — every project today — the wrapper's tenant resolution never runs
   and behavior is unchanged; this is what lets this step land on its own,
   independently testable, without the mount replacement also being live yet.
   `dax tenant set`/`dax tenants` are part of step 6, not this step, since
   they write/enumerate `.dax-tenant` files that only matter once a project
   has actually opted in.
6. Replace the wholesale `~/.claude` mount and the `~/.claude.json` dotfile
   mount with per-tenant/project trees under `~/.local/state/dax/tenants/`,
   behind the same opt-in feature (`claude_tenant_state`) that sets
   `DAX_TENANT_STATE`, `DAX_MULTI_TENANT`, and `DAX_PROJECT_NAME` (the last of
   these is what lets Gate B tell a real project mount apart from `~/.claude`/
   `~/.augment`/`~/.aws` sitting at the same `$HOME` level — see decision 7).
   For a multi-tenant repo, mount only `tenants/*/<project>/` — the repo's own
   subtree from each tenant the subdir map references, never a whole tenant
   tree. Includes `dax tenant set`/`dax tenants`/`dax tenant classify` (see
   decision 8).

   **Implemented and unit-tested** (`feature_claude_tenant_state` in `dax.py`,
   opted into via a project's own `features:` list — see decision 7's
   "`multi_tenant` is an explicit flag" note — plus `config['multi_tenant']`
   wired from the project registry in `cmd_run()`). `cmd_run()` also invokes
   `_ensure_tenants_classified()` whenever `claude_tenant_state` is opted in,
   refusing to launch until every root/child node resolves — 259 tests pass
   as of 2026-07-28, covering the resolution table, `dax tenants`,
   `_ensure_tenants_classified`, `_prompt_tenant`, and `dax tenant classify`.

   **Verified 2026-07-27: Docker auto-creates a missing bind-mount host
   directory owned by the actual host user, not root.** This mattered because
   the per-tenant/project host directories under `~/.local/state/dax/tenants/`
   don't exist until first use, and every manual test up to this point had
   only exercised a container-local `mkdir -p` (the wrapper's own, run
   entirely inside the container's filesystem) — never a real host bind-mount
   of a brand-new nested path. Checked directly:
   `docker run --rm -v ~/scratch-permission-test:/test alpine sh -c 'id; ls -la /test'`
   auto-created the host directory; `ls -la ~/scratch-permission-test` on the
   Mac side (not through a root-run container, which would trivially bypass
   permission checks either way) showed it owned by the actual host user.
   Since the container's own user is built with a matching UID (the repo's
   "host UID matching" build step), this confirms a brand-new tenant/project
   directory is fully writable by the container the moment Docker creates it —
   the specific risk carried out of implementation is closed. Still not done:
   the full end-to-end proof on a real opted-in project (all resolution-table
   rows, real Claude Code launch through this exact mount path, multi-day
   rotation soak) that the plan's Verification section calls for.
7. ~~Credential-span escalation check.~~ **Dropped.** Traced through what
   decision this would actually drive: it protects against a `dax_creds`
   credential-delivery misconfiguration (a project's `creds:` list spanning
   multiple tenants' credentials) that is orthogonal to the Claude-state-bleed
   problem this feature fixes, and is exactly as possible today as it would be
   after this feature ships — steps 3-6 fully close the actual bug without it.
   Revisit later, separately, if it ever actually bites, rather than inventing
   a tenant-labeling scheme now to guard a hypothetical.

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
| `theme` | ~~theme picker on first launch~~ — **retested 2026-07-27 against v2.1.220: false.** With only `hasCompletedOnboarding` seeded, `interactive_launch_probe.py --show` reaches `Welcome back Dave!` directly, no theme picker shown. Not seeded; nothing to fix. |
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

### Confirmed 2026-07-28 — steps 3-6 proven on the two real repos this design exists for

`claude_tenant_state` opted in for real, on the actual `fabric` (single-tenant)
and `discernment` (multi-tenant, `tenant_subdir: processes`) projects — not
scratch directories. Both directly exercised the exact failure this whole
design was written to fix (Problem section, above): `discernment` was the
repo where a session got preloaded with another customer's candidate slate
and a third party's compensation model.

- **fabric**: declared (`personal`), real launch, real credential flow (found
  and fixed a genuine, pre-existing `dax creds add`/Keychain-overwrite bug
  along the way — see `CLAUDE.md` Backlog), history and arrow-up recall work.
- **discernment**: `hydraulic-controls-it` and at least one other process
  labeled under `processes/`. Two concurrent `claude` sessions, different
  tmux windows, same container, different labeled processes — each got its
  own isolated `CLAUDE_CONFIG_DIR`. Turns run, quit, restarted: history on
  restart contained only that session's own prior turns, nothing from the
  other window's tenant. This is the concrete, positive version of the bug
  this design exists to fix, not just its absence.
- Root refusal, unmapped-subdir refusal, and the `tenant_subdir` depth
  accommodation all confirmed against the real repos, not just unit tests,
  once the image was rebuilt with that code included (an earlier attempt hit
  stale pre-rebuild behavior — see the `tenant_subdir` note above).

Still outstanding before this can be trusted as the default: the shared
`plugins`/`skills`/`commands`/`cache` migration (scoped, not yet built — see
CLAUDE.md In progress), and the multi-day rotation soak, still not clear
after any of today's testing since nothing here ran long enough to hit a
rotation.

### Confirmed 2026-07-28 — `unattributed` dropped, root-as-project, interactive classification

Real-world testing above surfaced two further design gaps addressed the same
day, both now implemented and tested (see decisions 7 and 8, and Sequencing
step 6):

- **discernment's own top-level tooling needs its own project/tenant.**
  Working on discernment independent of any one customer process is a real,
  recurring need, not an edge case — a multi-tenant repo's root now resolves
  as its own project when declared, in both single- and multi-tenant modes,
  rather than always refusing.
- **The `unattributed` fallback is gone entirely**, replaced by interactive
  classification at `dax run` time (`_ensure_tenants_classified()`), with
  `dax tenant classify` as an on-demand re-walk. No location remains that
  silently resolves to a pooled default; every start point either is already
  declared, gets classified before the container launches, or hard-refuses
  (only for a subdirectory discovered mid-session, where Docker's inability
  to add mounts to a running container makes interactive resolution moot
  anyway).

Also fixed in the same pass, found while re-testing `dax tenants` against a
multi-tenant repo whose root now resolves as a project: `cmd_tenants`' path
computation used `tenant_base / project` unconditionally, which produced a
nonsense doubled path (`discernment/processes/discernment`) for a declared
root. Now branches on whether `project == reponame`.

259 tests pass (`tests/test_dax_creds_tenant.py`,
`tests/test_dax_creds_cli.py`, `tests/test_features.py`,
`tests/test_dax_tenant_cmds.py`, full suite). Not yet re-run against the real
fabric/discernment containers since this change — that would require an
image rebuild (`dax_creds` is a frozen, non-editable install) and a fresh
round of manual verification, not yet done.

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
