# Tenant Isolation for Claude Code State

> **⚠ SUBSTANTIALLY REVISED 2026-07-29 — read
> "[Redesign 2026-07-29](#redesign-2026-07-29--multi-tenant-abandoned)" first.**
> The multi-tenant model is abandoned. Decisions 4, 6 (Gate B), 7, 8, and 12
> below are superseded in whole or in part, and the implemented code they
> describe is slated for deletion. The decisions are left in place rather than
> rewritten because the reasoning that produced them — especially decision 4's
> deletion argument — is what the replacement had to answer, and a reader who
> only sees the conclusion will re-derive the old model.

**Status:** Sequencing steps 1–2 implemented and merged (PR #4, 2026-07-27);
steps 3–6 implemented, unit-tested (259 tests), and confirmed on the two real
target repos (2026-07-28) — see Verification log; step 7 dropped (see
Sequencing). **Superseded by the 2026-07-29 redesign:** multi-tenant support is
being removed, and the outstanding work list is restated there. Still
outstanding from the original list: the shared
`plugins`/`skills`/`commands`/`cache` migration (reshaped — it becomes a nested
mount) and the multi-day refresh-token rotation soak.
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

> **Partly superseded (redesign A).** The conclusion survives — tenant is still
> the outer directory, for exactly the deletion reason below. What changed is
> that tenant is now a *declared grouping label* rather than something resolved
> from a repo's structure.

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

> **Partly superseded (redesign E).** Gate B is deleted: with one project per
> container and a single state mount, there is nothing to mis-resolve and
> nothing to refuse. Gate A survives, as does the separate "Gate 0" note below
> about `dax run` from a subdirectory.

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

> **Superseded (redesign A/E).** The table enumerates cwd→tenant outcomes that
> no longer arise once a container holds exactly one project.

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

> **Superseded (redesign A/E).** Interactive classification existed to resolve
> undeclared subdirectories inside a multi-tenant repo. With one project per
> repo there are none, and this machinery is slated for deletion.

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

> **Wording superseded (redesign C).** Still true that no *host* credential file
> is bind-mounted in. But the credential now lives on host disk for the
> session's duration, as `.credentials.json` inside the bind-mounted state tree,
> written there by the wrapper on first launch and thereafter owned and
> refreshed by Claude Code. The intended end state removes it on teardown; that
> write-back is not built yet.

## Redesign 2026-07-29 — multi-tenant abandoned

**Provenance:** the conversation in which the decision to abandon multi-tenant
was originally reached is **lost**. The Anthropic outage of 2026-07-28/29 left
sessions hanging; they were killed, and neither `~/.claude/projects/` nor
`history.jsonl` contains anything between 2026-07-27 18:55 and 2026-07-29
21:14 — the entire two days that produced commit `4c8fb6c`. The decisions below
were reconstructed from scratch on 2026-07-29 by walking the open questions one
at a time. **That loss is the reason this section exists and the reason the
memory directory is now populated.** Write decisions down when they are made.

### A. Multi-tenant is abandoned; `tenant` survives only as a grouping label

The only multi-tenant repo was discernment. It is being reworked so that each
process becomes its own repository (see D), which removes the sole justification
for the model. With one project per repo, tenant resolution becomes trivial.

`tenant` is **not** dropped, because decision 4's argument still holds for
*grouping* even though it no longer holds for *resolution*: a customer with two
repos still needs "end the engagement" to be one operation. So:

- `tenant:` becomes a declared field on the project's `~/.dax.yaml` entry. One
  value, no inference.
- State stays at `~/.local/state/dax/tenants/<tenant>/<project>/`, keeping
  tenant as the outer directory so `rm -rf .../tenants/<tenant>/` remains a
  complete deletion for that customer's Claude state.
- `.dax-tenant` files go away entirely. They labelled subdirectories inside a
  multi-tenant repo; there are none.

### B. The state tree mounts at `~/.claude`, and `CLAUDE_CONFIG_DIR` is a constant

```
host:       ~/.local/state/dax/tenants/<tenant>/<project>/
container:  ~/.claude
env:        CLAUDE_CONFIG_DIR=$HOME/.claude
```

The variable is kept **only** because of finding 1 (`CLAUDE_CONFIG_DIR`
relocates `.claude.json` too). It is no longer a selector — no cwd resolution,
no per-session computation — just a constant `dax run` sets.

Dropping it entirely was considered and rejected. Unset, Claude Code reads
`~/.claude.json`, a *sibling* of `~/.claude` rather than a child, so the file
falls outside the tree mount: container-local, discarded on teardown, taking
per-project trust, permissions, and the `projects{}` block with it. The symptom
is the first-run wizard reappearing, not an error. Two workarounds were
rejected:

- **Single-file bind mount** of `.claude.json` — reintroduces exactly what this
  design was pleased to escape (see the end of Concurrency): single-file
  grpcfuse mounts are where write-temp-plus-rename breaks.
- **Symlink** `~/.claude.json` → `~/.claude/.claude.json` — fails silently.
  Claude Code writes temp-then-rename; the rename *replaces the symlink* with a
  regular file and state quietly stops persisting.

Accepted cost: the "Accepted costs" table below applies per project. The seed
template mitigates most of it, but the wrapper currently seeds only
`hasCompletedOnboarding` — folder trust is deliberately not seeded — so expect a
trust dialog and replayed tips on each project's first launch.

Note this was a regression being fixed, not just a new capability: `.claude.json`
has been discarded on every teardown since the step-2 diagnostic removed it from
`dotfiles.rw` on 2026-07-27 (see Environment state).

### B2. No `shared/` tree — mount the host's own directories, decided 2026-07-30

Supersedes decision 11's shared-directory list and the "shared
plugins/commands/cache migration" work item, which is **deleted rather than
built**.

`shared/` was never multi-tenant machinery, so abandoning multi-tenancy does not
remove its rationale by itself: it solved "install once, use in every tree", and
that multiplicity is *per project*, which survives the redesign intact — four
envs means four trees. What changes is the membership, and then whether a copy
step is warranted at all.

Membership, checked against the real host directories rather than assumed:

| Dir | Actual contents | Verdict |
| --- | --- | --- |
| `commands` | 132K, 11 commands in active use (`tdd.md`, `pr-review.md`, `refactor.md`, …), all mtime Feb 19 | **mount** — real user state, lost per project otherwise |
| `plugins` | 6.3M, and *nothing installed*: only the official marketplace catalog, which Claude Code auto-installs itself (`officialMarketplaceAutoInstalled: true`, no `enabledPlugins` key, `lastUpdated` moving on its own) | **mount, weakly** — no state to preserve; avoids four independent catalog clones and refetches. Pure optimization |
| `skills` | discernment's own skills (`Council`, `FirstPrinciples`, `discernment.md`, …) | **drop** — arrives via decision D's sidecar mount at `~/.claude/skills/`. A `shared/skills` would be a stale copy of the repo |
| `cache` | 472K: a generic `changelog.md` plus a 2-byte `my-closed-issues.json` | **drop** — derived, generic, cheap to refetch, adds write contention, and is the only one of the four where fetched content can turn account-specific |

The `plugins` finding inverts the original justification. "Install once" was the
argument for sharing, and nothing is installed — so what would be shared is a
self-healing catalog, not user state.

**And with the list down to two, the copy step is not worth its cost.** The
original plan was a one-time host-wide copy into
`~/.local/state/dax/shared/{...}`, seeded on first use. Mounting the host's
`~/.claude/commands` and `~/.claude/plugins` directly — nested inside the tree
mount, which Docker orders by destination depth — achieves the same thing with no
seeding code, one source of truth, and no divergence class where a command
authored on the host is invisible in containers or vice versa. It is also
*strictly less* container write access than today, narrowing a wholesale rw
`~/.claude` mount to two subdirectories.

Mounted **rw**: authoring a command inside a container and having it persist is
worth more than insulating the host directory, which is already fully writable
from every container today. Read-only remains a cheap change if that stops being
true.

Accepted cost: an env wanting a *different* command set — a customer container
with none of the personal tooling — needs an opt-out. That is a per-env flag if it
ever comes up, not a reason to maintain a copy.

Retires decision 11's open caveat that `skills/` stops being shareable "the
moment a customer-specific skill is written": D resolves it differently, with
shared skills versioned in the discernment repo and project-specific ones in the
project's own `.claude/skills/`.

### C. One credential per tenant/project, each from its own fresh login

Credentials are named `claude-<tenant>-<project>`. Expect N Anthropic accounts
under M Keychain entries, where N is usually 1.

**Each entry must be minted by its own login flow.** This is load-bearing, not
incidental. Independent authorization grants have independent refresh-token
chains, so rotation in one project cannot invalidate another's credential — even
when every entry authenticates the same account. If a second project's entry is
instead created by *copying* an existing credential blob, the two share one
grant and the first rotation kills the other.

That copy path is live in the code today: both `_setup_claude_credential`
(`init.py`) and `_post_login_import` (`dax.py`) call
`ClaudeProvider.import_from_disk()`. Minting a per-project credential must not
go through it.

Intended lifecycle, once teardown is built:

```
dax run   → inject from Keychain into the project's state tree, if absent
session   → Claude Code owns the file and refreshes it in place
teardown  → write the current file back to Keychain, remove it from disk
```

**Implemented as of 2026-07-29: inject-if-absent only.** The wrapper previously
overwrote `.credentials.json` from the Keychain snapshot on *every* launch,
which under a persistent state tree would clobber a refreshed token with a stale
one on the next start — turning the rotation gap from "one shared copy might go
stale" into "every project independently walks into it." Create-if-absent lets
Claude Code own rotation from first launch onward and makes Keychain a
*bootstrap* rather than the running source of truth.

Teardown write-back is deliberately **not** built yet, and the gap is accepted:
a `kill -9`, host reboot, or Docker Desktop crash skips it, leaving the
refreshed credential in the tree and a stale one in Keychain. Because injection
is now create-if-absent, that stale copy is never forced back over the good one
— the tree's file simply persists and continues to work. A later sweep can
hoover up or re-import stray files.

Residual unknown, unchanged: whether Anthropic caps concurrent grants per
account or invalidates older grants for the same `client_id`. Cheap to test —
log in for two projects, use the first, confirm the second still works.

### C2. The per-env credential name is derived, not typed — built 2026-07-30

Listing a bare provider name in an env's `creds:` asks dax to build the name:

```yaml
projects:
  fabric:
    tenant: personal
    creds: [claude, github-dbfarrow, ssh-id-rsa]   # -> claude-personal-fabric
```

`BARE_PROVIDER_CREDS` (`dax_creds/config.py`) holds the providers this applies
to, and contains **only claude**: its credentials are per tenant/project by
decision C, while github/gmail/ssh are genuinely user-level, so naming those
explicitly stays correct.

Rationale: decision C makes correct naming load-bearing for isolation, and a
hand-maintained convention is one users typo. A real `claud-fre` entry sat
unused in `~/.dax.yaml` for weeks — silently, because a misspelled credential
is simply never matched.

- **An explicit name always wins**, keeping existing configs working and
  leaving an escape hatch for deliberately sharing one credential across two
  envs.
- **A bare token with no `tenant` is refused**, and `cmd_run` exits. Falling
  back to a shared credential would be the exact isolation failure the
  convention exists to prevent, so this fails loudly. It also makes declaring
  `tenant` load-bearing rather than optional.
- **Unregistered derived names are synthesized in memory**, not written to
  `~/.dax.yaml`. Resolution stays a pure read; the caller reports the
  credential missing from Keychain and offers the login that registers it.
- `dax env show` prints the resolved name, so the convention is visible rather
  than magic.
- `find_named_project_by_dir()` is new — returning `(name, project)` — because
  a derived name needs the registry name, which `find_project_by_dir` discarded.

Ambiguity to guard: a credential named literally `claude` would collide with
the bare token. Not yet blocked in `dax creds add`.

### C3. An abandoned login must not import the credential already on disk

**Found in live testing 2026-07-30, and it is the sharing failure C exists to
prevent — presenting as success.**

`dax creds login claude-personal-discernment` was cancelled at the browser step
(it opened the wrong Chrome profile — see C4). `_post_login_import` nevertheless
ran, because `cmd_creds_login` called it unconditionally after
`subprocess.run()` with no check that the flow succeeded. Its claude branch
reads `~/.claude/.credentials.json` — which, with `claude` back in `features`,
holds the *existing shared* credential. That token was stored under the new
per-env name.

The result authenticates perfectly, which is exactly the problem: two Keychain
entries backed by one OAuth grant, no isolation, and a rotation on either kills
both. Nothing in the output suggested anything had gone wrong.

Fix: snapshot what the provider's on-disk location holds *before* launching the
flow, compare after, and refuse to import when unchanged. Provider-agnostic —
`_disk_token_for()` covers github, claude, and auggie, all of which import from
disk and had the same hole. A real login always replaces the file, so a
before/after match means the flow wrote nothing.

Corollary fixed in the same pass: a derived credential could not be *removed*.
`dax creds remove` rejected it as unknown, since it has no `credentials:` entry,
leaving no way to clear a bad one. It now clears the Keychain secret, writes no
config, and reports that the env will be offered a fresh login next run —
exempt from the still-referenced refusal, because for a derived name the
reference is the convention, which survives removal and simply re-mints.

### C4. Derived credentials inherit browser settings from `credential_defaults`

Also found in the same test: the login opened the wrong browser. A derived
credential is synthesized as `{'provider': ...}` and has no entry to carry
`browser`/`chrome_profile`, so every per-env login fell back to the default
browser rather than the Chrome profile the account lives in.

```yaml
credential_defaults:
  claude:
    browser: chrome
    chrome_profile: Profile 2
```

Merged into every synthesized definition. This is the right shape rather than a
patch: browser and profile are properties of the human authenticating, not of
the env, so one setting covering all derived Claude credentials is correct.
Defaults never override `provider`, and an explicitly registered credential
ignores them entirely.

Related fix: `dax creds list` omitted derived credentials, so the host
disagreed with `dax-creds list` inside the container about which credentials
exist. It now unions the registry with `derived_credentials()` and tags them
`(derived)`. The "used by" column was matching names literally, so an env
listing a bare `claude` matched nothing — `credential_users()` resolves now,
which also correctly shows a superseded explicit credential as unused.

### C5. `dax creds login` refuses derived names — found 2026-07-30, step 4

**Blocking, and it means no sanctioned way to mint a per-env Claude credential
currently exists.**

`dax creds login claude-personal-fabric` answers `Unknown credential` while
`dax creds list` displays that same name as `(derived)`, `stored: yes`.
`cmd_creds_login` (`dax.py:698`) resolves against `config['credentials']` only.
`derived_credentials()` was wired into `creds list` (`init.py:434`), `creds
remove` (`init.py:741`), and `env show` (`init.py:591`) in the C2/C3/C4 pass —
login was missed.

Consequence: `login` refuses derived names, and `add`'s claude path is the
unguarded one C3 warns against, so **neither door works**. C3's guard is
unreachable for the case it was written for, and C2's promise that the caller
"offers the login that registers it" leads to a command that fails.

Fix (small — C4 comes free, since `derived_credentials()` builds definitions
through `synthesized_credential()`, which already merges `credential_defaults`):

```python
cred_def = config.get('credentials', {}).get(cred_name)
if cred_def is None:
    from dax_creds.config import derived_credentials
    cred_def = derived_credentials(config).get(cred_name)
if cred_def is None:
    raise KeyError(cred_name)
```

How the three existing derived secrets came to exist, by inference: they were
minted while those names were still *explicitly* registered in `credentials:`,
and C2's migration to bare `claude` tokens dropped those entries — Keychain
secrets are keyed by name and survived. Consistent with C3's record of a
`claude-personal-discernment` login opening a browser on 2026-07-30, and with
step 3's staggered mint dates. `backup/.dax.yaml`'s git history can't confirm
it: only one version exists and it predates per-env credentials.

**Fixed and confirmed live 2026-07-30.** `_login_credential_def()` now resolves
registered-then-derived, and `cmd_creds_login` calls it; 5 tests added, suite at
347. `dax creds login claude-personal-fabric` launched for real: Chrome on
Profile 2 (C4, reachable for a derived name for the first time), the device-flow
`redirect_uri` rewritten to `platform.claude.com/oauth/code/callback` (so
`_force_claude_device_flow_redirect` works), and a token imported to Keychain.
The grant hash changed `49a03025bda2` → `56b0ef07fcbe`, which is **direct**
proof the flow minted a new grant rather than copying an existing one — a copy
would have shown dax's or discernment's hash. Step 3 re-run: still PASS, 4
distinct grants.

**C3's refusal path confirmed live 2026-07-30**, on a second run cancelled at
the paste-code prompt: the refusal printed, and Keychain was untouched
(`claude-personal-fabric` still `56b0ef07fcbe`, unchanged from the completed
login). Note the disk happened to hold fabric's *own* grant at the time, so the
refusal prevented a harmless self-copy; the dangerous case is the disk holding a
*different* env's grant. The guard is name-agnostic — it compares the on-disk
token before and after and never inspects whose grant it is — so this exercised
the same code path with the same shape of input.

### C6. Enforce one-grant-per-name at import time — built 2026-07-30

Residual hole in C3, found by inspection 2026-07-30 while confirming the fix,
and closed the same day.

`_disk_token_for` returns the whole re-serialized envelope
(`providers/claude.py:77`), so `after == before` answers "is the file
byte-identical", not "is this the same OAuth grant". Under `feature_claude`'s
shared `~/.claude` mount, a container running Claude Code can refresh
`.credentials.json` *during* the login window. An abandoned login then sees a
changed file, concludes the flow wrote it, and imports another env's refreshed
token under the new name — C3's failure again, through a narrower door, and with
C3's silent-success signature.

Requires a refresh inside a window of minutes, and access tokens live hours, so
the probability is low but not negligible.

**What was built.** Rather than tightening the proxy, the invariant is now
enforced directly: before storing a Claude credential, hash its `refreshToken`
and compare against every *other* stored Claude credential. Refuse on collision.
This is `tests/manual/check_claude_grants.py`'s logic applied at the moment of
storing, which demotes the manual script from sole detector to redundant safety
net.

- `grant_id(blob)` (`providers/claude.py`) — sha256 of the refresh token,
  truncated. The blob carries no grant ID, so the refresh token stands in for
  one; hashing means callers can report and log it without handling the secret.
  None for anything that isn't a refreshable envelope.
- `ClaudeProvider.grant_collision(name, token, candidates)` — the already-stored
  credential this token would share a grant with, or None. Skips `name` itself,
  since re-importing a credential's own token is a no-op rather than sharing —
  and refusing it would block re-importing after a `dax creds remove`.
- `credential_names_for_provider(config, provider)` (`config.py`) — registered
  **and derived** names. Enumerating only the registry would have made the check
  pass by seeing nothing, since the colliding name is normally derived.
- `grant_collision_message()` — shared, so `login` and `add` explain the refusal
  identically.

Wired into **both** doors: `_post_login_import`'s claude branch (`dax.py`), and
`_setup_claude_credential` (`init.py`), which never had the before/after guard at
all and was the outstanding item under "Outstanding after this redesign". Its
`config` parameter is optional, so callers without one still work and the check
simply doesn't run.

Deliberately claude-only. Sharing a grant is a problem because decision C makes
per-env Claude credentials load-bearing for isolation; github/gmail/ssh are
genuinely user-level, where one credential under one name is the intent.

Where it is still inert: inside a container there is no Keychain to compare
against, so `grant_collision` returns None. Nothing to check is not the same as
a collision, so the import proceeds — but the enforcement is a host-side
property, and the manual script remains the only way to audit sharing that
already exists.

20 tests in `tests/test_dax_claude_grant_collision.py`; suite at 367. Verified
that `grant_id` on the live shared `~/.claude/.credentials.json` returns
`56b0ef07fcbe`, matching what the manual script computed independently for
`claude-personal-fabric`.

**Side effect of any Claude login, worth knowing before running one:**
`_LOGIN_PROVIDERS['claude']` mounts the host's `~/.claude` (`dax.py:644`), so the
flow writes its new credential into the **shared** `.credentials.json`. The
verification script's "matches" line moved from `claude-personal-dax` to
`claude-personal-fabric` accordingly. While `feature_claude`'s wholesale mount is
still in play, that repoints *every* container — inject-if-absent skips a
non-empty file, so each one authenticates on whichever env logged in last,
regardless of its own `DAX_CREDS_CLAUDE`. Logging in for one env silently
changes the grant every other env runs on. It is also exactly why C3's
before/after guard functions: the flow writes to the same file the guard
snapshots.

Corollary sharpening the step-2 presentation finding: `_post_login_import`
stores to Keychain and never writes a `credentials:` entry, so a derived name
stays derived **permanently**. `env show`'s `[not registered yet]` will therefore
never stop saying "yet" no matter how many successful logins run — the label is
wrong, not merely ambiguous. And C2's phrase "the login flow is what registers
them" is loose: the flow stores a secret, it registers nothing.

### C7. `dax creds add` rebuilds the definition, dropping unprompted fields

*Partly superseded by C8, which reorders the flow so a per-env name is never
typed. The lossy rebuild still applies to explicitly named credentials.*

Found by inspection 2026-07-30 while scoping the manual step 6.

`_define_credential` starts from `{'provider': provider_name}` (`init.py:113`) and
`_run_creds_add` assigns the result wholesale (`init.py:369`), so re-running
`dax creds add` on an existing credential **silently drops every field its prompts
do not ask for**. `dax creds update` is the non-lossy path for metadata.

This makes the "Overwrite it?" confirmation misleading in a second, opposite
direction from the bug fixed on 2026-07-30: the *definition* is always
overwritten by reconstruction, whether or not the user is thinking about it,
while the *secret* was the thing that used to survive.

Consequence for testing the `replace=` plumbing live: the safe-looking target is
the wrong one. `github-dbfarrow` rebuilds losslessly but, having no `client_id`,
exits at "skipped" before `_report_no_token` runs, so it never demonstrates the
message. `gmail-personal` does reach it but requires retyping `client_id`,
`client_secret`, and `scopes` exactly. `claude-fre` rebuilds cleanly and must
*not* be used at all: claude's replace path calls `import_from_disk`, which would
copy the shared `~/.claude/.credentials.json` under that name and manufacture the
very shared grant C3 exists to prevent — the still-open `dax creds add` gap,
reachable by accident while testing something else.

All three `replace=` behaviors are unit-tested
(`tests/test_dax_creds_init.py:281–320`), so the manual step was downgraded to
optional rather than made safe.

### C8. `dax creds add` asks the provider first and never asks for a per-env name

Built 2026-07-30, replacing C7's confirmation-prompt idea with a reordering that
makes the problem structural rather than something to warn about.

The old flow asked for a **name** first, then the provider. Two consequences:

- A per-env credential's name is dax's to build (C2), so asking for it invites
  exactly the typo C2 exists to eliminate — a misspelled `claud-fre` sat unused
  for weeks, silently, because a name nothing resolves to is never matched.
- Typing a derived name converted it to an explicitly registered one, with no
  "Overwrite it?" prompt (it has no registry entry to collide with), which
  silently stopped it inheriting `credential_defaults` (C4).

Provider first dissolves both: once the answer is a provider in
`BARE_PROVIDER_CREDS`, dax knows the name is derived and never asks.

The claude path is now:

1. `Provider:` → `claude`
2. `Env:` — the registry's envs, with the one containing cwd offered first
   (`find_named_project_by_dir`, falling back to `find_enclosing_project`, so a
   subdirectory resolves too)
3. `tenant` **only if that env has none** — the one point where it genuinely is
   not known, and where C2 refuses rather than falling back to a shared
   credential. Asked here rather than earlier because tenant belongs to the env,
   not the credential; everywhere else the registry already knows it, and a
   separate prompt would let the answer disagree with the env.
4. the derived name is printed, not requested
5. offer to add the bare `claude` token to that env's `creds:` list when absent —
   otherwise the credential would exist and go unused
6. `credential_defaults.<provider>` is written **only if missing**, since browser
   and profile belong to the human authenticating, not the env (C4)
7. **no token is ever imported from disk.** `add` hands off to
   `dax creds login <derived-name>`, offering to run it immediately

Point 7 is the substantive change, and it is what closes the last route to a
shared grant by construction rather than by refusal. C6 still refuses collisions
at store time, but not offering the copy is better than rejecting it afterward.
`_run_creds_add` returns `(config, pending_login)`; `cmd_creds` runs the login
when the user accepts.

Related fix in the same pass: every `_setup_*` now returns whether a usable
secret ended up stored, so the flow stops printing `Credential "X" saved.` for a
credential nothing was stored for. That message was not false — the YAML write
did happen — but it answered a different question than the one being asked,
which is whether the credential works now.

**Still carrying the old shape: `dax init`'s inline creds loop** (`init.py`,
~line 325) has its own copy of name → provider → define → setup, so a claude
credential registered during `dax init` is still hand-named. It is safe rather
than correct: `_setup_claude_credential` keeps C6's collision guard, so the worst
case is a badly named credential, not a shared grant. Folding it into this flow
belongs with the already-backlogged work to prompt for `tenant` during `dax init`.

