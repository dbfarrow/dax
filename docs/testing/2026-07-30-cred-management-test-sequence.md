# Credential Management — Manual Test Sequence

Written 2026-07-30, after the container rebuild that picked up the C2/C3/C4
credential work (see `docs/design/2026-07-27-tenant-isolation.md` decisions
C, C2, C3, C4 and G2).

**Why a manual sequence at all.** The automated suite covers resolution,
naming, listing, and the import guards, but three things it cannot reach are
exactly where the real bugs were: the macOS **Keychain**, the **Chrome profile**
a login opens, and whether two credentials are **the same OAuth grant**. The
2026-07-30 sharing bug (C3) passed every unit test and authenticated perfectly.
Only step 3 below catches that class of failure.

**Where things run.** `dax` commands that touch credentials are **host**
(macOS) commands — keyring is unreachable from inside a container, so
`dax creds list` there always reports `stored: no` / `[!] key on disk`. Env
commands are host commands too: `~/.dax.yaml` records host paths
(`/Users/dfarrow/...`), so `dax env show` from inside a container can't match
cwd to an env.

Run the steps in order. Steps 0–4 are non-destructive; step 5 mints a real
OAuth grant; step 6 overwrites a stored secret.

---

## 0. Container — did the rebuild take?

Run inside a freshly launched container, from the repo:

```bash
python3 -m pytest -q
diff -rq /usr/local/lib/python3.14/dist-packages/dax_creds ./dax_creds \
    -x __pycache__ -x '*.pyc'
diff -q /usr/bin/claude ./dax_creds/wrappers/claude
python3 -c "import ruamel.yaml; print('ok')"
dax-creds list
env | grep DAX_CREDS_CLAUDE
```

| Check | Expected output |
| --- | --- |
| pytest | `342 passed` (as of 2026-07-30) |
| installed `dax_creds` vs repo | exactly `Only in ./dax_creds: wrappers` — **and nothing else.** That one line is expected: wrappers install to `/usr/bin`, not into the package. Any other line means the image is running different code than the repo. |
| `/usr/bin/claude` vs repo wrapper | **no output** → identical → inject-if-absent shipped |
| `ruamel.yaml` | `ok` (the `Dockerfile.tmpl` pip line took) |
| `dax-creds list` | the env's resolved credential names, e.g. `claude-personal-dax  claude` |
| `DAX_CREDS_CLAUDE` | `claude-<tenant>-<project>`, e.g. `claude-personal-dax` |

Two of the six pass by printing **nothing** — the `diff -q` on the wrapper, and
`diff -rq` if wrappers ever stop being the only difference. Silence is the pass
there; count the lines rather than skimming, because a missing check reads
identically to a passing one. Expect four blocks of output from six commands.

The last two are the end-to-end proof that C2's derived naming survives all the
way from `~/.dax.yaml` through `dax run` into the container's environment.

**This matters because `dax_creds` in a container is a frozen, non-editable
install.** Editing the repo changes nothing about what the container actually
runs until the image is rebuilt; the `diff -rq` above is what tells you the two
are in step.

Recorded 2026-07-30: all six passed, confirmed twice in separate containers.

## 1. Host — editable install took, and the config round-trip is byte-stable

```bash
cd ~/fatsec/dax && pip install -e . && python3 -c "import ruamel.yaml; print('ok')"
cp ~/.dax.yaml /tmp/dax.yaml.bak
dax env set dax tenant personal        # writes through save_config
diff /tmp/dax.yaml.bak ~/.dax.yaml     # must be empty
```

Expect `[dax] tenant: personal -> personal` from the set, and an **empty diff**
— meaning comments, key order, long unwrapped `dir:` paths, and the
commented-out `#- claude_tenant_state` toggle all survived a real write.

Setting the field to the value it already holds is a **valid** probe here:
`run_env_set` calls `save_config(config)` unconditionally, with no equality
short-circuit (`dax_creds/init.py:649`), so a no-op value still triggers a full
serialize-and-rewrite. That's the point — it exercises the write path while
leaving the file's *content* unchanged, so any diff at all is pure round-trip
damage rather than the edit you asked for. Verified 2026-07-30.

The host is an editable install and the container is not, so this step gates
everything else on the host side.

## 2. Host — resolution and listing agree with each other

```bash
dax creds list
dax env show dax
dax envs list
```

- `dax creds list`: one row per derived Claude credential, tagged `(derived)`,
  `stored: yes`, and a populated "used by" column. On the host this column is
  meaningful — it's Keychain-backed.
- `claude-fre` should show as used by **nothing**. That's the credential whose
  name was typo'd (`claud-fre` in an env's `creds:`) and sat unused for weeks;
  the resolving "used by" column is what finally makes it read as dead.
- `dax env show dax`: prints the *resolved* credential name, so the derived
  convention is visible rather than magic.
- `dax envs list`: TENANT/STATE/USED columns; `?` marks a state tree with no
  registry entry, `!` a missing directory, `*` running.

Cross-check against step 0's `dax-creds list`. Host and container disagreeing
about which credentials exist is itself the bug C4's related fix closed.

## 3. Host — grant independence (**the load-bearing check**)

