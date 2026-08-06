# Codebase review — 2026-08-04

A skeptical read of the current state of `dax`, after the credential-externalization,
per-session Claude state, and discernment/virgil sidecar work. Scope: complexity of
the design and concrete cruft/tech debt worth cleaning up. Verified against the code
directly (grep + read), not against CLAUDE.md's narrative alone — a few things the
narrative describes as "still live" turned out to be dead in practice.

## Verdict

The architecture is sound. The `feature_*` convention (a function named `feature_x`,
looked up by string, returns a list of docker flags) is the right amount of structure
for "extensible list of docker run options" — no plugin system, no unnecessary
indirection. The complexity that exists is not structural, it's residue: **live code
sitting directly next to the ghost of the design it replaced**, from three redesigns
landing in the same files in five weeks. That's tech debt in its most treatable form —
code whose only problem is that it's still there. This is an afternoon of deletion away
from clean, not a rewrite.

## What's genuinely good

- **Refuse-over-fallback as a house style.** No tenant → refuse. Grant collision →
  refuse. Ambiguous shared-file drift → warn and skip. Applied consistently, not just
  where convenient.
- **`grant_id()` / `grant_collision()`** (`dax_creds/providers/claude.py:53-113`) —
  hashing the refresh token to detect two names sharing one OAuth grant is real
  defensive engineering against a failure mode that actually happened in production.
- **grpcfuse/atomic-rename discipline** — `save_config`/`sync_claude_shared_files`
  are written around the known constraint that `~/.dax.yaml` is a single-file bind
  mount where write-temp-plus-rename breaks, instead of rediscovering that the hard
  way twice.
- **The three-way shared-files manifest** (`dax_creds/config.py:92-145`) — a plain
  host-vs-tree diff can't distinguish "host moved on" from "container edited its own
  copy," and getting that wrong means silently clobbering an edited `CLAUDE.md`. The
  one place in the codebase where three-way comparison is load-bearing, not decorative.

## Findings

### 1. A whole dead subsystem is still standing — the biggest item

`dax_creds/tenant.py` (304 lines) implements a full two-gate tenant resolution table
(`resolve_tenant`, `resolve_for_cwd`, `all_tenant_projects`), a `.dax-tenant` file
format, and documentation of how the two gates must never disagree. Verified live:

- `dax_creds/wrappers/claude` no longer calls `dax-creds resolve-tenant` (Gate B) —
  confirmed by reading the wrapper; it now just honors `CLAUDE_CONFIG_DIR` directly.
- `cmd_run` never calls `_ensure_tenants_classified` (Gate A) — its only caller is
  `cmd_tenant classify`, which CLAUDE.md's own backlog already flags for retirement
  ("superseded by `dax envs list`").
- The only live caller into `tenant.py` is `all_tenant_projects`, reached via
  `_known_tenant_names()`, which feeds a pick-list for `dax process new`'s tenant
  prompt. One function, kept alive to feed a dropdown.

The tests followed the code into the grave: `test_dax_tenant_cmds.py` (492 lines) and
`test_dax_creds_tenant.py` (510 lines) — over 1000 lines — exercise `cmd_tenant`,
`cmd_tenants`, and `resolve_for_cwd` against a path the wrapper no longer calls.

**Action:** delete `tenant.py`, `cmd_tenant`/`cmd_tenants` and their argparse wiring
(`dax.py:1312-1392`, `1906-1916`), the `dax-creds resolve-tenant` subcommand
(`dax_creds/cli.py:54-72`), the `.dax-tenant` file convention, and both test files.
Salvage `all_tenant_projects` (~15 lines) into wherever `_known_tenant_names` lives.
Keep `_prompt_tenant`/`_known_tenant_names` — `dax process new` genuinely needs them.

### 2. Dead writes in the live path

`cmd_run` (`dax.py:706-707`):

```python
config['multi_tenant'] = bool(project.get('multi_tenant'))
config['tenant_subdir'] = project.get('tenant_subdir', '')
```

Nothing reads either key — `feature_claude_tenant_state` only touches
`config['tenant']` and `config['workdir_name']`. Fossil from when Gate A ran here.
Delete alongside item 1.

### 3. Provider duplication — the one place to *add* structure

`_KEYCHAIN_SERVICE = 'dax-creds'` is defined as an independent literal six times
(`daemon.py`, `init.py`, and all four of `providers/{claude,github,google,auggie}.py`).
`claude.py` and `auggie.py` also reimplement `_require_keyring()`/`check()`/
`get_token()` verbatim. This is present-tense duplication, not hypothetical future
variance — a `_KeyringBackedProvider` base class with one `SERVICE` constant removes
~40 lines and six sources of truth for the same string.

### 4. Leftover limbs from dax's general-tooling era

`feature_msf`, `feature_ovpn` (ships its own permanent `WARNING: ovpn feature not
fully implemented`, `dax.py:277`), `feature_X11`, `feature_optdir`, `feature_aws` —
referenced only in `dax.py` and their own unit tests, absent from every design doc
and decision log since the credential/tenant/substrate work began. Decide: delete, or
mark formally unsupported/legacy rather than leaving a permanent "not fully
implemented" warning with no path to resolution.

### 5. `cmd_run`'s 180-line try block ends in a bare `except (FileNotFoundError, KeyError): pass`

`dax.py:646-738`. `FileNotFoundError` for "no `~/.dax.yaml` yet" is intentional and
documented. `KeyError` riding along in the same clause is a landmine: a genuine
`KeyError` bug anywhere in that block (project resolution, credential resolution, SSH
bootstrap, feature promotion) disappears into "credentials silently not loaded"
instead of a stack trace. Split the except clauses.

### 6. Duplicate questionary wrapper helpers

`init.py` has `_q_text`/`_q_select`/`_q_confirm`/`_q_checkbox`. `dax.py` has its own
`_q_select_tenant`/`_q_text_tenant` doing the identical pattern for the identical
reason (testability injection). Once item 1 is done, `_prompt_tenant` should import
`init.py`'s helpers instead of maintaining a second copy.

### 7. Judgment call, not a verdict: `tools/migrate_env_state.py`

428 lines plus its own doc and discovery-report logic. Per CLAUDE.md, never run with
`--apply` against real data as of this session. Not cruft — the ancestor/commingled-
parent problem it guards against is real, and it deliberately refuses to auto-guess.
But worth revisiting whether the discovery-report logic alone plus a documented
manual runbook would capture most of the value at a fraction of the maintenance
surface, next time it's actually run for real.

## Priority order

1. Delete the tenant subsystem (items 1 + 2) — highest value by far: ~300 source
   lines, ~1000 test lines, and an entire "two gates must agree" mental model that no
   longer needs to exist.
2. Provider base class (item 3) — mechanical, low-risk, removes real duplication.
3. Fix the bare `except (FileNotFoundError, KeyError)` (item 5) — five minutes,
   prevents a real future debugging session.
4. Decide on the legacy features (item 4).
5. Collapse the duplicate questionary wrappers (item 6) — falls out for free once
   (1) is done.

None of this is architecture surgery. It's running the deletions the design docs
already decided on.