18 tests in `tests/test_dax_creds_add_flow.py`; suite at 385.

### D. Discernment becomes a sidecar, not a multi-tenant repo

Discernment exists to give a set of processes a shared spine of context (telos)
and skills. Reworked shape:

- Each process moves to **its own repository**, with **history split, not
  `git mv`** — deleting a directory does not remove it from git history, and a
  wholesale sidecar mount would otherwise put every customer's historical
  process data in every other customer's container. Out of scope for dax; the
  combined repo is to be distributed into per-process repos and then deleted.
- Discernment mounts **wholesale** at `~/discernment` (rw), a sibling of the
  process repo at `~/<processname>`. Any process session can update telos and
  skills directly.
- Its `skills/` mounts a **second time** at `~/.claude/skills/` (rw, same host
  directory). This is required: Claude Code discovers skills by scanning
  `~/.claude/skills/`, project `.claude/skills/`, and plugins. A CLAUDE.md
  *pointing* at `~/discernment/skills/` registers nothing — the files would be
  readable as prose but not invokable, losing progressive disclosure. Edits
  through either path land in the same working tree.
- Telos lives as **repo files** and is reached from each process's CLAUDE.md by
  `@`-import, not by prose reference. Prose is advisory and may not be acted on;
  `@`-imports are inlined at load.