```bash
python3 tests/manual/check_claude_grants.py
```

Expect `PASS — N distinct grant(s) across N credential(s); no sharing.`

Per-env credentials only isolate anything if **each came from its own login**.
Two entries holding the same refresh token are one grant wearing two names:
they authenticate fine, nothing looks wrong, and a rotation on either kills
both. That is precisely what happened on 2026-07-30 (C3). No other check in
this document, and no test in the suite, detects it.

Also read the final line:

```
~/.claude/.credentials.json  grant:abc123def456  matches: claude-personal-dax
```

While `feature_claude` still mounts `~/.claude` wholesale, whichever env wrote
last owns that file and *every* non-tenant-state container reads it. This line
tells you which env's grant your sessions are actually running on.

Refresh tokens are never printed — only a 12-char hash, enough to compare.
Exits non-zero on any sharing, and prints the `dax creds remove` commands to
clear the duplicates.

**What a PASS does and does not prove.** The refresh token stands in for grant
identity because the blob carries no grant ID. That catches a copy only while
both entries still hold the same token: if each side refreshes independently
afterward, both get new tokens while still descending from one grant, and this
reports PASS. Read the `refresh expires` column alongside it — dates spread
across the days the logins actually happened corroborate separate mints; tightly
clustered dates with differing tokens is the shape a shared-then-rotated grant
would take. A PASS is strongest immediately after minting, so run this **right
after** step 5 rather than only weeks later.

Recorded 2026-07-30: PASS, 4 distinct grants across 4 credentials, refresh
expiries staggered 08-27 / 08-28 / 08-29 / 08-28 — consistent with separate
mints on separate days. `~/.claude/.credentials.json` matched
`claude-personal-dax`.

## 4. Host — C3 and C4 in a single launch, non-destructively

```bash
dax creds login claude-personal-fabric
```

Observe two things from this one run, then **cancel without completing** —
close the browser tab, or Ctrl-C:

1. **C4:** it opens **Chrome, Profile 2** — not the default browser. This
   proves the top-level `credential_defaults: {claude: {browser, chrome_profile}}`
   block reached the *synthesized* definition. A derived credential has no
   `credentials:` entry to carry those keys, so before C4 every per-env login
   opened the wrong browser.
2. **C3:** on cancel it **refuses to import** and reports that the provider's
   on-disk token was unchanged. Previously `_post_login_import` ran
   unconditionally after `subprocess.run()`, read the existing shared token out
   of `~/.claude/.credentials.json`, and stored it under the new per-env name.

Then re-run step 3 and confirm no new shared grant appeared.

This mints nothing and costs one cancelled browser tab, which makes it the
cheap regression test for the bug that drove this entire pass. Run it first,
before step 5 changes any state.

**If you complete the flow instead of cancelling, it is not read-only.**
`_LOGIN_PROVIDERS['claude']` mounts the host's `~/.claude` (`dax.py:644`), so a
successful login writes its new credential into the **shared**
`.credentials.json`. While `feature_claude`'s wholesale mount is still in use,
that repoints *every* container: inject-if-absent skips a non-empty file, so each
one then authenticates on whichever env logged in last, regardless of its own
`DAX_CREDS_CLAUDE`. Re-run step 3 afterward and read the "matches" line to see
which env owns the shared file now. (This is also why the C3 guard works — the
flow writes to the same file it snapshots.)

Recorded 2026-07-30: run after the C5 fix. Chrome opened on Profile 2, the
device-flow `redirect_uri` was rewritten to
`platform.claude.com/oauth/code/callback`, the flow was **completed** rather than
cancelled, and the grant hash changed `49a03025bda2` → `56b0ef07fcbe` — direct
proof of a new grant, since a copy would have carried another credential's hash.
The
shared file's owner moved from `claude-personal-dax` to `claude-personal-fabric`.

A second run was then **cancelled** at the paste-code prompt, confirming the C3
refusal live: the "predates this login" message printed and Keychain was
untouched (`56b0ef07fcbe` unchanged). Both halves of step 4 are now confirmed
against real infrastructure. See decision C6 for the residual hole this
inspection turned up — the guard compares the whole on-disk envelope, so a token
refresh landing inside the login window can still let an abandoned login import
another env's grant.

## 5. Host — mint a real per-env grant

```bash
dax creds remove claude-personal-dax    # derived → clears Keychain only, no config write
dax creds login claude-personal-dax     # complete it this time
python3 tests/manual/check_claude_grants.py
```

Expect a **distinct** grant hash for `claude-personal-dax`, different from every
other row.

`dax creds remove` on a derived name is exempt from the still-referenced
refusal: the reference is the naming convention itself, which survives removal
and simply re-mints on the next run. Before C3's corollary fix, a derived
credential couldn't be removed at all — it was rejected as unknown, so a bad
one was unclearable.

**Use `login`, never `add`.** `dax creds add`'s claude path still imports from
`~/.claude/.credentials.json` with no before/after guard, so minting a derived
name that way would silently share a grant — the exact C3 failure, through the
one door that's still open. This is a known gap, not an oversight of this doc.

