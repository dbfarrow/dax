# Manual test: shared-files sync (`sync_claude_shared_files`)

Verifies the copy-with-drift-detection mechanism that replaced the abandoned
read-only file mount (see `CLAUDE.md`'s "Built, same day" note under decision
B2). All host commands — the sync runs while `dax run` assembles the docker
command, before any container exists, so no image rebuild is needed.

## 0. Check where fabric's tree currently stands

```
diff ~/.claude/CLAUDE.md ~/.local/state/dax/tenants/personal/fabric/CLAUDE.md
diff ~/.claude/settings.json ~/.local/state/dax/tenants/personal/fabric/settings.json
```

fabric's tree already holds a hand-copied `CLAUDE.md` from before this was
built (see CLAUDE.md's item 2). If these differ, the first run below should
warn rather than silently overwrite — that's expected, not a bug.

## 1. First run under the new code

```
dax run fabric
```

- No warning + `diff` in step 0 showed no difference: confirm the tree still
  matches host — nothing should have changed.
- No warning + step 0 showed a difference: unexpected — the manifest has no
  prior record, so a differing tree should have been treated as drift, not
  silently overwritten. Worth investigating before trusting the mechanism.
- Warning (`[!] fabric: CLAUDE.md in state tree differs from both host and
  last-synced copy...`): expected if step 0 showed a difference. Confirm the
  tree file is untouched, then:

  ```
  dax env accept-shared-files fabric
  dax run fabric   # should be silent now
  ```

## 2. Host moves on — should sync silently

```
echo '<!-- sync test -->' >> ~/.claude/CLAUDE.md
dax run fabric
cat ~/.local/state/dax/tenants/personal/fabric/CLAUDE.md   # should show the new line
```

No warning expected — the tree's copy was untouched since the last sync, so
this is the "host moved forward" case. Revert the host edit afterward:

```
sed -i '' '/<!-- sync test -->/d' ~/.claude/CLAUDE.md   # macOS sed
```

## 3. Tree edited independently — should warn, not clobber

This is the actual risk the manifest exists to catch: a container can now
write `~/.claude/CLAUDE.md` directly (a plain file, not a mount).

```
# inside the fabric container
echo 'edited from inside the container' >> ~/.claude/CLAUDE.md
exit
```

```
# on the host
echo '<!-- host moved on too -->' >> ~/.claude/CLAUDE.md
dax run fabric
```

Expect the `[!]` warning, and the tree's file to still hold the in-container
edit — not the host's. Then:

```
dax env accept-shared-files fabric
cat ~/.local/state/dax/tenants/personal/fabric/CLAUDE.md   # now matches host
```

## 4. Confirm Claude Code actually sees it

The original bug (2026-07-31) was silent loss — a wholesale tree mount simply
made the global `CLAUDE.md` disappear, no error. Worth confirming once for
real rather than trusting the file copy alone:

```
# inside the fabric container
cat ~/.claude/CLAUDE.md   # should be the real global content
claude                    # should behave per your global instructions
```

## 5. Manifest hygiene

```
cat ~/.local/state/dax/tenants/personal/fabric.shared-files.json
ls ~/.local/state/dax/tenants/personal/fabric/   # manifest must NOT appear here
```

Sibling of the tree, three sha256 hashes, valid JSON — and absent from the
tree itself, since anything inside it is what the container sees at
`~/.claude`.