- Memory stays per-project and isolated. `feedback_*`/`user_*` spine material
  belongs in the discernment repo as telos; `project_*` stays with its process
  and dies with it. Some existing process memory is lost in the move —
  acceptable, possibly worth a migration pass.

Decision 10 already anticipated this: *"The telos is a bleed channel by design.
Isolation is the wrong control there; hygiene rules are the right one."* The
sidecar is the mechanism decision 10 assumed but never built.

Concurrency: the sidecar is rw in every process container, so two process
sessions can write telos concurrently — dax's container-naming guard does not
help, since these are different projects. Accepted. It is strictly better than
the status quo, which is multiple Claude instances inside *one* container
writing the same discernment artifacts.

### E. What gets deleted

Delete: `_ensure_tenants_classified()`, `_prompt_tenant()`, `dax tenant
classify`, `dax tenant set`'s `.dax-tenant` writing, `.dax-tenant` files,
`DAX_MULTI_TENANT`/`DAX_TENANT_SUBDIR`, `multi_tenant`/`tenant_subdir` registry
fields, `resolve_for_cwd()`, `dax-creds resolve-tenant`, and **Gate B in the
`claude` wrapper**. Gate B's refusal existed to stop a session starting
somewhere that would pool state; with one project per container and one mount,
there is nothing to mis-resolve and nothing to refuse.