## 6. Host — a confirmed overwrite actually overwrites (OPTIONAL, low value)

**Downgraded 2026-07-30 after reading the code paths.** All three behaviors are
already covered by unit tests (`tests/test_dax_creds_init.py:281–320`: replace
rewrites the secret, replace reports "left in place", no-replace still skips),
and running it live carries a real hazard, so this is optional rather than part
of the required pass.

**The hazard: `dax creds add` on an existing credential is lossy.**
`_define_credential` rebuilds the definition from scratch (`init.py:113`) and
`_run_creds_add` assigns it wholesale (`init.py:369`), so **any field the prompts
don't ask for is silently dropped**. Use `dax creds update` for metadata edits.
Copy the entry out of `~/.dax.yaml` before running this, and retype every field
exactly.

Target selection matters, and the obvious choice is wrong:

| Target | Rebuild | Reaches the "left in place" message? |
| --- | --- | --- |
| `github-dbfarrow` | lossless (`{browser, provider}`) | **no** — with no `client_id` it exits at "skipped — no client_id provided" |
| `gmail-personal` | lossless only if `client_id`, `client_secret`, `scopes` are retyped exactly | **yes** — goes straight to `_report_no_token` |
| `claude-fre` | lossless | yes, but **never do this** — claude's replace path calls `import_from_disk`, copying whatever is in the shared `~/.claude/.credentials.json` into this name and manufacturing the shared grant C3 exists to prevent |

So if you run it, run it on `gmail-personal`:

```bash
dax creds add        # name: gmail-personal, answer yes to "Overwrite it?", provider: gmail
```

Expect `no token found` plus the two "left in place" lines — where **not**
overwriting is the correct outcome, since Google's setup has nothing to import.
The pass condition is that the early-exit was bypassed at all: without
`replace=`, this prints `refresh token already in Keychain` and stops.

The old `if provider.check(...): return` early-exit made that confirmation a
lie — YAML metadata was rewritten while the Keychain secret went untouched, so
a rotated credential couldn't be re-imported without `dax creds remove` first.
Every `_setup_*_credential` now takes `replace=`, set only on a confirmed
overwrite.

If nothing new is found to store, it must say **explicitly that the old secret
survived** and how to clear it — not print the ordinary "no token found" and
leave you believing the store was emptied.

## 7. Container — end-to-end, per-env credential in use

```bash
dax run dax
# inside the container:
env | grep DAX_CREDS_CLAUDE                    # claude-personal-dax
stat -c %y ~/.claude/.credentials.json
claude                                          # authenticates
```

Then test both halves of inject-if-absent:

- Exit `claude`, relaunch it, `stat` again → **mtime unchanged.** The wrapper
  did not clobber a live token with the Keychain snapshot.
- `rm ~/.claude/.credentials.json`, relaunch → recreated from Keychain. The
  wrapper's test is `-s`, not `-e`, so a zero-byte file left by an interrupted
  write also counts as absent.

Keychain is a **bootstrap**; Claude Code owns rotation from first launch onward.

## 8. Multi-day soak — the residual unknown

Two things only time reveals:

- **Cross-grant independence.** Use env A's Claude session, then launch env B's,
  then return to A. All must keep working. It's unknown whether Anthropic caps
  concurrent grants per account or invalidates older grants for the same
  `client_id`.
- **Refresh-token rotation.** Re-run step 3 after several days. No env's
  credential should have died. Because teardown write-back to Keychain is
  **not built** (deferred by choice — decision C), the Keychain snapshots go
  stale by design. Inject-if-absent is what makes that survivable: a stale
  snapshot is never forced over the tree's working token.

---

## What this sequence cannot yet reach

`feature_claude_tenant_state` is still in its **pre-redesign shape**. It mounts
trees at `~/.local/state/dax/tenants/<tenant>/<project>` and still emits
`DAX_MULTI_TENANT`/`DAX_TENANT_SUBDIR`, and the `claude` wrapper still carries
Gate B calling `dax-creds resolve-tenant`. **Decision B — state tree mounted at
`~/.claude` with `CLAUDE_CONFIG_DIR` as a constant — is not built.**

So step 7 exercises inject-if-absent under the **shared** `~/.claude` mount,
where the divergence it exists to prevent cannot actually arise: there's one
tree, and Claude Code refreshes it in place. The failure mode the logic guards
against — a session refreshing into its own persistent per-project tree, and
the next launch overwriting it with the stale Keychain snapshot — stays
unit-tested only until decision B lands.

**Do not enable `claude_tenant_state` to test this.** You would be exercising
the abandoned multi-tenant design, whose code is slated for deletion (decision
E), not extension.

## Known-open items, so a failure here isn't misread as a regression

- `dax creds add`'s claude path has no `_disk_token_for` guard (step 5).
- Teardown write-back to Keychain is deferred (step 8).
- A credential named literally `claude` would collide with the bare token in an
  env's `creds:` list. Not blocked in `dax creds add`.
- Debug scaffolding still present: `~/.dax-debug/open-url.log` in the
  `dax-creds-open-url` wrapper, and the `~/.dax-debug` mount in
  `_LOGIN_PROVIDERS`.
