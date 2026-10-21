# `dax env` lifecycle consolidation

**Status:** Steps 1–8c built. 41 decisions:
1–24 from 2026-10-18; 25–37 plus several in-place corrections from a
pre-implementation risk review on 2026-10-19; 38 on 2026-10-20; 39–41
on 2026-10-21.

**Branch:** `feature/env-lifecycle-consolidation` (off `master`, rebuilt
2026-10-18 after PR #10 merged — see note on `auto_enter` below).

**Date:** 2026-10-18

**Note on `auto_enter`:** `63201e4` (disabling questionary's `auto_enter` on
confirm prompts) was committed and pushed directly to `master`, unprompted,
ahead of pull request 10 — a mistake, corrected per the user's standing
git-workflow rule. It conflicted with that PR (which independently touched
the same function, adding `_flush_stdin()`), so it was reverted from
`master` (`18f6c40`) before the PR merged. This branch was then rebuilt
from `master` *after* that merge, so it does **not** carry the fix —
confirmed live, `grep auto_enter dax_creds/init.py` on this branch returns
nothing. Decision 8 below is therefore back to an open item for this
branch's own work, not something already landed.

## Context

Raised while working CLAUDE.md's "collapse `dax process` into `dax env`"
backlog item. That question kept pulling in bigger ones:

- Where should `destroy`/`archive`/`restore`/`export`/`import` live?
  (Decisions 1–3.)
- Should `dax init`, `dax process new`, and `dax run`'s own
  auto-registration fallback really be three separate things? (Decision 4
  — they're not, anymore.)
- Once that's settled, do `dax env`'s existing `show`/`set`/
  `accept-shared-files` still earn their place? (Decisions 13–14 — two out
  of three didn't.)
- Is the shared-`claude`-mount feature and its drift-tracking sync still
  load-bearing, given how the user actually runs dax today? (Decisions
  12–13 — no.)

The throughline: rationalize dax's CLI and feature surface against actual
usage, not preserve structure that made sense for an earlier way of
working.

## Decisions locked in

1. **"process" is not a real concept in dax's own data model.** There is no
   `is_process` flag; it's 100% derived from `substrate` being in an env's
   `features`. Every command that operates on "processes"
   (`destroy`/`archive`/`restore`/`export`/`import`) already treats every
   registered env identically and never branches on substrate — confirmed by
   reading each implementation, not assumed. "Process" is a term of art
   borrowed from virgil/discernment's own vocabulary (a scaffolded unit of
   work: `context.md`/`inbox.md`, wired to the substrate's shared skills) that
   leaked into dax's CLI naming during the discernment-migration period and
   never got revisited once the command set generalized past it.

2. **`destroy`/`archive`/`restore` move from `dax process` to `dax env`** (not
   five commands — see decision 3). They were never process-specific; the
   naming was actively misleading (implying substrate-only, when they work
   on any registered env — found live when `archive`/`restore` were needed
   for `dax`/`fabric`, both plain envs).

3. **`export` and `import` are not surviving as separate commands —
   `archive` absorbs `export`, `restore` absorbs `import`.** Traced the
   actual code in `dax.py` (on `master`, since PR #10 merged):
   `archive`/`export` already share their write core
   (`_write_process_archive`) and differ only in target count (one vs.
   batch/`--all`/picker), destination (fixed dated `backup_dir/daily/` vs.
   an arbitrary chosen path), and `--remove` (archive only). `restore`/
   `import` *don't* share their core — `restore` has
   `_extract_process_archive` as a proper shared helper, but `import`
   carries its own near-identical duplicate instead of calling it. Rather
   than preserve four verbs that only differ by one or two yes/no questions
   the user has to already know the answer to before picking a verb, those
   questions become wizard prompts, and `destroy` — which has no sibling to
   fold into — is unaffected:

   - **`dax env archive`:** pick env(s) (name(s), or a checkbox picker) →
     "save to dax's managed backup location, or to a specific file?" (the
     archive-vs-export distinction, now a prompt) → (managed only;
     no credential prompt, per decision 29) also remove after success? → summary → confirm.
   - **`dax env restore`** (refined by decisions 26–27: `cwd` must be
     empty and unregistered, restore never overwrites anything, and the
     in-place-over-a-registered-env case described below no longer exists;
     a rollback is `destroy` then restore): single target only, always, and the destination
     is always `cwd` — no batch/`--all` (confirmed with the user: never
     needed in practice), and no separate in-place-vs-move prompt. `cd` to
     wherever you want the result and run the command; if that's the
     original location, that's what used to be called "in-place," and if
     not, that's what used to be called "move" — same mechanism either way,
     matching the bootstrap wizard's own cwd convention (decision 5) rather
     than inventing a separate one. Flow: "restore from dax's managed
     backups, or from a specific file?" → (managed) pick which env's backup
     by name/date → (specific file) prompt for the path, read its manifest →
     extract into `cwd` either way, with a plain confirm noting the original
     manifest `dir` when it differs from `cwd` (a sanity check, not a
     branch) → summary → confirm. `archive`'s batch support is unaffected —
     "back up everything on the machine" is a real, different need from
     restoring one specific thing.

   No flags on either (same reasoning as decision 6). Side effect: resolves
   the `import`/`restore` duplication for free — only one extraction path
   survives.

4. **No `dax init`, no `dax process new`, no `dax env new` either.** All
   three collapse into one bootstrap flow, triggered the way `dax init`
   already partly is today: implicitly, when `dax run` hits a `cwd` that
   isn't registered. There is no separate explicit command for it anymore.
   `dax init`'s only other reachable behavior — re-running the wizard against
   an *already*-registered project to redefine multiple fields including
   inline credential creation — is confirmed unused in practice and is not
   being preserved; the equivalent (rare) need is `dax creds add`, then
   hand-editing the env's `creds:` in `~/.dax.yaml` (`dax env set` is
   dropped too, per decision 14), not a dedicated command.

5. **The env name always comes from `cwd.name`.** Never prompted, never a
   separate CLI argument (today's `dax process new <name>` requires it
   up front with no prompt fallback at all — the one field in that wizard
   that didn't follow its own "flag or prompt" rule). You make the directory
   and `cd` into it; dax names the env after it.

6. **No CLI flags for any bootstrap field.** `process new`'s
   `--dir`/`--tenant`/`--substrate`/`--title`/`--type`/`--git`/`--no-git`/
   `--dry-run` are **not** carried over onto `dax run` or anywhere else.
   Confirmed with the user: real usage is "cd in, run dax, answer the
   wizard" — flags would never actually get used. Fully interactive, every
   time.

7. *(Credential and tenant handling superseded by decision 35: tenant is
   always asked and required, the credential checkbox lists only existing
   non-Claude credentials, and there is no define-new loop. Only the
   substrate questions below still distinguish the branches.)*
   **One substrate-or-not branch inside the single wizard.** Non-substrate:
   today's `init` shape (image, optional free-text tenant, credential
   checkbox + inline "define a new credential" loop). Substrate: also the
   `process-types.tsv`-validated type picker, the git-tracking question, and
   `new-process.sh` (always `--dir <cwd>` now, never a separate path) — and
   tenant becomes required+validated instead of optional, since
   `claude_tenant_state`'s state-tree path needs one.

8. **Directory guardrails on `cwd` are fatal, correctly — not a retry
   target.** `process restore`'s move-restore prompt retries on a bad
   answer because its destination is something just *typed* in response to
   a question — re-asking makes sense there. That doesn't transfer to the
   bootstrap wizard's directory check: it's always `cwd` (decision 5), fixed
   before the wizard starts, so there's no different answer to collect
   within the same `dax run` invocation if it violates a guardrail (nested
   in/colliding with an existing project, outside `$HOME`). Exiting is
   correct there — the actual fix is making the exit message useful (which
   directory, which existing project it collides with, what to do: `cd`
   elsewhere and run `dax run` again). Retry-on-bad-input still applies to
   genuinely *prompted* wizard fields — substrate path, title, type, tenant
   — where re-asking is the right response to a bad answer.

9. **`_q_confirm` should disable `auto_enter`** — still outstanding, now
   part of this branch's own work (see the status-line note above: the
   original fix was reverted off `master` to unblock PR #10, and this
   branch doesn't carry it). questionary's default commits a y/n answer on
   the keypress alone with no Enter and no chance to reconsider; every
   confirm in dax (not just this wizard) inherits that fragility. Reapply
   `auto_enter=False` in `_q_confirm` as part of this branch, this time
   merged with whatever `_flush_stdin()` behavior PR #10 brought in rather
   than conflicting with it.

10. **Spacing/structure polish is scoped to the new wizard specifically**,
    not retrofitted onto code about to be deleted: deliberate blank-line
    separation between logical sections (location/guardrails → substrate
    branch decision → type/git/credentials → summary+confirm) so it reads as
    a paced conversation, not a wall of back-to-back prompts.

11. **The confirmation summary renders via `_print_table`** (the same helper
    `creds list`/teardown tables already use), not hand-padded `print()`
    lines — today's summary pads every field name to a hardcoded 12
    characters, which only works by accident for the current field set.

12. *(Refined by decision 35: the old shared-mount `feature_claude` is
    deleted, and the name `claude` is then reused for the always-on baseline
    feature that `feature_claude_tenant_state` becomes.)*
    **The old shared `claude` mount feature is decommissioned.** Checked
    every registered env's `features:` directly — `discernment` is the only
    one anywhere still on plain `claude`, not `claude_tenant_state`.
    Confirmed with the user: `discernment` (becoming `virgil`) will never be
    `dax run` from again — it's kept registered purely as the mount source
    other substrate envs pull `wire.sh`/`process-types.tsv` from, never a
    main project. With nothing left to use it, `feature_claude` (the
    function mounting `claudedir`/`~/.claude` wholesale), the `cmd_run`
    mutual-exclusion check against `claude_tenant_state`, `claude`'s entry
    in the feature list, and `claudedir:` in `.dax.yaml.example` all go.
    (`discernment`'s own `~/.dax.yaml` entry still says `features: [claude]`
    — left alone; that's a live-config edit the user makes by hand, not
    something touched here.)

13. **`sync_claude_shared_files`'s drift-tracking is replaced by a one-time
    seed, and `dax env accept-shared-files` is deleted.** Decision 12
    removes the *only* way a `claude_tenant_state` env's host-side
    `CLAUDE.md`/`settings.json`/`settings.local.json` ever changed through
    ordinary dax use (a session running under the old shared `claude` mount
    writing to the real host file). The user also confirmed they never
    hand-edit those host files directly, and may delete them outright at
    some point. With neither trigger for "the host moved on" left, the
    three-way hash comparison (host vs. tree vs. last-synced manifest) and
    the drift warning it exists to raise have nothing left to detect — and
    `accept-shared-files`, built specifically to resolve that warning, has
    nothing left to resolve.

    Checked before dropping it entirely, though: the host `CLAUDE.md`
    specifically carries one load-bearing instruction (the "save context
    locally unless told to save globally" rule) that actively prevents a
    real, previously-hit failure mode (cross-project bleeding into the
    global file). The substrate can't substitute for it — by the user's own
    framing, a coding project never wires in the substrate at all, so
    substrate-held shared context never reaches it. Losing this entirely
    would mean every fresh `claude_tenant_state` coding env starts with no
    default protection against that exact mistake.

    Resolution: keep seeding `CLAUDE_SHARED_FILES` into a tree, but reduce it
    to the simplest possible version — copy each file in once, only if it
    doesn't already exist in the tree, and never touch it again afterward.
    No manifest, no hashing, no drift detection, no warning, no command to
    resolve anything. A tree's copy is now purely its own accumulated state
    from first launch onward, exactly like every other file in the tree.

    **Known gap, deliberately deferred (reviewed 2026-10-19):** with
    copy-once, an edit to the host's global `CLAUDE.md` (the sanctioned
    "draft it here, apply it on the host" path) never reaches an
    already-seeded env. Considered and rejected for now:
    - **"Update when the tree's copy is untouched":** gains nothing for
      substrate envs. `wire.sh` wires the substrate into the tree's
      `CLAUDE.md`, so that copy always differs from the host's, would read
      as container-edited, and would never update.
    - **"Host always wins":** would clobber that wiring.

    The user says global `CLAUDE.md` edits are rare, so this isn't pressing.
    Revisit if it starts to bite; any fix must handle substrate-wired
    `CLAUDE.md`s, not just plain trees. Until then, a host edit that matters
    has to be copied into each tree by hand.

14. *(Per decision 35, `show` displays the env's implied
    `claude-<tenant>-<env>` credential, since there are no bare `claude`
    entries to resolve any more.)*
    **`dax env set` and `dax env accept-shared-files` are dropped; `dax env
    show` survives.** Checked each of `env`'s
    three existing subcommands against actual usage rather than assuming
    all three earn their place just because decision 2 is moving three more
    in alongside them:
    - `show` stays — it surfaces things not retrievable from `~/.dax.yaml`
      at all: whether `dir` exists, whether `mounts`/`substrate` are
      configured but the matching feature isn't active, the *resolved*
      derived credential name for a bare `claude` entry, whether the
      container is running, the state tree's real size/age.
    - `set` is dropped — confirmed with the user: real day-to-day edits
      (mounts, creds, features) already happen by hand-editing
      `~/.dax.yaml` directly, every time. `set`'s only real value over that
      is typo validation (features against the known list, creds against
      what's defined) — value that was never actually being captured given
      the user's real habit. Same dead-in-practice shape as `init`'s update
      path.
    - `accept-shared-files` almost went the same way on "never used it,"
      but decision 13 shows why that's different from `set`: its trigger
      condition was real and reachable (a container legitimately writing to
      its own `settings.local.json`, same as this very repo's
      `.claude/settings.local.json` did this session), it just hadn't come
      up yet. It still goes away, but because decision 13 removes what it
      was for, not because it was unused.

    Net result for `env`'s final surface: `show`, `destroy`, `archive`,
    `restore`. Four subcommands, all operating on an env that already
    exists, every one doing something hand-editing `~/.dax.yaml` can't
    replace.

15. **Internal functions get renamed to match: `_run_process_destroy` →
    `_run_env_destroy`, and the two merged functions are
    `_run_env_archive`/`_run_env_restore`.** "process" was simply the wrong
    word for these (decision 1); leaving it in the internal names would keep
    a known inaccuracy around for no reason. No extra cost — the archive/
    restore merges and the destroy move are already touching these function
    bodies and their test files, so the rename rides along for free.

16. **Clean break — no backward-compat aliases for `dax process archive`/
    `destroy`/`restore`/`export`/`import`/`new`.** Solo-maintained tool, no
    other consumers; same reasoning as the `.claude/settings.local.json`
    call. (Re-confirmed 2026-10-19: the user doesn't script dax at all,
    including from the virgil substrate, so no external caller needs the
    old names.) `dax process` disappears entirely except for `new`, nothing left
    answering to the old names.

17. **The new bootstrap function is named `_bootstrap_env`.** Private
    (leading underscore, never CLI-facing — no one but a future reader of
    `dax.py` ever sees it), so the name only has to be clear to that reader.
    Lives in `dax.py`, since it needs `_check_process_dir`/`_prompt_tenant`/
    `_read_process_types`, which already live there, not in
    `dax_creds/init.py`.

18. **`dax envs` is dropped; `dax envs list` becomes `dax env list`.**
    `env`/`envs` only split into two top-level commands because `env` was
    explicitly modeled on the `tenant`/`tenants` pair (a comment in `dax.py`
    says so directly) — and that pair is itself dead (decision 19). The
    still-active precedent to follow instead is `creds`, which never split:
    `dax creds list` is just one of `creds`'s own subcommands, not a
    separate `credses` command. One consistent rule across the whole CLI:
    the noun is the top-level command, `list` is one of its verbs.

19. **`dax tenant`/`dax tenants` are deleted outright — confirmed dead, not
    merely superseded.** Checked the actual code, not just CLAUDE.md's own
    backlog note: `dax tenant set` writes `.dax-tenant` files, `dax tenant
    classify` keys off `multi_tenant`/`tenant_subdir` project fields via
    `_ensure_tenants_classified`, `dax tenants` reads both — all three
    belong to the multi-tenant model abandoned on 2026-07-29. None of the
    14 real registered envs carry `multi_tenant`/`tenant_subdir` at all
    (confirmed earlier in this conversation). This executes CLAUDE.md's own
    already-standing "decision B step 5 — the E deletions" backlog item,
    folded into this branch rather than left as a separate pass, since the
    evidence is already in hand and nothing about it needs further
    investigation.

    One real wrinkle found while scoping this, not just a deletion:
    `_known_tenant_names()` (the pick-list `_prompt_tenant` offers during
    bootstrap) currently calls `all_tenant_projects` from
    `dax_creds/tenant.py` — the very module decision 19 deletes — looping
    every registered project through multi-tenant classification just to
    collect tenant names. That's leftover complexity from before the model
    was abandoned, never simplified afterward (the same shape as
    `run_env_show`'s legacy-field warning, which already flags
    `multi_tenant`/`tenant_subdir` as deprecated wherever it finds them).
    Since every project now carries a single flat `tenant:` field, this
    becomes a one-line set comprehension over `projects[*].tenant`, no
    classification needed. `_prompt_tenant` itself (the general-purpose
    tenant-prompting helper, used by every bootstrap flow) is unaffected —
    only a function it calls into simplifies.

20. **`ENV_FIELDS` gets a real validation pass, not just a stale-comment
    fix.** Found while explaining what it's for: the comment directly above
    the dict claims `"features" is not here either"`, while `features` is
    one of the dict's own keys two lines down — stale from before `features`
    became settable, never corrected. Fixing the comment surfaced the better
    question: should `~/.dax.yaml` actually be checked against this list?
    Verified first, not assumed — grepped every `project.get(...)`/
    `proj.get(...)`/`existing_project.get(...)` read and every
    `register_project`/`run_env_set` write across `dax.py` and
    `dax_creds/*.py`. The complete set of keys ever read or written on a
    project entry is exactly `ENV_FIELDS`'s 7 keys plus `multi_tenant`/
    `tenant_subdir` (both deleted by decision 19) — nothing missing, so
    validating against it can't produce a false positive on anything the
    codebase actually uses today.

    Built as a generalization of an existing check, not a new mechanism:
    `run_env_show` already hardcodes `legacy = [k for k in ('multi_tenant',
    'tenant_subdir') if k in proj]` and warns if either is present.
    Generalizing that to `unknown = [k for k in proj if k not in
    ENV_FIELDS]` catches the same two cases for free (no special-casing
    needed once decision 19 lands) plus any genuine typo. Also added,
    quietly, to the same pass `cmd_run` already runs on every launch for the
    backup check (silent unless something's wrong) — scanning *every*
    registered project, not just the one launching, so a typo in an
    untouched env still gets caught on the very next `dax run` regardless
    of which env that run is for. This is precisely the failure mode
    already seen once (`claud-fre` sitting unused for weeks, undetected).

    One real, ongoing cost, not a one-time fix: any future new per-project
    field must be added to `ENV_FIELDS` or it starts false-positiving on
    its own legitimate use. Judged acceptable — `substrate`/`mounts` are the
    only two fields added in this project's whole history, a low-frequency
    cost against a real, previously-hit failure mode.

21. **`show`/`destroy`/`archive`/`restore` keep different "no arguments"
    defaults, deliberately.** Raised, then left unresolved, earlier in this
    doc's own drafting: `show` defaults to the env containing `cwd`;
    `destroy`/`archive` default to an interactive picker over every
    registered env, and `restore` defaults to its source prompt and
    archive picker (decision 27). It picks among *archives*, not registered
    envs, since its target is always an empty, unregistered `cwd` (decision
    26). Decided explicitly rather than left as an
    unexamined gap: this tracks a real difference in what each command is
    for, not an accident. `show` is single-target inspection, where
    "wherever I'm standing" is the obviously useful default. The other
    three are usually choosing *among* envs you're not necessarily standing
    in — defaulting to "show me everything, let me pick" is the more useful
    behavior there. Kept as-is.

22. **`_check_process_dir`/`_process_dir_conflict` get renamed too** (e.g.
    `_check_bootstrap_dir`/`_bootstrap_dir_conflict`) — same reasoning as
    decision 15, just not originally listed there: "process" isn't a real
    concept (decision 1), so these 13 call sites carry the same known
    inaccuracy. Risk-free — pure internal Python names, no effect on the
    tar format or anything already written to `backup_dir/daily/` (that's
    decision 23, a different kind of rename with real stakes). Both
    functions are already being touched building `_bootstrap_env` (step 4)
    and the restore merge (step 3, which reuses `_process_dir_conflict` for
    its own guardrails), so the rename rides along for free.

23. **The tar manifest member is renamed to `dax-env-manifest.json`, written
    going forward, read with a fallback to the old `dax-export-manifest.json`
    name.** Unlike decision 22, this one has real stakes: the manifest
    filename is baked into every archive already sitting in
    `backup_dir/daily/`, and these files exist specifically for disaster
    recovery — "replace GitHub as the actual backup store," per this
    project's own repeated framing. Decision 16's "clean break" reasoning
    doesn't transfer here: that was about CLI muscle memory, a cosmetic
    cost; a clean break on the manifest name means an archive someone
    already has silently fails to restore the day it's actually needed,
    which is a far worse failure than a renamed command. So: write only
    `dax-env-manifest.json` from now on (`"export"` no longer describes
    either path — managed-backup or specific-file), but have the manifest
    reader try that name first and fall back to `dax-export-manifest.json`
    if absent, so every existing archive stays fully restorable.

24. **`docs/design/2026-07-27-tenant-isolation.md` gets a historical-only
    marker on its pre-redesign section only, not the whole doc.** Checked
    its actual structure (section headers), not assumed: everything before
    the "Redesign 2026-07-29" heading (line 460) — Problem, Verified
    findings, decisions 1–9 and 12 — is the original multi-tenant design,
    which decision 19 deletes outright; that part becomes pure history.
    Everything from "Redesign 2026-07-29" onward (decisions A–G2, the
    verification logs, concurrency notes, known limits including the still-
    open refresh-token-rotation gap) describes the *current*,
    unaffected-by-any-of-this architecture — `claude_tenant_state` mounting,
    per-tenant/project credential isolation, derived naming, grant-collision
    enforcement, discernment's sidecar role. Roughly 80% of the doc stays
    exactly as-is; only the pre-redesign ~20% gets an explicit "this
    section describes deleted code, kept for historical record only"
    marker at its start, not an archive move and not a deletion.

25. **An env-name collision is fatal in the bootstrap wizard, in both of
    its forms.** Decided 2026-10-19, from a pre-implementation risk review.
    Decision 5 derives the name from `cwd.name`, but the existing guardrails
    (`_process_dir_conflict`) only check *directory* collisions, never
    *name* collisions, and `register_project` merges into whatever entry
    already holds that name (`existing.update(...)`). So bootstrapping
    `~/work/dax` while `dax` is registered at `~/dax` would silently repoint
    the existing env at the new directory, and share its state tree
    (`tenants/<tenant>/dax`) and derived credential (`claude-<tenant>-dax`).
    Today's `process new <name>` can't hit this only because the name is
    chosen explicitly. Two cases, both fatal:

    - **Name already registered** (any directory): exit naming the existing
      env and its `dir`, and say to rename this directory or `cd`
      elsewhere.
    - **Leftover state tree with no registry entry**: `tenants/<tenant>/<name>/`
      already exists, e.g. an orphan (`?` in `env list`) or the residue of
      an interrupted destroy. Silently adopting it would inherit old
      transcripts, memory, and any `.credentials.json`, which is the
      untracked-grant failure already seen on `fabric`. Exit naming the
      path, and say to remove it, `dax env restore` it if it's a real env,
      or use a different directory name. This check runs right after the
      tenant prompt, since the tenant is part of the path.

    Not a re-prompt for a different name: that would break decision 5, and
    letting an env's name diverge from its directory basename is exactly
    what forced `_relocate_claude_project_key` (the `fabric`→`denim`
    `/resume` breakage). Fatal for the same reason as decision 8: the name is
    as fixed as `cwd`, so the wizard has nothing different to collect.

26. **Restore is never destructive: it only targets an empty, unregistered
    `cwd`, never overwrites a project directory or a state tree, and names
    the env `cwd.name`.** Decided 2026-10-19 (user: "restore should always
    happen to an empty folder"). Today's in-place restore over a registered
    env asks one default-No confirm and then `rmtree`s both the project
    directory and the state tree (`_extract_process_archive`). One wrong
    keypress loses the live env, which is more destruction than restore should
    ever be able to do. Consequences:

    - **Rolling back an env is two commands:** `dax env destroy <name>` (typed
      `DESTROY`, refuses on a running container), then `mkdir <dir> && cd
      <dir> && dax env restore`. All destruction lives in the one command
      built for it; restore only ever creates.
    - **Side-by-side restore comes for free:** restoring into an empty
      `~/fabric-check` registers `fabric-check` next to the live `fabric`,
      untouched. This is useful for inspecting an archive before committing to
      a rollback.
    - **Name from `cwd.name`**, matching decision 5. `_relocate_claude_project_key`
      handles the archived basename differing. Decision 25's collision checks
      apply unchanged: a name registered elsewhere, or a leftover
      `tenants/<tenant>/<name>/`, is fatal.
    - **Code deletions:** the restore-over-registered branch and its
      prompt go away, and so does the separate running-container concern (restore can no
      longer touch an existing env). The `rmtree` calls in
      `_extract_process_archive` are replaced by refusals (target dir must be
      empty, dest state tree must not exist), enforced in the core itself, not
      only in the wizard, so no caller can ever reach a deletion.
    - **"Empty" ignores `.DS_Store`** (confirmed with the user). The host is
      macOS, where Finder can drop one into a folder just from opening it.
      Any other entry, hidden files included, refuses.

27. **Archive selection: env first, then date, with older archives reachable.**
    Decided 2026-10-19. `_available_archives` currently keeps only the newest
    archive per env name, so the managed picker can't reach an older date.
    That's the wrong default exactly when restore matters most: break an env,
    archive it (e.g. a routine `archive --all`), and the newest archive is the
    broken one. New flow:

    1. Source: managed backups, or a specific file.
    2. Managed: pick the env (one row per archived name, showing its newest
       date and archive count, and pre-highlighted when it matches `cwd.name`,
       the rollback case), then pick the date (every archive for that env,
       newest first and highlighted, so one Enter takes the common case).
       `_available_archives` returns all archives per name, not just the
       newest.
    3. Specific file: a path prompt with completion (`questionary.path`),
       manifest read from the file.
    4. Summary via `_print_table`: source archive and date, archived name
       and original dir, new name (`cwd.name`), new dir (`cwd`), tenant,
       state-tree path. Then confirm.

    **Positional shortcut kept:** `dax env restore <name>` jumps straight to
    that env's date list, `dax env restore <path>` straight to the summary.
    It's a single optional positional, not a flag, so it isn't what
    decision 6 rules out. With no argument, the wizard is unchanged.

28. **A restore under a different name rewrites every path-keyed record,
    not just `projects/<key>/`.** Decided 2026-10-19. When `cwd.name` (N)
    differs from the archived name (A), `_relocate_claude_project_key` today
    renames only the `projects/<mangled-cwd>/` directory. Checked against a
    real tree: two more files key on the absolute container path:
    `.claude.json`'s `projects["<home>/<A>"]` (trust accepted,
    `allowedTools`, MCP enablement) and every `history.jsonl` line's
    `project` field (prompt-history recall). Left as-is, a renamed restore
    re-prompts for trust, drops granted permissions, and starts with empty
    prompt history. This is a live bug in today's move-restore too. Fix:
    rewrite all three, matching only exact `<home>/<A>` values, never
    substrings. Tests for each, plus a same-name no-op.

    **Open, to be settled by the step 3 manual check:** session transcripts
    (`projects/<key>/*.jsonl`) also carry `cwd: <home>/<A>` on every line,
    and this decision doesn't rewrite them. It's unknown whether Claude Code
    cares when resuming a renamed session. The runbook's step 3 resumes one
    and sends a prompt; if that misbehaves, extend the rewrite to
    transcripts.

29. **Credentials are removed from archives entirely.** Decided 2026-10-19,
    superseding an interim "drop the archived credential only when the
    restore name differs" call made minutes earlier. Two findings:

    - **The option almost never captures anything useful.**
      `_sync_and_clear_claude_credential` writes a stopped env's tree
      credential back to Keychain and deletes it from disk, and archive
      refuses on a running container. So a normally stopped env has no
      `.credentials.json` to include (archive's own `--explain` calls its
      absence "normal"). The only time it catches a file is when that
      teardown failed, i.e. a crash or a dead credential, which is exactly
      what should never be restored. Even a captured live token is likely
      dead by restore time, having rotated since, and is shared with the
      original env if that env still exists.
    - **The real `backup_dir` is OneDrive.** A captured credential is a
      plaintext account refresh token in a cloud-synced folder with version
      history, so deleting the archive later doesn't reliably remove it.

    The gain was skipping one browser login, and a grant is trivially
    re-mintable. So archive never includes `.credentials.json`, and the
    include-credential prompt is gone. Restore always strips any
    `.credentials.json` from an older archive's state tree before moving it
    into place, and warns that the archive contained one (the user's cue to
    delete that archive from OneDrive). Every restored env does a fresh
    login on its first `dax run`. Deletes `_state_tree_tar_filter`'s
    include branch and every `--include-credential` test.

30. **`--explain` survives decision 6; `--dry-run` does not.** Decided
    2026-10-19. Decision 6's "no flags" is about not supplying wizard
    *answers* on the command line. `--explain` answers nothing: it's
    long-form documentation of what a command captures and where it writes,
    and `dax backup --explain` already redirects users to archive's. So it
    stays on `dax env archive` and `dax backup`, and is added to
    `dax env restore`, whose rules (empty unregistered `cwd`, name from
    `cwd.name`, credentials stripped) now warrant one. `--dry-run` is
    dropped from every command, `destroy` included. Every merged flow ends in
    a summary table plus a confirm before touching anything (`destroy`'s is
    typed `DESTROY`), so declining that confirm is the preview. Its only
    extra value is a non-interactive preview, which these commands are never
    used for. Its tests are deleted, or converted to "decline the confirm,
    assert nothing changed."

31. **Restore keeps archived feature names that no longer exist, but
    commented out, and never half-restores.** Decided 2026-10-19. Archives
    outlive the code (same reasoning as decision 23): any archive recorded
    before decision 12 may list `claude` in its manifest `features`, and so
    will any future feature removal. Today restore writes features back via
    `run_env_set(..., valid_features=_feature_names())`, which raises on an
    unknown name. It does so *after* extracting the project dir and state
    tree and saving a partial registry entry, leaving a half-restored env
    plus a traceback. Fix:

    - Check manifest `features` against `_feature_names()` in the summary
      phase, **before anything is extracted.** Unknown names appear in the
      summary as "no longer exists in dax, kept commented out", and the
      restore continues.
    - Write each unknown name into the env's `features:` list in
      `~/.dax.yaml` as a **commented-out entry** (e.g. `#- oldfeature`), matching
      the file's existing toggle convention (`#- claude_tenant_state`).
      This goes through ruamel's comment API on the `CommentedSeq`, so the
      record of what the env used to have survives without being active. If
      every feature is unknown, `features:` is an empty list carrying the
      comments.
    - All registry writes after extraction (dir/image/creds, tenant,
      features, mounts, substrate) go through **one** `save_config` call,
      not several, so a failure can't leave a partly written entry.

    Tests: an old-style archive with `features: [oldfeature, mounts]` (a
    name that is not, and never was, a dax feature) restores with `mounts`
    active and `#- oldfeature` present in the saved YAML, and a forced
    failure before the save leaves `~/.dax.yaml` untouched. **`claude` and
    `claude_tenant_state` are not examples of this:** decision 36 handles
    them as known-redundant names, dropped silently rather than kept
    commented out. (`claude` is also a valid feature name both before step
    7, as the old shared mount, and after it, as the baseline.)
    **Single save, concretely:** `run_env_set` calls `save_config` on every
    call and can't write ruamel comments, so restore (and the bootstrap
    wizard) build the whole entry in memory and call `save_config` once,
    not `register_project` plus a chain of `run_env_set` calls.

32. **The Claude login container writes into a fresh temp folder, never
    the host's `~/.claude`.** Decided 2026-10-19, folded into this branch
    at the user's call (step 7). `cmd_creds_login` runs from manual
    `dax creds login`, `dax creds add`'s hand-off, `dax run`'s "not in
    Keychain, log in now?", and `dax run`'s proactive relogin, so it mostly
    runs automatically at launch. `_LOGIN_PROVIDERS['claude']` mounts the
    host's real `~/.claude` rw. That's a leftover from before state trees
    (`c33faa4`, 2026-06-22, when every container shared it), never
    revisited when per-env trees arrived. `_post_login_import` copies the
    token into Keychain but, unlike Auggie's branch
    (`_clear_auggie_session`), never deletes it, so the host's `~/.claude`
    always holds a plaintext copy of the most recently minted env grant.

    New flow (Claude only; GitHub keeps mounting `~/.config/gh`, which is
    the host `gh`'s real, long-lived config):
    1. Sweep leftover folders under `~/.local/state/dax/login/` (from a
       previous run killed before cleanup), **but only ones older than an
       hour**. Two `dax run`s relogging in at once must not delete each
       other's in-progress folder. Then create a fresh `0700` folder
       there. This is under `/Users`, which Docker Desktop always
       file-shares; Python's default temp location (`/var/folders/...`)
       depends on the user's Docker Desktop settings.
    2. Mount it at the login container's `~/.claude`, the same container
       path as today, so `claude auth login` sees no difference.
       `~/.dax-debug` stays mounted as before.
    3. After the container exits, read `.credentials.json` from the folder
       via `import_from_disk(..., credentials_file=...)` (already
       supported), apply the grant-collision check, store in Keychain.
    4. Delete the folder in a `finally:`, so cancelled and failed logins
       are cleaned up too.

    The `before` snapshot becomes trivially "nothing" (the folder starts
    empty), so an abandoned login is simply "no file written". The env still
    receives the token the way it does on every launch: the `claude` wrapper
    injects it from Keychain into an empty tree. Considered and rejected:
    mounting the env's own state tree (the GitHub pattern). It needs
    credential→env mapping (ambiguous for explicit names), overwrites the
    tree's existing credential, and needs a running-container guard. It also
    leaves a token on disk after a manual login unless deleted, at which
    point it's just a worse temp folder.

    **Related, for step 4** *(the wizard part is superseded by decision
    35, which removes the wizard's credential-definition loop entirely; the
    deletion of `_setup_claude_credential` still stands, see step 4)*:
    `_setup_claude_credential`
    (`dax_creds/init.py:250`), reachable today only from `dax init`'s
    inline credential loop, imports straight from the host's
    `~/.claude/.credentials.json`. Once logins stop writing there, that
    file only gets staler. The bootstrap wizard's "define a new credential"
    loop must hand Claude credentials off to the login flow (as `dax creds
    add` already does, decision C8), never import from disk, and
    `_setup_claude_credential`'s disk-import path is deleted.

    Needs a manual check (unreachable from the suite) that `claude auth
    login` is happy with an empty config folder. It's a bare auth
    subcommand, so expected fine. The leftover token already sitting in the
    host's `~/.claude/.credentials.json` is the user's to delete by hand.

33. **A renamed restore replaces explicitly named Claude credentials with
    bare `claude`.** Decided 2026-10-19. The archive manifest records
    `creds:` verbatim (`_write_process_archive`), and restore registers it
    verbatim. A bare `claude` is safe: it derives
    `claude-<tenant>-<new name>`. But an explicit name, e.g. `ot`'s
    `claude-ysecurity-ot`, would leave the restored copy and the original
    pointing at one Keychain entry. Both would inject one grant, and the
    first to refresh logs the other out. The grant-collision check can't
    catch it, because there's only one name, and a relogin in the copy
    would replace the original's credential. So when `cwd.name` differs
    from the archived name, every Claude-provider credential in the
    restored `creds:` becomes bare `claude`, shown in the summary as a swap
    (`claude-ysecurity-ot` → `claude`, derives `claude-ysecurity-<new>`).
    The restored env does a fresh login on first `dax run`. Same-name
    restores keep `creds:` unchanged. Non-Claude credentials (github etc.)
    are user-level and are never touched. Tests: renamed restore of an
    explicit-Claude-cred archive registers bare `claude`; same-name restore
    keeps the explicit name; github entries are untouched either way.

34. **Archive reads each tarball back before removing any env.** Decided
    2026-10-19. Today removal waits only for every archive write in the
    batch to finish without raising an error; nothing is read back, so a
    truncated or corrupt tarball would only surface at restore time, after
    the env it backed up is already gone. When removal was chosen, archive
    now reopens each tarball between "written" and "remove", reads its
    manifest (through the same reader restore uses, so "verified" means
    "restore can read it"), and confirms a `project/` member exists. Any
    failure cancels every removal in the batch, matching today's rule that
    one failed write cancels them all, and reports which archive failed.
    Runs only when removal was chosen; a plain archive skips it. Tests: a
    corrupt or truncated tarball in a batch blocks all removals; a good batch
    removes as before.

35. **Claude is an always-on baseline feature; every env has a tenant; the
    Claude credential is implied.** Decided 2026-10-19 (user: "i can't think
    of a time when i won't use claude inside a dax env", and "all envs should
    have a tenant value"). Supersedes parts of decisions 7, 12, 32 and 33.

    - **Tenant stays, with a real future job.** Today it only feeds the
      derived credential name, the state-tree path, and grouping; it doesn't
      pick the browser or profile (`credential_defaults.claude` applies to
      every env). The user wants to eventually steer Claude logins to
      different browser profiles per tenant. That's backlogged, not built
      here. It's **required for every env**; an entry without one is
      refused at launch with a clear message. All 14 live envs already have
      one.
    - **Claude joins `_ALWAYS_ON_FEATURES`** with `workdir`, `dotfiles` and
      `webpreview`. What `feature_claude_tenant_state` does today (mount the
      env's state tree at `~/.claude`, `CLAUDE_CONFIG_DIR`, the one-time
      seed) becomes the baseline `feature_claude`, after the old shared-mount
      `feature_claude` is deleted (decision 12). Nothing about Claude is ever
      written in `features:`. `cmd_run`'s `'claude_tenant_state' in
      features` checks (proactive relogin, teardown credential sync) become
      unconditional, and the mutual-exclusion check is deleted.
    - **The Claude credential is implied, never configured:** always
      `claude-<tenant>-<env>`, added to the env's resolved credentials by
      `cmd_run`, never listed in `creds:`. Explicitly named Claude
      credentials stop being a thing for envs. (Of the two live ones,
      `claude-ysecurity-ot` equals what derivation produces for `ot`;
      `claude-fre` does **not**, since `discernment` derives
      `claude-personal-discernment`, so `claude-fre` simply becomes an
      unused registered credential to `dax creds remove` by hand.)
      Decision 33's restore swap reduces to "drop Claude entries
      from `creds:`".
    - **Wizard (replaces decision 7's per-branch credential handling):**
      tenant is always asked, required, picked from tenants already in use
      or typed new. Other credentials come from a checkbox of **existing
      non-Claude** credentials, with no "define a new credential" loop (the
      user creates those beforehand with `dax creds add`). No Claude
      question. The coding and substrate branches now differ only in the
      substrate questions. This also removes decision 32's step-4 concern:
      there's no inline credential definition left.
    - **Note for implementation:** `cmd_run` appends per-env `features:` to
      the baseline without de-duplicating, so a baseline feature also listed
      per-env would be applied twice (duplicate mounts). How existing
      redundant entries are treated is decision 36.

36. **Redundant Claude entries are ignored, with a launch warning until
    hand-removed.** Decided 2026-10-19. After decision 35, these are
    redundant: `claude_tenant_state` in `features:` (13 live envs), the old
    `claude` in `features:` (`discernment`), and Claude-provider entries in
    `creds:` (bare `claude` in 11 envs, plus explicit `claude-ysecurity-ot`
    and `claude-fre`). dax filters these known names out wherever it reads
    an env's `features:` or `creds:`, and from **every** feature source
    (the env's own list, a cwd-local `.dax.yaml`, `-f`), so that
    `-f claude_tenant_state` can't reach `_add_feature`'s exit. That covers
    `cmd_run`, `env show` and restore, so
    they never count as unknown features (which would stop `dax run`) and
    never apply the baseline twice. As long as any env still carries one, every `dax run` prints a
    one-line warning naming those envs and entries, using the same
    quiet scan of every project as decision 20. The user removes them by hand;
    dax never rewrites the config for this. Restore drops them from old
    manifests silently. Nothing is lost, so unlike decision 31 they are
    *not* kept as commented-out entries. Tests: an env listing
    `claude_tenant_state` launches with Claude applied exactly once; the
    warning names it; it disappears once removed; an old manifest with
    `features: [claude_tenant_state]` and `creds: [claude]` restores to
    neither.

37. **A missing `~/.dax.yaml` exits with instructions; dax never creates
    one.** Decided 2026-10-19, from the second independent review. `dax
    init`'s missing-config fallback was the only thing that ever created
    `~/.dax.yaml`, and step 4 deletes it. Every command that loads config
    (`load_config`, `load_dax_config` callers such as `cmd_env` and
    `cmd_creds`) currently exits with "run `dax init` first", which would
    point at a command that no longer exists, including on the fresh-machine
    restore that archives exist for. They now exit with one shared message
    that prints the minimal `~/.dax.yaml` to create by hand (`defaults:
    {image: dax-base}`, plus where `backup_dir:` goes), so the user can then
    re-run the command. Lives in one helper so every caller says the same
    thing. Built in step 4, alongside deleting `dax init`. Test: with no
    config, `dax run`, `dax env restore` and `dax creds list` each print it
    and exit non-zero, and nothing is written.

38. **`dax config` opens `~/.dax.yaml` in an editor, keeps the previous
    version, and checks the result.** Decided 2026-10-20 (user: the
    filename is awkward to type). Since decision 14 dropped `dax env set`,
    hand-editing this file is the only way to change an env, so the command
    earns its place by what it does around the edit, not the typing alone:
    - Opens `$VISUAL`, else `$EDITOR`, else `vi`, on `~/.dax.yaml`. A
      missing file is decision 37's message, never created.
    - Before opening, copies the current file to `~/.dax.yaml.prev`
      (overwritten each time), so a bad edit is one `cp` from undone.
    - After the editor exits, parses the file the way every other command
      does (ruamel round-trip, so duplicate keys are caught). On a parse
      error, prints it and offers to reopen the editor (default yes). A
      broken file otherwise breaks every dax command until fixed.
    - Then runs the same quiet checks `dax run` does: decision 20's
      unknown-key warning and, once step 7 lands, decision 36's old Claude
      entries. Reported, never auto-fixed.
    - **Refuses inside a dax container** (revised 2026-10-21, at the
      user's call, from an earlier "warn"): there `~/.dax.yaml` is a
      single-file bind mount, and an editor that saves by write-then-rename
      silently detaches the container's copy from the host file, so a
      successful-looking edit may never reach the host. Detected by the dax
      image's own `/usr/local/bin/dax-entrypoint.sh`.

    The seventh top-level command (decision 4's six plus this), still down
    from eleven. Built as step 8b, after step 8 and before step 6's docs
    sweep, so the sweep documents it.

39. **Auggie support is removed entirely.** Decided 2026-10-21 (user: "we
    can remove the entire auggie feature support"). Deleted:
    `feature_auggie` (the `~/.augment` mount), `dax_creds/providers/auggie.py`,
    the auggie branches of `dax creds add`/`login`/`list`/`update` and the
    credential checks, the manual-login instructions, `_clear_auggie_session`,
    the image's `npm install -g @augmentcode/auggie` and `auggie` wrapper, and
    `auggiedir` in `.dax.yaml.example`.

    **Old entries are ignored and warned about, like decision 36's.** The
    live `ys-augmentcode` lists `auggie` in both `features:` and `creds:`, and
    an `auggie` credential (provider `auggie`) is registered. Rather than let
    `dax run` stop with "unknown feature: auggie", the name `auggie` in
    `features:` and any credential whose provider is `auggie` are filtered out
    wherever an env is read. Each `dax run` names envs still carrying them until
    they're removed by hand, and restore drops them from old archives. The
    Keychain secret is cleared with `dax creds remove auggie`, which needs
    no auggie code. Built as step 8c, before step 6's docs sweep.

40. *(The substrate half is superseded by decision 41: substrate stays a
    selected feature, with one global path. The mounts half stands.)*
    **`mounts:` and `substrate:` switch their own features on.** Decided
    2026-10-21 (user: "it's redundant to have to specify the feature and
    then also the mount points"). Both features read their data straight off
    the env's entry, and do nothing without it, so also listing them in
    `features:` was pure ceremony. Now a non-empty `mounts:` turns on the
    mounts feature, and a `substrate:` path turns on the substrate feature. No
    `features:` entry is needed for either, and none means none.
    - **`mounts`/`substrate` left in `features:`** are treated like decision
      36's retired names (the user's call, over accepting them quietly):
      filtered out, and named on every `dax run` until removed by hand.
      Listing one without its data still does nothing, just as before.
    - `dax env show`'s "set but the feature isn't active" notes for mounts and
      substrate are deleted, since that state can no longer exist.
    - The bootstrap wizard stops writing `features: [substrate]`.
    - Restore drops `mounts`/`substrate` from an archive's `features:` (the
      `mounts:`/`substrate:` values themselves travel as before).
    - `-f mounts`/`-f substrate` and a cwd-local `.dax.yaml` listing them
      are filtered the same way. A feature has no data to work with outside
      the env entry anyway.
    Built as step 8d, before step 6's docs sweep.

41. **One global substrate; each process opts in with `features:
    [substrate]`.** Decided 2026-10-21 (user: "the folder holding the
    substrate should be set as a global setting (so all projects share the
    same substrate) rather than having to provide the path to it every place
    it's used"). Replaces decision 40's substrate half.
    - **The path is a top-level `substrate:` in `~/.dax.yaml`.** The
      bootstrap wizard already used that key as its default answer, so this
      makes an existing convention the only one. The live config has 8
      substrate envs, all pointing at `/Users/dfarrow/fatsec/discernment`
      and none at a top-level value, so migration is one line added and 8
      per-env keys removed.
    - **`substrate` in `features:` is the opt-in** (not warned about,
      unlike decision 40's plan). An env that selects it while no global path
      is set is refused at launch, with a message saying to add
      `substrate: <path>` via `dax config`.
    - **A per-env `substrate:` key is retired:** removed from `ENV_FIELDS`,
      so it's reported as a retired key ("ignored; remove them") until taken
      out. No per-env override: one shared substrate is the point.
    - **Bootstrap wizard:** the substrate branch uses the global path. If none
      is set yet, it asks once (validated as now), writes it as the top-level
      `substrate:`, and says so in the summary; every later process uses it
      without asking.
    - **Archive/restore:** new archives stop recording a substrate path.
      Restore keeps `substrate` in features and ignores any path in an old
      manifest. If the global path isn't set, the summary says the env will
      need it.
    - **`dax env show`** shows the global path for envs with the feature.

42. **Product versioning: semver, starting at 3.0.0.** Decided 2026-10-21
    (user: "this is ... the third major revision of it ... we can just start
    this as v3.0"). Built as step 9.
    - **`VERSION` holds `MAJOR.MINOR.PATCH`.**
      - Major: something has to change by hand (a removed or renamed
        command, or a `~/.dax.yaml` change that breaks or warns about
        existing entries).
      - Minor: a new command, feature or key, or any change that affects
        the image, with old configs still working.
      - Patch: fixes only.
    - **The release:** bump `VERSION` as the branch's last commit, then put
      an annotated tag `vX.Y.Z` on `master` after the merge. Pushing the
      tag needs its own approval.
    - **`dax --version`** prints the host version. Inside the image, where
      dax is a plain pip install with no `VERSION` file beside it, it falls
      back to the installed package's version, which pip took from the same
      file.
    - **`dax build` stamps a `dax.version=<version>` label** on the image.
    - **`dax run` warns (never refuses) when the image's label differs from
      the host's version**, naming `dax build` as the fix. An unlabelled
      `dax:*` image is reported as predating versioning; an unlabelled
      custom image, or one docker can't inspect, is silent. This is the
      point of the whole decision: the in-image dax/dax_creds is frozen at
      build time, and "needs a rebuild" was otherwise only ever a note.
    - **`CHANGELOG.md`** gets one section per release. History before 3.0.0
      lives in git.
    - **No `version:` key in `~/.dax.yaml`.** The retired-name warnings
      already handle old configs; add one only if dax ever needs to tell
      config formats apart.

43. **Per-tenant browser profiles for Claude logins.** Decided 2026-10-21
    (user, at decision 35: "eventually i'll want to steer claude credentials
    to different profiles based on the tenant the env is in"). Built as step
    10, on this branch, as part of 3.0.0.
    - **A top-level `tenants:` block, keyed by tenant, then provider:**

      ```yaml
      tenants:
        ysecurity:
          claude:
            chrome_profile: Profile 5
      ```

      It is merged over `credential_defaults.<provider>`, so a tenant only
      states what differs. A browser and its profile are overridden
      together: a tenant that sets `browser` without `chrome_profile` doesn't
      inherit the default's profile. It's the first per-tenant setting, and
      the block leaves room for more.
    - **Applied where the implied credential is synthesized**
      (`synthesized_credential`, which now takes the tenant). Login, `creds
      list` and `env show` all read that definition, so they pick it up
      unchanged. An explicitly registered credential keeps its own settings,
      as it already did over `credential_defaults`.
    - **The bootstrap wizard asks only for a brand-new tenant** (in neither
      the registry nor `tenants:`). The choices are "Same as default
      (<current>)" or any browser or Chrome profile found on the machine.
      The default, chosen either way, writes nothing. Any other choice
      writes `tenants.<t>.claude`, shown in the summary and saved in the same
      single save. A tenant already in use is never asked again. `dax env
      restore` doesn't ask, even when it asks for a new tenant; set the block
      with `dax config`.
    - **Checked like env keys,** on `dax run` and `dax config`:
      - a `tenants:` entry no env uses yet (typo?). The block still applies to
        the next env given that tenant, for example after `dax env destroy`
        removed its last one;
      - a provider other than `claude`;
      - a level that isn't a map, which is ignored rather than crashing
        (`dax config` refuses a non-map `tenants:` outright);
      - a `chrome_profile` whose merged browser isn't Chrome, which would be
        silently ignored.
    - **`dax env show`** says which browser the env's Claude login opens and
      where that setting comes from (tenant or default).

44. **Changing an env's tenant: `dax config` carries its Claude state
    along.** Decided 2026-10-21 (user: "if i want to change the tenant on an
    env i have to change the tenant value in the .dax.yaml and i need to move
    the state tree. is there anything else? perhaps we should automate that").
    Built as step 12.
    - **What depends on the tenant:**
      - the state tree path `tenants/<tenant>/<env>/`;
      - the implied credential name `claude-<tenant>-<env>`, which is its
        Keychain secret;
      - the relogin record `creds-login/<name>.json`.

      Nothing inside the tree does: it's keyed by the container path
      `/home/<user>/<env>`. The env's own folder never moves.
    - **Trigger:** after a `dax config` save, every env whose `tenant:`
      changed from one value to another gets the offer "Move them to <new>?"
      (default yes). An env gaining its first tenant had nothing to move. No
      new command, and no `dax run` fallback; declining prints the manual
      steps.
    - **The move:**
      - the tree goes to the new tenant's folder, and the old tenant's
        folder is removed if it's now empty;
      - the Keychain secret is renamed, set under the new name and then
        deleted under the old: the same grant, so no login, and nothing is
        orphaned;
      - the relogin record follows;
      - a pre-3.0 shared-files manifest beside the old tree is deleted.
    - **Refused before anything moves** when:
      - the env's container is running (its tree is mounted at the old
        path, and stopping it syncs the credential back under the old name);
      - the new tree path exists;
      - the new credential name already has a Keychain secret.

      Never a merge. A failure partway through is reported step by step, not
      rolled back.
    - **Review fixes, 2026-10-21:**
      - The running check covers both the old and the new `dir:`, in case
        the same edit changed it. A `docker ps` that times out counts as
        "can't tell", and refuses.
      - The old config is parsed with the same ruamel loader as the new, so a
        YAML 1.1 value like `on` never reads as a change. A non-string
        tenant (`2025`) is skipped with "quote it".
      - The manifest is deleted before the old tenant folder is removed.
      - A failed tree move stops before Keychain, and says what's at each
        path.
      - The Keychain delete is retried once.
      - Every manual fallback names a raw `keyring.delete_password` command,
        never `dax creds remove`: once the tenant changed, the old name is
        neither registered nor derived, so dax refuses it.
      - Two notes: a registered `credentials:` entry under the old name
        (left holding nothing), and an env that left the registry in the
        same edit (renamed or removed) while its tree is still on disk.

45. **Hiding tenants from `dax env list`.** Decided 2026-10-21 (user: "don't
    show parked or test envs unless the --all flag is present"). Built into
    step 11.
    - **A per-tenant `hidden: true`** in the `tenants:` block, next to the
      browser setting, rather than a separate top-level list naming tenants
      a second time. A missing `hidden`, or `false`, shows the tenant.
    - **Plain `dax env list`** leaves out every row filed under a hidden
      tenant (orphaned trees included) and says so in a footer:
      `N envs hidden in parked, test` (in `tenants:` order). This is
      combined with the archive-only count on one line.
    - **`--all`** shows hidden tenants in their `tenants:` order, and the
      archive-only group. One flag, because both mean "envs I'm not working
      on".
    - **Only the listing is affected.** `show`, `run`, `archive`, `restore`,
      `destroy` and their pickers treat hidden envs like any other.
    - **Checked:** `hidden` must be a boolean. Anything else is warned about
      by `dax run` and `dax config`, and doesn't hide.

## The full env lifecycle, end to end

Every stage, naming the exact command and the decision behind its shape.

1. **Create.** `mkdir <name> && cd <name> && dax run`. The directory doesn't
   exist in the registry yet, so `dax run` calls `_bootstrap_env` before
   doing anything else (decision 4 — no `dax init`, no `dax process new`,
   no `dax env new`):
   - Guardrail check against `cwd` (decision 8) — fatal with a clear message
     if it's nested in/colliding with an existing project or outside
     `$HOME`; nothing to retry, `cd` elsewhere and run `dax run` again.
   - Image, then tenant (always required: an existing one or a new one),
     then a checkbox of existing non-Claude credentials (decision 35).
     Claude is never asked about: it's baseline, and its credential is
     implied.
   - "Is this a coding project or a process that needs the substrate?"
     (decision 7).
     - Coding: nothing further.
     - Process: substrate path, title, type (picked from the substrate's own
       `process-types.tsv`), git-tracking decision. `new-process.sh --dir
       <cwd> ...` runs only **after** the summary is confirmed (as today's
       `_run_process_new` already does), so declining leaves `cwd` untouched.
   - Name is always `cwd.name` (decision 5). No flags anywhere in this flow
     (decision 6) — every field above is a prompt, never a CLI argument.
   - Summary via `_print_table` (decision 11), paced with real spacing
     between each section above (decision 10), confirm (decision 9's
     `auto_enter=False` applies here same as everywhere), then the entry
     is built in memory and saved once (decision 31). Declining or
     cancelling **ends `dax run`**: nothing is registered and no container
     launches.
   - `dax run` continues immediately into launching the container — create
     and launch are one motion, not two commands.

2. **Launch, every time after that.** `dax run` from the registered
   directory. For every env (Claude is baseline, decision 35): the baseline
   `feature_claude` idempotently creates the state tree if needed, then seeds
   `CLAUDE_SHARED_FILES` into it once (decision 13 — real work only the
   first time, a silent no-op every launch after). Proactively reruns the
   Claude login flow if the cooldown says it's due. Then launches.

3. **Inspect.** `dax env show <name>` — registry fields plus everything not
   visible in `~/.dax.yaml` (decision 14): directory existence, feature/
   mount mismatches, the resolved derived credential name, whether the
   container's running right now, state tree size and age.

4. **Edit.** `dax config` (decision 38): `~/.dax.yaml` in your editor,
   the previous version kept as `~/.dax.yaml.prev`, and the result
   checked. No `dax env set` (decision 14).

5. **Back up.** `dax env archive` (decision 3). Pick env(s) → "save to dax's
   managed backup location, or to a specific file?" → managed keeps today's
   batch, all-envs and remove-after-archive behavior, now as prompts rather
   than flags (decision 6), with removal gated on a read-back (decision 34);
   specific-file is one env to a chosen
   path. Credentials are never archived (decision 29). No flags (decision 6's
   reasoning applies here too, carried over from the original `process
   archive` design).

6. **Recover.** `mkdir <dir> && cd <dir> && dax env restore [name|path]`
   (decisions 3, 26, 27). Single target only, always, into an empty,
   unregistered `cwd`, never overwriting anything. "From dax's managed
   backups, or from a specific file?" → env then date, or a path →
   summary → confirm → extract. The env is named `cwd.name`. Rolling back a
   live env is `dax env destroy` first, then this.

7. **Destroy.** `dax env destroy` (decision 2) — unchanged from today's
   `process destroy` except that `--dry-run` is gone (decision 30): picker or explicit names, refuses on a running
   container, typed `DESTROY` to confirm, removes directory/state
   tree/derived credential/registry entry.

Credentials (`dax creds ...`) and image builds (`dax build`) sit outside
this lifecycle entirely — untouched by any of this.

## The resulting `dax --help` surface

Every top-level command, after every decision above — including 18 and 19,
so `envs`/`process`/`tenant`/`tenants` are gone too, not just the things
decided earlier.

```
usage: dax [-t] {build,run,features,backup,creds,env,config} ...

  build                 Build the dax Docker image
  run                   Launch a dax container (bootstraps an unregistered
                         cwd automatically — see "dax env" for everything
                         else in an env's lifecycle)
  features              List available features
  backup                Back up dotfiles and config to backup/
  config                Edit ~/.dax.yaml in $EDITOR, then check it
  creds                 Manage credentials
    add                   Add or update a credential in the global store
    list                  List registered credentials
    remove <name>         Remove a credential value from Keychain
    update <name>         Update credential metadata (chrome profile, etc.)
    login <name>          Authenticate a credential via its auth flow
  env                   Inspect, list, back up, recover, or remove an
                         environment
    list                  List registered environments
    show [name]           Show one env's configuration
    destroy [name ...]    Permanently remove one or more envs
    archive [name ...]    Snapshot one or more envs to a managed backup,
                           or one env to a specific file
    restore [name|path]   Recover one env from a managed backup or a
                           specific file, into an empty cwd
```

Gone entirely: `dax init`, `dax process` (including `new` — folded into
`dax run`'s bootstrap, decision 4), `dax envs` (folded into `dax env list`,
decision 18), `dax env set`, `dax env accept-shared-files`, `dax tenant`,
`dax tenants` (decision 19). No aliases for any of it (decision 16). Seven
top-level commands (six survivors plus `dax config`, decision 38), down
from eleven.

## Implementation plan

**How to build it (from the 2026-10-19 review):**
- **Build directly in the repo's own checkout; no worktree.** Considered and dropped
  2026-10-19: the user tests only on scratch envs, and reverting to
  `master` is a sufficient fallback. The host's `dax` is an editable
  install of the repo (`~/fatsec/dax` on the host), so whatever is in the working tree is what the host
  runs. That's safe at step boundaries (suite green) and risky only
  mid-step. Rules:
  - Claude says explicitly when each step is done. **Don't `dax run` a real
    env mid-step**; between steps it's fine.
  - **Step 7 changes the path every real launch and every Claude login
    takes** (the shared-files seed, the login temp folder). Run its manual
    checks on a scratch env before the next real launch.
  - **The repo is `~/fatsec/dax` on the host** (`/home/dfarrow/dax` is
    only its path inside the container). Every host command uses the host
    path.
  - **Escape hatch:** `cd ~/fatsec/dax && git stash -u && git checkout master`
    gets master back immediately (`-u` because the design doc and new
    files are untracked). `git checkout feature/env-lifecycle-consolidation
    && git stash pop` resumes. `~/.dax.yaml.bak` covers config damage.
- **Update `CLAUDE.md`'s progress note after every step**, commit or not.
  It's eight steps of uncommitted work, and a context compaction or outage
  mid-build shouldn't lose where things stand.
- **Execution order is 1, 2, 3, 4, 5, 7, 8, 8b, 8c, 8d, 6.** Step 7 needs steps 3
  and 4 in place, and step 6's doc sweep and grep gate can only pass once
  steps 7 and 8 have removed the old names. The step numbers are kept as
  labels.
- **Manual-test gate before each step** (user's standing instruction,
  2026-10-19): before implementing a step, Claude names the runbook checks
  that must precede it and waits for the user to confirm they're done and
  passed. Before step 1: the runbook's Setup and Fixtures. Before each
  later step, in execution order: the previous step's checks. This is
  needed because the host runs the working tree, so a step's checks can't
  be run once the next step starts, and some checks produce state later
  ones use (step 2's archives feed step 3, step 3's `dxt-b2` feeds step 7).
  A failed check gets fixed before moving on.
- One commit per step, so a regression bisects to a single step. Commits
  still wait for explicit approval, per the standing git-workflow rule.
- Record the test count before step 1. About 76 tests are deleted on
  purpose, so a falling count proves nothing; each step's commit message
  states how many tests it deleted and added.
- `tests/test_cmd_run_project_resolution.py` guards Gate 0 (refusing
  `dax run` from a registered project's subdirectory). Its rewrite in step
  4 must assert that refusal before the old file is removed.
- Finish with `dax build` (the image's `dax_creds` is a frozen install),
  then a live pass: bootstrap a new env, archive it, restore it into an
  empty folder under a different name, destroy both.

**Step 1 — move `destroy` to `dax env`, unchanged; drop `set`.**
- Move `process_destroy_p`'s parser definition from `process_sub` to
  `env_sub` in `main()`.
- Move its dispatch branch from `cmd_process` to `cmd_env`. **Restructure
  `cmd_env` first** (found by the second independent review): today it
  calls `_resolve_env_name` before dispatching (`dax.py:1589`), and that
  exits when cwd isn't a registered env. Moved under it as-is, `destroy`
  and `archive` with no arguments would exit instead of showing the picker
  (decision 21), `restore` would always exit (its cwd is unregistered by
  definition, decision 26), and `list` would exit outside an env. Resolve
  the name only inside `show`, and route `--explain` before the config load
  (as `cmd_process` does today). Tests go through `cmd_env`, not just the
  `_run_env_*` functions, for each subcommand run from an unregistered cwd.
- `tests/test_dax_process_archive_explain.py:98` dispatches
  `cmd_process(process_command='destroy')`; update it here, not in step 4.
- Rename `_run_process_destroy` → `_run_env_destroy` (decision 15).
- Drop destroy's `--dry-run` (decision 30), the one behavior change to an
  otherwise unchanged command.
- `tests/test_dax_process_destroy_cmds.py` calls `_run_process_destroy`
  directly — name update only if renaming, no structural change.
- Per decision 14: remove `env_set_p`'s argparse entry and `cmd_env`'s
  `set` branch. `run_env_set` stays as an internal helper **only until
  step 4**: once the merged restore (step 3) and the wizard (step 4) build
  entries in memory and save once (decision 31), it has no callers left, so
  step 4 deletes it and its tests. Delete its CLI-level tests here. User-facing messages that tell the
  user to run `dax env set` (`dax.py:270`, `dax.py:552`,
  `dax_creds/config.py:334`, `dax_creds/init.py:955`, `dax_creds/init.py:1059`)
  are rewritten to say which `~/.dax.yaml` field to edit.
- Per decision 20: fix `ENV_FIELDS`'s stale comment; generalize
  `run_env_show`'s hardcoded `multi_tenant`/`tenant_subdir` legacy check to
  `unknown = [k for k in proj if k not in ENV_FIELDS]`; add the same check,
  quietly, to `cmd_run`'s existing backup-check pass, scanning every
  registered project. New tests for the generalized check (replacing the
  old legacy-field-specific ones) and for the `cmd_run`-time warning.

**Step 2 — build the merged `dax env archive` (absorbs `export`).**
- New function `_run_env_archive` (decision 15) combining today's
  `_run_process_archive` and `_run_process_export`: env selection → managed-
  vs-specific-file prompt → the managed branch's existing batch/`--all`/
  picker/`--remove` logic, or the specific-file branch's existing single-env/
  chosen-path/no-remove logic → shared `_write_process_archive` write.
- Replaces `process_archive_p`/`process_export_p` with one `env_sub` parser
  entry, no flags (decision 6): `--all`/`--remove`/`--include-credential`/
  `--dry-run` all become wizard prompts instead, except
  `--include-credential`, deleted outright (decision 29), and `--dry-run`,
  also deleted (decision 30; declining the confirm is the preview).
  `--explain` is kept (decision 30).
- `tests/test_dax_process_archive_restore.py` (archive) and the archive
  side of `tests/test_dax_process_export_import.py` get reshaped around the
  merged function and the new mode-prompt; scenarios themselves (missing
  dir skip-not-fatal, remove-after-batch, confirmation) carry over. The
  `--include-credential` scenarios are replaced by one test asserting that
  `.credentials.json` is never archived (decision 29).
- Per decision 34: when removal was chosen, read every tarball in the
  batch back (manifest via the shared reader, plus a `project/` member)
  before removing anything; any failure cancels all removals.
- Per decision 23: `_write_process_archive` writes the manifest as
  `dax-env-manifest.json` only, no longer `dax-export-manifest.json`. New
  test confirming the written tarball uses the new name.
- **Also per decision 23, moved here from step 3 (2026-10-19 review):** the
  manifest readers (`_read_archive_manifest` and `_run_process_import`'s
  own lookup) try `dax-env-manifest.json` first and fall back to
  `dax-export-manifest.json`. The writer and the reader must land together.
  Otherwise, between steps 2 and 3, an archive made with branch code can't
  be restored by branch code. Tests: both an old-named and a new-named
  archive restore successfully through today's (not yet merged) restore.

**Step 3 — build the merged `dax env restore` (absorbs `import`;
single-target, cwd-based per decision 3).**
- New function `_run_env_restore` (decision 15) combining today's
  `_run_process_restore` and `_run_process_import`, simplified to one
  target and `cwd` as the destination: managed-vs-specific-file prompt →
  (managed) pick which env's backup by name/date → (specific file) prompt
  for the path, read its manifest → extract into `cwd` via the shared
  `_extract_process_archive` core (import's duplicate goes away) → a plain
  confirm noting the original manifest `dir` when it differs from `cwd`.
- No `--all`, no in-place/move distinction, no `--dir` flag — `cwd` always.
- Per decision 22: rename `_check_process_dir`/`_process_dir_conflict` to
  `_check_bootstrap_dir`/`_bootstrap_dir_conflict` here, since restore's
  guardrails are the first new caller (step 4 then uses the new names).
- Per decision 26: refuse unless `cwd` is empty and unregistered; apply
  decision 25's name/state-tree collision checks against `cwd.name`;
  replace `_extract_process_archive`'s `rmtree`s with refusals in the core;
  delete `_restore_in_place`'s restore-over-registered branch. New tests:
  non-empty `cwd`, registered `cwd`, and existing dest state tree each
  refuse with nothing touched on disk or in `~/.dax.yaml`.
- Per decision 28: extend `_relocate_claude_project_key` to also rewrite
  `.claude.json`'s `projects` key and `history.jsonl`'s `project` fields.
- Per decision 31: validate manifest features before extraction, write
  unknown ones as commented-out `#- <name>` entries, and collapse the
  post-extraction registry writes into one `save_config`.
- Per decision 33: on a renamed restore, replace explicitly named Claude
  credentials in `creds:` with bare `claude`, and show the swap in the
  summary. (Step 7 then simplifies this to "drop Claude entries from
  `creds:`" per decisions 35–36; until then the bare-`claude` swap is the
  correct interim behavior.)
- Per decision 29: strip `.credentials.json` from any extracted state tree
  and warn when an archive contained one; test with an old-style archive
  that carries a credential.
- Per decision 27: `_available_archives` returns every archive per name;
  two-step env→date picker with `cwd.name` and newest-date pre-highlight;
  `questionary.path` for the specific-file branch; optional positional
  `name|path` shortcut. Tests for picking a non-newest archive, and for both
  shortcut forms.
- Replaces `process_restore_p`/`process_import_p` with one `env_sub` parser
  entry: an optional `name|path` positional plus `--explain` (decision 30),
  nothing else.
- **Extraction into an existing `cwd`:** `_extract_process_archive` does
  `shutil.move(project_src, process_dir)`, which only worked because
  `process_dir` was `rmtree`d first. With `cwd` now always an existing
  (empty, maybe `.DS_Store`-only) directory, that would nest the project at
  `cwd/project/`. Move the *contents* of `project_src` into `cwd` instead.
  Test: files land directly in `cwd`, and a pre-existing `.DS_Store` is
  left alone.
- Archive's own `--explain` text and `dax backup --explain`'s redirect (and
  `tests/test_dax_process_archive_explain.py`'s assertions on it) is updated
  in **step 2**, when the flags it describes change, not deferred to step 6.
  Likewise restore's `--explain` here.
- `tests/test_dax_process_restore.py` and the import side of
  `tests/test_dax_process_export_import.py` get reshaped around
  single-target/cwd-based restore; drop the batch/`--all`/multi-pick test
  scenarios entirely (confirmed with the user: never used), keep
  `_relocate_claude_project_key`/collision/manifest-reading scenarios, which
  still apply regardless of which branch triggers them.
- Per decision 23: the manifest reader's fallback already landed in step 2.
  Here, just make sure the merged function uses that one reader and keep
  the old-name/new-name tests passing through it.
- `cmd_process` disappears entirely once step 4 (bootstrap) also removes
  `new` from under it.

**Step 4 — build `_bootstrap_env` and wire it into `dax run`.**
- New function in `dax.py`: `cwd` guardrails (fatal, per decision 8) →
  registered-name check (fatal, decision 25; the leftover-state-tree check
  runs later, right after the tenant prompt) → image → tenant (always,
  required; pick an existing tenant or type a new one, per decision 35) →
  leftover-state-tree check → checkbox of **existing non-Claude**
  credentials (no define-new loop, no Claude question, per decision 35) →
  substrate-or-not branch → substrate sub-flow (type/git; `new-process.sh`
  runs only after confirm) → summary via `_print_table` → confirm →
  one in-memory entry and a single `save_config` (decision 31).
  **Declining or cancelling must end `dax run`.** Today `cmd_run` re-looks
  up the project after the wizard; when nothing was registered, that
  `KeyError` is swallowed by the outer `except (FileNotFoundError,
  KeyError)` and a container launches anyway. `_bootstrap_env` exits (or
  returns a sentinel `cmd_run` checks) on decline or cancel. Test at the
  `cmd_run` level: decline → no `docker run`, nothing registered.
  Delete `run_env_set` and its tests here (its last callers are gone).
  Per decision 37: replace every "run `dax init` first" exit with the
  shared missing-config message showing the minimal `~/.dax.yaml`.
  Until step 7 lands, the wizard still
  writes bare `claude` into `creds:` and `claude_tenant_state` into
  `features:`, so the env works on pre-step-7 code. Step 7 stops writing
  both.
- `cmd_run`'s existing fallback (`dax_print("[+] project not registered —
  starting dax init")` → `run_init(...)`) calls `_bootstrap_env` instead.
- Delete `dax init` entirely: `cmd_init`, its argparse entry, `run_init`/
  `_run_init` in `dax_creds/init.py` (including the now-fully-dead
  "already registered, update it?" branch — already unreachable from
  `dax run`, now unreachable from anywhere).
- Delete `_setup_claude_credential` (`dax_creds/init.py:250`): its only
  caller is `_run_init`, deleted above (decision 32). Update its tests in
  `test_dax_claude_grant_collision.py` and `test_dax_creds_init.py`.
- `test_dax_process_archive_explain.py` calls `cmd_process`, so it moves
  to the `dax env` entry points as `cmd_process` disappears.
- Delete `dax process new`: its argparse entry, `_run_process_new` (folded
  into `_bootstrap_env`), `cmd_process` (now empty — remove the `process`
  top-level command entirely, per decision 4).
- Rewrite `tests/test_dax_run_init.py` (122 lines),
  `tests/test_cmd_run_project_resolution.py` (101 lines),
  `tests/test_dax_process_cmds.py` (403 lines, currently pure
  `_run_process_new` coverage) against `_bootstrap_env` — same scenarios
  (missing tenant/substrate/title, unknown type, git flag behavior,
  declining the confirm), reshaped around one function instead of two.
  Per decision 8: a `cwd` guardrail violation (nested/colliding project,
  outside `$HOME`) should assert a clean fatal exit with a useful message,
  not a retry; per decision 25, so should a registered-name collision and
  a leftover `tenants/<tenant>/<name>/` state tree (new tests for both,
  including that `~/.dax.yaml` is left untouched); declining the substrate
  branch's confirm never runs `new-process.sh` (cwd stays empty); a bad *prompted* field (substrate path, title, type, tenant)
  should assert a retry/re-prompt instead.

**Step 5 — wizard polish.**
- Reapply `_q_confirm`'s `auto_enter=False` per decision 9 — this time as
  part of this branch, merged with PR #10's `_flush_stdin()` addition to
  the same function rather than conflicting with it.
- Blank-line section spacing per decision 10.
- `_print_table`-based summary per decision 11.
- Verify end to end that every *prompted* field (substrate path, title,
  type, tenant) re-prompts on a bad answer, and that every fixed-input
  check (the `cwd` guardrails per decision 8, name and state-tree collisions
  per decision 25, restore's empty-`cwd` check per decision 26) exits
  cleanly instead.

**Step 8b — `dax config` (decision 38).**
- New top-level `config` command in `dax.py`: copy to `~/.dax.yaml.prev`,
  run the editor (`$VISUAL`/`$EDITOR`/`vi`, split with `shlex` so
  `EDITOR="code -w"` works), parse via `load_dax_config`, offer to reopen on
  a parse error, then print the unknown-key and old-Claude-entry warnings.
  A missing file is decision 37's message. Warn once when run inside a dax
  container (single-file bind mount).
- Tests: the editor command is built from `$VISUAL`, then `$EDITOR`, then
  `vi`; `.prev` matches the pre-edit file; a parse error re-offers the
  editor and a decline exits non-zero; unknown keys are reported; a missing
  config is never created. The editor is replaced by a script that edits
  the file.

**Step 8c — remove auggie (decision 39).**
- Delete the auggie code listed in decision 39, the image's auggie install and
  wrapper, and `test_dax_creds_auggie.py`. Drop auggie cases from the other
  test files.
- Generalize decision 36's filter and warning so `auggie` in `features:`
  and auggie-provider credentials in `creds:` are retired entries too,
  reported with a message that fits both (not "Claude is automatic now").
  Restore drops them.
- Tests: an env listing `auggie` launches with no auggie mount and a warning;
  `dax creds add` no longer offers auggie; an auggie-provider credential is
  not resolved for an env; restore drops `auggie` from an archive's features.

**Step 8d — `mounts:` implies its feature; one global substrate
(decisions 40, 41).**
- `cmd_run` adds `mounts` when the entry's `mounts:` is non-empty. `mounts`
  in `features:` joins the retired-features filter and warning for every
  source. Delete `run_env_show`'s "mounts feature not active" note, and
  restore's carrying of `mounts` in features.
- `feature_substrate` reads the top-level `substrate:` and refuses if it's
  unset. Drop `substrate` from `ENV_FIELDS` and stop promoting
  `project['substrate']`. The wizard's substrate branch uses the global
  path, or asks once and saves it as the top-level `substrate:`. Archive
  stops writing the path; restore ignores it. `env show` displays the
  global path.
- Tests: an env with `mounts:` and no `features:` gets its mounts; `mounts`
  in `features:` is warned about and mounts nothing extra; an env with
  `features: [substrate]` mounts the global path and sets `SUBSTRATE_ROOT`;
  no global path refuses at launch; a per-env `substrate:` is reported as
  retired; the wizard asks for and saves the global path only when it's
  missing.

**Step 6 — doc/example sweep (covers both halves of this doc).** **Run it
last, after steps 7 and 8**, even though it keeps its number: until then
`accept-shared-files`, `dax envs` and `dax tenant` still exist, step 7
changes the most user-facing behavior of all (Claude baseline), and the
final grep gate can't pass. Execution order: 1, 2, 3, 4, 5, 7, 8, 6.
- CLAUDE.md: retire old `dax init`/`dax process new`/`dax process
  archive`/`export`/`restore`/`import` write-ups, add this consolidation's
  own entry once built.
- `.dax.yaml.example`'s comments currently point at `dax process new` by
  name; drop the `claudedir:` block per decision 12.
- **`README.md`**, which is user-facing and was missing from this list
  until the 2026-10-19 review.
- User-facing strings in code, not just docs: `dax backup --explain`'s
  redirect to `dax process archive` (asserted by
  `tests/test_dax_backup.py:172-186`, update together), archive/restore
  `--explain` text, and any message in `dax.py`/`dax_creds/config.py`/
  `dax_creds/init.py` naming a removed command. Decision 16 adds no
  aliases, so each of these becomes a dead pointer otherwise.
- `docs/testing/2026-07-30-cred-management-test-sequence.md` (update
  command names) and `docs/testing/2026-07-31-shared-files-sync-test.md`
  (mark superseded by decision 13). Leave
  `docs/design/2026-08-04-codebase-review.md` alone; it's a dated record.
- **Final gate:** `grep -rn "dax process\|dax init\|dax envs\|dax tenant\|dax env set\|accept-shared-files\|include-credential\|--dry-run\|claude_tenant_state"`
  across the repo comes back clean, excluding dated historical docs, the
  superseded-history sections of `CLAUDE.md` that step 7 deliberately keeps,
  and files that name these terms on purpose: decision 36's legacy-name
  filter and its tests, tests asserting a removed command is gone, this
  design doc, and the runbook. Exclude those **by path in the command
  itself**, so the gate stays mechanical.
- `docs/design/2026-08-22-process-archive-restore.md` (already on `master`
  since PR #10 merged — needs its own pass describing the merged commands)
  and anything in `docs/design/VALIDATION.md` describing the old entry
  points.

**Step 7 — Claude becomes baseline; decommission the old `claude`; simplify
the shared-files sync** (must come **after steps 3 and 4**: decisions 35–36
change restore's credential handling and the wizard's writes, not just
launch).
- Per decision 35: delete the old shared-mount `feature_claude`. Rename
  `feature_claude_tenant_state` to `feature_claude` and add `'claude'` to
  `_ALWAYS_ON_FEATURES`. Make `cmd_run`'s relogin and teardown-sync checks
  unconditional. `cmd_run` adds the implied `claude-<tenant>-<env>` to the
  env's resolved credentials, and refuses an env with no `tenant` (clear
  message). The wizard stops writing `claude`/`claude_tenant_state`.
- Per decision 36: filter `claude_tenant_state`, `claude` (in `features:`)
  and Claude-provider entries (in `creds:`) wherever an env is read, warn at
  launch naming envs that still carry them, and have restore drop them from
  manifests. This supersedes step 3's interim decision-33 swap.
- **The implied credential goes in `resolve_credential_names`
  (`dax_creds/config.py:314`), not in `cmd_run`.** Found by the independent
  2026-10-19 review: every credential lookup derives from an env's `creds:`
  list. That includes `derived_credentials`, `credential_users`,
  `credential_names_for_provider`, `_login_credential_def`,
  `_derived_creds_for` (destroy and `archive --remove` Keychain cleanup)
  and `run_creds_list`. If only `cmd_run` knew about the implied
  credential, then for an env with no Claude entry in `creds:`:
  `dax creds login claude-<t>-<env>` would fail with "Unknown credential",
  destroy would leave its Keychain entry behind, `creds list` would drop
  its row, and the grant-collision check (C6) would silently stop seeing
  it. So `resolve_credential_names` always yields
  `(claude-<tenant>-<env>, 'claude')` for every env and ignores Claude
  entries in `creds:` (decision 36); everything downstream then works
  unchanged. Tests through each of those callers for an env whose `creds:`
  has no Claude entry.
- Also make unconditional the `'claude_tenant_state' in features` gates in
  `run_creds_list` (`dax_creds/init.py:630`, `:713`, the "key on disk" and
  relogin columns), not just the ones in `dax.py`.
- **De-duplicate features across all sources** (baseline, the env's own
  `features:`, a cwd-local `.dax.yaml`, `-f`) before applying them, so
  `-f claude` or a stray `claude` anywhere can't apply the baseline twice.
  Decision 36's filter alone covers only the env's own list.
- **Error ordering:** an undefined explicitly named credential currently
  raises `KeyError` inside `cmd_run`'s `try`, which swallows it before
  `config['tenant']` is set. With Claude always on, the user would then see
  a misleading "needs a tenant". Report the undefined credential by name,
  and refuse on a missing tenant only when the tenant really is missing.
- `_print_features` marks always-on features (`workdir`, `dotfiles`,
  `webpreview`, `claude`) as such, so `dax features` reflects decision 35.
- **`resolve_credential_names` needs `config`.** Its signature is
  `(project, project_name)`, so it can't tell that an explicitly named
  `creds:` entry (e.g. `claude-ysecurity-ot`) is a Claude-provider
  credential, which decision 36's filter needs. Pass `config` and update
  every caller.
- **`run_env_show`** prints a derived name only when a bare `claude` is in
  `creds:` (`dax_creds/init.py:858`). Make it show the implied
  `claude-<tenant>-<env>` for every env (decision 14's note).
- **Destroy of an env whose implied name is also explicitly registered**
  (`ot`, with `claude-ysecurity-ot` both registered and listed in its
  `creds:`): `_derived_creds_for` now returns it, but `run_creds_remove`
  refuses while any project references it (`dax_creds/init.py:1054`), and
  the env being destroyed still does. Teardown must not count decision-36
  filtered references, or the env being destroyed, as users. It removes the
  `credentials:` entry along with the Keychain secret. Test with an
  `ot`-shaped fixture.
- **Envs that had `claude_tenant_state` but no Claude credential** (live:
  `a-priest-and-an-imam`) have state trees whose `.credentials.json` was
  never synced or tracked. Once teardown sync is unconditional, that
  untracked grant would be stored into Keychain on the next stop with no
  grant-collision check (`_sync_and_clear_claude_credential` calls `store`
  directly). Apply the same `grant_collision` check before `store` there,
  refusing with the existing collision message. Test: a tree credential
  whose grant is already under another name is not stored.
- Update the remaining stale pointers this step creates:
  `tools/migrate_env_state.py:239,414` (prints `dax env set ... features
  claude_tenant_state`) and the comment at `dax_creds/wrappers/claude:18`.
- **Test files this step touches** (from the review's caller sweep):
  `test_dax_claude_mount_exclusivity.py` (delete; its check is gone),
  `test_dax_claude_tenant_state_mount.py`, `test_features.py`,
  `test_dax_cmd_run_relogin.py`
  (`test_relogin_not_triggered_without_claude_tenant_state_feature` is now
  wrong), `test_dax_cmd_run_claude_credential_sync.py`,
  `test_dax_creds_list.py`, `test_dax_process_destroy_cmds.py` (imports
  `_shared_files_manifest_path`).
- (Order within this step: delete the **old** shared-mount `feature_claude`
  and the `cmd_run` mutual-exclusion check *first*, then do the rename
  above. Read in the other order, "delete `feature_claude`" would delete the
  feature just renamed.)
- **Do not touch `_LOGIN_PROVIDERS['claude']`** (`dax.py:1209`). It also
  mounts the host's `~/.claude`, but it's a separate mechanism from
  `feature_claude`: `dax creds login` starts a throwaway container with
  the host `~/.claude` mounted rw, runs `claude auth login` in it (the
  token lands in the host's `~/.claude/.credentials.json`), then reads
  that file host-side and stores it in Keychain. Deleting it along with
  the feature would break every Claude login. Instead, change its host
  source per decision 32: a fresh `0700` temp folder under
  `~/.local/state/dax/login/` replaces the host's `~/.claude`, read via
  `import_from_disk(..., credentials_file=...)`, deleted in a `finally:`,
  with stale folders swept at the start. Tests: the docker command mounts
  the temp folder at the container's `~/.claude` and never the host's;
  the folder is gone after success, cancel, and an exception; a leftover
  folder is swept; an empty folder after the flow imports nothing.
- **`claudedir:` stays in the live `~/.dax.yaml`**, harmless once
  `feature_claude` is gone. The user removes it by hand if wanted; only
  `.dax.yaml.example` changes here.
- Rewrite `sync_claude_shared_files` (`dax_creds/config.py`) to the one-time
  seed described in decision 13: copy a `CLAUDE_SHARED_FILES` entry into the
  tree only if absent, nothing else, returning nothing (no more "drifted"
  list — there's nothing left to report). Delete
  `_shared_files_manifest_path`/`_load_shared_files_manifest`/
  `_save_shared_files_manifest` and whatever hashing helper only existed for
  them. **But first, check the caller found in the 2026-10-19 review:**
  `_teardown_process` (shared by `destroy` and `archive --remove`) imports
  `_shared_files_manifest_path` to delete each env's sibling
  `<tenant>/<project>.shared-files.json`. Deleting the helper outright
  would make every destroy fail with an `ImportError`. Real envs already
  have those sibling files on disk, so teardown keeps a one-line inline
  cleanup (delete `state_root()/<tenant>/<project>.shared-files.json` if
  present) as a cleanup step for old files. Test: destroying an env with a
  leftover sidecar removes it, and one without succeeds. No change needed to when it's called — `feature_claude_tenant_state`
  already calls it unconditionally on every `dax run`, right after its own
  idempotent `Path(host_tree).mkdir(parents=True, exist_ok=True)`; both are
  no-ops after the first real launch, which is what makes this a one-time
  seed in practice without needing a separate "first launch" check.
- In `feature_claude_tenant_state` itself: delete the loop that turns
  `sync_claude_shared_files`'s return value into the drift warning and the
  `dax env accept-shared-files` suggestion (currently right after the
  `sync_claude_shared_files` call) — not just the command and the sync
  internals, this caller-side code goes too.
- Delete `dax env accept-shared-files`: its argparse entry, `cmd_env`'s
  dispatch branch, `run_env_accept_shared_files` in `dax_creds/init.py`.
- Rewrite the shared-files-sync test file around the trivial seed-once
  behavior (drift/manifest scenarios go away entirely); delete the
  `accept-shared-files` cases from the env-commands test file.
- CLAUDE.md's decision-C narrative (the shared-files sync's extensive
  "found live" history) stays as a record of how this got here, but gets a
  superseded note pointing at decisions 12/13 and this simplification,
  matching how other superseded sections in that doc are already handled.

**Step 8 — fold `envs` into `env list`; delete `tenant`/`tenants` and the
dead multi-tenant model entirely** (independent of steps 1–7).
- Move `envs_sub.add_parser('list', ...)`'s logic into `env_sub` as
  `env_sub.add_parser('list', ...)`; drop the `envs` top-level parser
  (decision 18). `run_envs_list` can likely keep its name and move, or
  become `run_env_list` to match — minor, not worth a separate decision.
- Delete `cmd_tenant`, `cmd_tenants`, their argparse entries (`tenant_p`/
  `tenant_sub`, the `tenants` parser), and `_ensure_tenants_classified`
  (decision 19).
- Delete `dax_creds/tenant.py` entirely: `resolve_tenant`,
  `all_tenant_projects`, `resolve_for_cwd`, `TENANT_FILE`,
  `_read_tenant_label`, `_NOT_A_PROJECT_SUBDIR`.
- Delete `dax_creds/cli.py`'s `resolve-tenant` subcommand and its
  `resolve_for_cwd` import — Gate B's last remnant now that the wrapper
  itself no longer calls it (confirmed already gone from
  `dax_creds/wrappers/claude` per CLAUDE.md's decision B notes).
- Remove `cmd_run`'s `config['multi_tenant']`/`config['tenant_subdir']`
  promotion lines. They're already dead (nothing downstream reads them),
  and step 8 is just where they get cleaned up with the rest of the
  multi-tenant model.
- **Keep test coverage for `_prompt_tenant` and `_known_tenant_names`.**
  Their only unit tests live in `tests/test_dax_tenant_cmds.py`, which this
  step deletes, yet `_prompt_tenant` survives (it's what makes tenant
  required) and `_known_tenant_names` is rewritten. Move those tests into a
  surviving file and adapt them before deleting the rest.
- Simplify `_known_tenant_names()` (whose registry half already landed in
  step 4, a `/code-review` fix) to a one-line set comprehension over
  `projects[*].tenant`, dropping its `all_tenant_projects` call (the real
  wrinkle noted under decision 19) — `_prompt_tenant` itself is unaffected.
  (`run_env_show`'s hardcoded `multi_tenant`/`tenant_subdir` check is
  already handled — step 1/decision 20 generalizes it, nothing further
  needed here.)
- Per decision 24: add a historical-only marker to
  `docs/design/2026-07-27-tenant-isolation.md` right before its "Redesign
  2026-07-29" heading, covering only the Problem/Verified findings/
  decisions 1–9/12 sections above it. Nothing below that heading changes.
- Delete `tests/test_dax_creds_tenant.py` (39 tests), `tests/test_dax_tenant_cmds.py`
  (29 tests), `tests/test_dax_creds_cli.py` (8 tests, entirely
  `resolve-tenant` scenarios) — 76 tests total, counted directly rather
  than assumed from CLAUDE.md's older "~68" estimate.

## Risks going in (2026-10-19 review)

Checked against the code and the live `~/.dax.yaml` before building, not
assumed:

- **Baseline: 671 tests passing.** Roughly 100 get deleted or rewritten
  on purpose, so a green suite can't by itself show a weakened assertion.
  Gate 0 (refusing `dax run` from a registered project's subdirectory) is
  the highest-risk one to lose (see "How to build it").
- **The live config is clean against decision 20.** No project in the real
  `~/.dax.yaml` has a key outside `ENV_FIELDS`, so the new check won't
  fire on day one.
- **`auto_claude` doesn't depend on `feature_claude`.** It only sets
  `_final_exec`. The three envs using it (`ys-augmentcode`,
  `self-employment-setup`, `ysecurity-onboard`) are unaffected by step 7.
  `discernment` is the only env still listing `claude`.
- **`run_env_set` stays as an internal helper.** Only its CLI entry goes.
  But it saves on every call, so the merged restore and the bootstrap
  wizard build the entry in memory and save once (decision 31), rather than
  chaining it.
- **Unreachable from the suite, so they need the manual pass:** real-TTY
  prompt behavior (`auto_enter`, `_flush_stdin`; the `auto_enter` fix was
  reverted once already), Keychain removal on destroy, Docker, the OneDrive
  `backup_dir`, macOS `.DS_Store`. A dax container can reach none of these.
- **ruamel comment API (decision 31) is fiddly.** Writing `#- name` into a
  `CommentedSeq` can attach the comment to the wrong item. Test against a
  copy of the real `~/.dax.yaml`, never the file itself.

## Test plan, per step

**The manual half is a full runbook:**
`docs/testing/2026-10-19-env-lifecycle-test-sequence.md`. It covers setup
(a config backup, a scratch `backup_dir` so nothing reaches OneDrive), `dxt-*` fixtures, per-step commands with pass criteria,
a failure protocol, and cleanup. The table below is the summary; the
runbook is what to actually run. Scratch envs stay credential-free except
where a step needs a real login (about four grants across the whole
sequence).

**Every step ends with the same automated gate:** `python3 -m pytest -q`
all green, with the change in test count accounted for in the step's
commit message (tests deleted and added).

**Before any host test that writes config:** `cp ~/.dax.yaml
~/.dax.yaml.bak`. Use the runbook's throwaway `dxt-*` envs only, never a
real one, until the end-of-branch pass. The table below abbreviates; the
runbook's fixture names and commands are authoritative.

| Step | Automated (in the container) | Manual (on the host) |
|---|---|---|
| **1** destroy moves, `set` dropped, `ENV_FIELDS` check | destroy tests renamed; new unknown-key tests; `run_env_set` callers still pass | `dax env --help` shows `destroy` and no `set`. Add a bogus key to a scratch env, `dax run` anywhere, see the warning, remove the key. `dax env destroy` a scratch env (typed `DESTROY`; dir, state tree and registry entry gone; Keychain removal is checked in step 7) |
| **2** merged archive, plus the manifest reader | archive tests reshaped; `.credentials.json` never in the tarball; new manifest name written; old- and new-named archives both readable | `dax env archive` → managed → scratch env; `tar -tzf` shows `dax-env-manifest.json` and no credential. Archive again to a specific file. `dax env archive --explain` |
| **3** merged restore | non-empty `cwd`, registered `cwd`, and existing state tree each refuse with nothing touched; non-newest date selectable; both shortcut forms; rename rewrites `projects/`, `.claude.json`, and `history.jsonl`; credential stripped plus warning; an unknown feature written as `#- <name>`; one `save_config` | Into an empty `~/dxt-b2`: restore `dxt-b` (the fixture with a real session), check that `.claude.json`'s project key and `/resume` history follow the rename. Restore into a non-empty folder → refused. Restore one **old real archive** from OneDrive into an empty folder (the backward-compatibility proof), then destroy it |
| **4** `_bootstrap_env`; `init` and `process new` deleted | rewritten wizard tests; Gate 0 asserted; name clash and leftover tree are fatal; bad prompted answers re-prompt; tenant always required; credential checkbox lists only existing non-Claude credentials, with no define-new path (decision 35) | `mkdir ~/scratch-c && cd ~/scratch-c && dax run`, through both the coding and substrate branches. `cd` into a subfolder of a registered env and `dax run` → refused. Reuse a registered name → refused. `dax init` and `dax process` are gone |
| **5** wizard polish, `auto_enter` | `_print_table` summary test | Answer a confirm with `y` and no Enter → it must wait. Press Enter twice quickly → no prompt is skipped |
| **6** docs sweep | the final grep gate comes back clean | Skim `dax --help` and `README.md` |
| **7** Claude baseline, old `claude` retired, copy-once seed, login temp folder | an env with empty `features:`/`creds:` gets the state tree and implied credential; no tenant → refused; legacy entries filtered, applied once, and warned (decisions 35–36); seed-once tests; `accept-shared-files` gone; login mounts the temp folder, never the host `~/.claude`, and removes it on success, cancel, and error; stale folders swept | A new scratch env gets `CLAUDE.md` in its tree; existing env trees unchanged. `dax creds login` for a scratch env's Claude credential succeeds against an empty config folder; afterwards the host `~/.claude/.credentials.json` mtime is unchanged and `~/.local/state/dax/login/` is empty. Cancel a second login partway → nothing imported, folder gone. Launch the scratch env → wrapper injects the new grant from Keychain |
| **8** `envs`/`tenant`/`tenants` deleted | 76 tenant tests gone, rest green; `import dax_creds.tenant` fails | `dax env list` matches the old `dax envs list` output; `dax tenant` is gone |
| **End of branch** | full suite | `dax build`, restart one real env (`fabric`), then one full pass on scratch envs: bootstrap, archive, restore under a new name, destroy both |