Keep: `find_enclosing_project()` and Gate 0 — not tenant machinery; it stops
`dax run` from a subdirectory silently auto-registering a new project, which is
an independent bug. `dax tenants` stays as a flat enumeration.
`all_tenant_projects()` collapses to reading the registry.

Roughly 68 tests across `test_dax_creds_tenant.py` and `test_dax_tenant_cmds.py`
go with the deletions.

### F. `claude_tenant_state` stays opt-in for now

Not flipped to default-on. The 2026-07-28/29 outage forced a fallback to the old
shared-mount model to get a session running at all, and that escape hatch is
worth keeping until the new path has been exercised. Also still gated on the
shared `plugins`/`commands` migration, without which an opting-in project loses
access to already-installed plugins.

### G. `tenant` is set through `dax env set`, not `dax tenant set`

**Revised 2026-07-30.** The original plan routed tenant-setting through
`dax init` plus a surviving `dax tenant set`. Both parts changed once the
command surface was looked at directly:

- **`dax init` is the wrong tool for changing one field.** `_run_init` does
  detect an existing project and offer "Update it?", but then walks image →
  credential checkbox → new-credential loop before reaching anything else. That
  is a lot of confirmation for a one-string edit, and every pass is a chance to
  disturb something unrelated. A tenant prompt still belongs in `init` for
  *registration*; it is not the path for *editing*.
- **`dax tenant set` is retired rather than repointed.** It wrote `.dax-tenant`
  files, which no longer exist. Reusing the name for a different mechanism would
  preserve nothing but muscle memory.

**Built 2026-07-30** — singular `env` acts on one environment, matching the
existing `tenant`/`tenants` singular/plural split:

- `dax env show [name]` — full registry entry, plus the resolved container name
  and running state, the state-tree path and whether it exists, and a warning
  when the entry still carries deprecated `multi_tenant`/`tenant_subdir` keys.
- `dax env set [name] <field> <value>` — one field, no wizard. Fields are
  `tenant`, `dir`, `image`, `creds` (comma-separated, replaces the list, and
  validated against `~/.dax.yaml`'s `credentials` before writing). `--help`
  lists every field with a description, and an unknown field error reprints the
  same table.

`name` is optional in both and defaults to the env containing the cwd, falling
back to the *enclosing* project so it also works from a subdirectory — where
`dax run` itself refuses (Gate 0). `ENV_FIELDS`/`env_field_help()` live in
`dax_creds/config.py` rather than `init.py` so building the argument parser
does not import keyring on every `dax` invocation.

Deliberately not settable: `multi_tenant`/`tenant_subdir` (being deleted — not
worth teaching), and `features` (top-level in `~/.dax.yaml` or in a
project-local `.dax.yaml`, not a registry field).

**Prerequisite, now met:** `save_config` used pyyaml, so every write deleted all
comments and alphabetised all keys — including the commented-out
`#- claude_tenant_state` toggle, which is configuration rather than decoration.
`dax env set` is a routinely-invoked writer, which would have made that much
worse. Fixed 2026-07-29 with ruamel round-trip loading and saving; `save_config`
now refuses outright when ruamel is unavailable rather than falling back and
trampling, while reads still fall back so `dax run` never breaks.

### G2. `dax envs list` absorbs the tenant view — built 2026-07-30

`dax tenants` was registry-driven by design, deriving its rows from
`~/.dax.yaml` plus `.dax-tenant` files. Once tenant is a declared field, its
rows become 1:1 with `dax envs list`, which is already the richer view (it also
knows about running containers). Keeping both means answering the same question
twice.

The merged `dax envs list` walks **both** the registry and
`~/.local/state/dax/tenants/`, as an outer join, because an env can be orphaned
from either direction:

| condition | meaning |
| --- | --- |
| registry entry, no state tree | declared but never launched, or its state was deleted |
| state tree, no registry entry | true orphan — deregistered or renamed, transcripts and memory still on disk |
| both, `dir` missing | repo moved or deleted, state stranded |

That second row is what no previous command could show, and it is exactly what
decision A's deletion-grouping rationale needs. TENANT, STATE, and USED columns
added — STATE the tree's size on disk, USED a compact relative age (`now`, `3d`,
`5w`, `1y`). Size and recency answer different halves of "is this worth
keeping": a large tree nobody has opened in a year is a better deletion
candidate than a small one from this morning. Both come from a single walk
(`_tree_stats`), so recency is effectively free.

USED is the newest *file* mtime, not the directory's own — a directory's mtime
only moves when entries are added or removed, so editing a transcript in place
would otherwise read as ancient.

Status markers: `*` running, `!` directory missing, `?` orphaned tree, with a
legend printed for whichever markers appear.

A registry row **claims** its matching tree, so whatever survives the join is by
definition an orphan. Only a *declared* tenant can claim one — with `tenant`
unset there is no path to look under, so a tree bearing that project's name
stays unclaimed and is reported. That is the honest reading: the registry no
longer says which tenant it belongs to.

`state_trees()` / `state_tree_path()` live in `dax_creds/config.py` and are
filesystem-driven by design, unlike the registry-driven listing they replace.

Caught while testing: the original `run_envs_list` returned early on an empty
registry, which would have hidden every orphan in exactly the case where
*everything* is one — deregistering all projects. The empty check now runs after
the join instead of before it.

Naming settled: **env and tenant are different cardinalities, not synonyms.** An
env is one repo, one container, one state tree — the row. A tenant is a grouping
label that can span several envs — a column, plus a `--tenant` filter. So the
command stays `envs`.

Related, unfixed: `_run_init` finds an existing project by exact `dir == cwd`
match, the same blind spot `find_enclosing_project` closed for `cmd_run`, so
`dax init` from a subdirectory still registers a duplicate.

### Outstanding after this redesign

- Reinstall dax on the host and rebuild the image — gates both the ruamel fix
  and the still-pending classification-flow retest on the real repos.
- Enforce fresh-login-per-project. **Login path fixed 2026-07-30 (see C3);
  `dax creds add` still outstanding** — its claude path imports from
  `~/.claude/.credentials.json` with no such guard, so minting a *derived* name
  that way would still share a grant.
- Teardown write-back to Keychain (deferred by choice — see C).
- ~~`dax creds add`'s overwrite early-return~~ — **fixed 2026-07-30.** Every
  `_setup_*_credential` takes `replace=`, set only on a confirmed "Overwrite
  it?", so the provider's store is actually reached. A confirmed replace that
  finds nothing new now says the old secret survived, rather than printing the
  ordinary "no token found" and leaving the user believing it was cleared.
  `dax creds remove` was fixed in the same pass to drop the config entry as
  well as the Keychain secret, refusing if any project still references it.
- Remove the debug scaffolding: `~/.dax-debug/open-url.log` in the
  `dax-creds-open-url` wrapper and the `~/.dax-debug` mount in `_LOGIN_PROVIDERS`.
- Retire `dax tenants` and `dax tenant classify`, now that the merged
  `dax envs list` (G2) supersedes them.
- Prompt for `tenant` during `dax init` registration (editing is now covered by
  `dax env set`).
- Shared `plugins`/`commands`/`cache` migration — reshaped by B into nested
  mounts inside the `~/.claude` tree mount.
- Execute the deletions in E.

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

### Confirmed 2026-07-30 — post-rebuild manual pass, steps 0–2

Sequence and full expectations in
`docs/testing/2026-07-30-cred-management-test-sequence.md`.

**Step 0 (container), passed twice in separate containers.** 342 tests pass;
installed `dax_creds` byte-identical to the repo; `/usr/bin/claude` identical to
`dax_creds/wrappers/claude`, so inject-if-absent shipped; `ruamel.yaml`
importable; and `DAX_CREDS_CLAUDE=claude-personal-dax` in a live container —
C2's derived naming resolves end to end, from `~/.dax.yaml` through `dax run`
into the container environment.

**Step 1 (host), passed.** Editable reinstall picked up ruamel 0.19.1 (host is
Python 3.9; the container is 3.14). `dax env set dax tenant personal` rewrote
`~/.dax.yaml` byte-identically — empty diff, comments and the commented-out
`#- claude_tenant_state` toggle intact. This is a real round-trip proof rather
than a short-circuit: `run_env_set` calls `save_config` unconditionally
(`init.py:649`), so a same-value set still serializes and rewrites the whole
file while leaving its content unchanged. Any diff would therefore be pure
round-trip damage.

**Step 2 (host), passed, with findings.** `stored: yes` for all three derived
Claude names; `claude-fre` shows used by **nothing**, so the resolving "used by"
column does read the typo'd credential as dead; `dax env show` prints the
resolved name; `dax envs list` reports orphans.

- **All three derived credentials already exist in Keychain, minted before the
  C3 fix.** The likeliest provenance is the abandoned-login import path, which
  copies `~/.claude/.credentials.json` under the new name. Step 3 therefore has
  a high prior of failing, and that is exactly what it is for.
- **8 orphaned state trees, all recognizable as discernment processes** —
  `a-priest-and-an-imam`, `seans-new-gig`, `self-employment-setup`,
  `ysecurity-onboard`, `american-binary-soc2`, `augmentcode`,
  `symlink-disclosure`, `hydraulic-controls-it`, under tenants `hci` and
  `ysecurity` that no project declares. These are artifacts of the abandoned
  multi-tenant classification (decision 8). Most are 0B shells; two hold real
  data (`ysecurity-onboard` 1020K, `hydraulic-controls-it` 393K). G2 was built
  to make exactly this visible, and did — every one of them was invisible to
  every previous command.
- **`dax`'s own state tree exists (122K, last used 21h) but is not in use.**
  `claude_tenant_state` is commented out and the running container has
  `.claude.json` at `$HOME`, outside the tree. Tree existence is not evidence of
  activation, in this doc's own test output as much as anywhere.

Two presentation defects, both in the same family as C4's related fix — two
views disagreeing about what exists:

- `dax env show` prints `[not registered yet]` (no `credentials:` entry) for a
  name that `dax creds list` reports as `stored: yes` (Keychain holds a secret).
  Both are true of different stores, but "not registered yet" reads as "you must
  log in". Worth distinguishing registered-in-config from stored-in-Keychain.
- `[!] key on disk` tests the single global `~/.claude/.credentials.json`
  (`ClaudeProvider().has_disk_copy()`), so it repeats identically on every
  claude row — a per-provider condition rendered in a per-credential column,
  saying nothing about which credential the disk copy is.

Unrelated, noticed in passing: `ssh-id-rsa` reports `stored: no` on both host
and container. Worth checking whether `~/.ssh/id_rsa` is still the right path.

**Step 3 (host), PASSED — and the step-2 prediction above was wrong.** 4
distinct grants across 4 credentials, no sharing, despite all three derived
credentials having been minted before the C3 fix landed. Refresh expiries are
staggered 08-27 (`claude-fre`), 08-28 (`dax`), 08-29 (`discernment`), 08-28
(`fabric`) — spread across the days the logins actually happened, which is what
separate mints look like. `~/.claude/.credentials.json` matched
`claude-personal-dax`, so every non-tenant-state container is currently riding
dax's grant.

Limit of this result, now recorded in the script's docstring: the refresh token
is a **proxy** for grant identity, since the blob carries no grant ID. A copy is
caught only while both entries still hold the same token. If each side then
refreshes independently, each gets a new token while still descending from one
grant, and the check reports PASS. These credentials predate the fix and have
been used since (access tokens now expiring 07-31), so rotation was possible.
The staggered refresh expiries are what make this PASS read as genuine rather
than a rotation artifact — inference, not proof. Consequence for the sequence:
run step 3 **immediately after** minting in step 5, where the signal is
strongest, not only on the multi-day soak.

**Correction, from step 4's mint (recorded in C5 above): the staggering inference
is weaker than stated here.** A `claude-personal-fabric` grant minted on 2026-07-30
reported `refreshTokenExpiresAt` of **2026-08-27** — *earlier* than grants
already in Keychain (08-28, 08-29), where a fixed ~30-day window would have put
a same-day mint near 08-29. So that field does not reliably encode mint date and
cannot corroborate separate mints. What does carry the claim is a grant hash
**changing across a known-good login**, which step 4 observed directly.

**Step 4 (host), passed — recorded in C5 and C6 rather than here**, since it
produced a code change and a new finding rather than only a result. In short: it
found C5 (login refused every derived name), the fix was made and confirmed live,
C4's Chrome profile and the device-flow redirect were both verified, a real mint
produced a new grant, the abandoned-login refusal was confirmed on a second
cancelled run, and inspection turned up C6.

Steps 5–8 not run: step 5 is moot (step 3 found no sharing to repair, and step 4
effectively re-minted fabric), step 6 is pending, and steps 7–8 are gated on
decision B, without which per-env credentials remain inert at runtime.

### Confirmed 2026-07-31 — decision B steps 1-3 live on `fabric`

Built and verified against the real env, in this order: the feature rewritten to
mount at `~/.claude`, Gate B stripped from the wrapper, and `claude` /
`claude_tenant_state` made mutually exclusive. Deletions (E) deliberately left
until after this ran.

`dax -t run` from `~/fatsec/fabric` emitted exactly the shape B specifies —
`adding claude_tenant_state` rather than `adding claude`,
`CLAUDE_CONFIG_DIR=/home/dfarrow/.claude`, the tree at
`~/.local/state/dax/tenants/personal/fabric` mounted **at** `~/.claude`, the two
host directories nested inside it, no wholesale `~/.claude` mount, and none of
`DAX_TENANT_STATE`/`DAX_PROJECT_NAME`/`DAX_MULTI_TENANT`.

**No image rebuild was needed, by design.** The image still carried a wrapper
containing Gate B, which is gated on `DAX_TENANT_STATE` — so `dax run` not
setting that variable leaves the old gate dormant and the handed-down
`CLAUDE_CONFIG_DIR` honoured. Confirmed live: the credential landed in the tree,
not at a `~/.local/state/dax/...` path computed in-container.

**Persistence across teardown confirmed.** The container was destroyed and
recreated and the session history survived — which is the whole point of B, and
closes the regression it records: `.claude.json` had been discarded on every
teardown since the step-2 diagnostic removed it from `dotfiles.rw` on 2026-07-27.

Two things found on the way:

- **The per-env feature opt-in was never wired up.** Features came only from the
  global `features:` list, a `.dax.yaml` in cwd, and `-f`. An env's own
  `features:` list — named by this doc, CLAUDE.md, and the feature's own docstring
  as *the* way to switch `claude_tenant_state` on for one project — was read by
  nothing. Fixed, with dedupe, since a duplicate emits the same mount twice and
  Docker rejects it. `features` also became a `dax env set` field, validated
  against the real feature list because a misspelled feature is silently ignored
  at launch — the same failure mode a typo'd credential name had.
- **Per-env features are additive only**, so an env cannot decline a globally
  inherited feature. Switching one env therefore takes four writes. Deferred
  fit-and-polish: let `claude_tenant_state` *supersede* an inherited `claude`
  with a printed note, while still refusing when both are listed explicitly on
  the same env. See CLAUDE.md Backlog.

### An orphan grant was sitting in the state tree

Found while pre-flighting the above, and it is the reason to keep the pre-flight
step. `~/.local/state/dax/tenants/personal/fabric/.credentials.json` existed with
an mtime of 2026-07-28, left by the multi-tenant testing. Its grant —
`4e6854d7062f` — matched **none** of the four credentials in Keychain.

Access token three days expired, refresh token valid until 2026-08-24. So
inject-if-absent would have left it in place (correctly, by its own rule: the
file was non-empty), Claude Code would have refreshed it, and `fabric` would have
run indefinitely on a live OAuth grant that:

- no Keychain entry backs, so nothing could re-bootstrap it if the file were lost
- `DAX_CREDS_CLAUDE` does not name
- `tests/manual/check_claude_grants.py` cannot see

Removed; the wrapper then injected `56b0ef07fcbe` from Keychain, verified
byte-identical to `dax-creds get claude-personal-fabric` inside the container.

**Consequence — the grant audit has a blind spot.** `check_claude_grants.py`
enumerates names derivable from `~/.dax.yaml` and reads Keychain. It never looks
in the state trees, which under B is exactly where every env's working credential
now lives. Two trees could hold the same grant, or an orphan grant could persist
for months, and it would still report PASS. It should also scan
`~/.local/state/dax/tenants/*/*/.credentials.json`, report each tree's grant, and
flag any that match no Keychain entry or that duplicate another tree's — the same
one-grant-per-name invariant (C6), applied where the credentials actually live.
Not yet built.

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
