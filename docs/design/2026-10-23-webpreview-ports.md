# Webpreview ports and the env index page

Started 2026-10-23. Two linked changes:

- an index page listing every env, with a link to its webpreview, that the user bookmarks
  once in Firefox;
- stored, unique webpreview ports, so those links are always right.

## Why

Today the port is a hash of the env's folder path, mapped into 8000–8999, and it moves to the
next free port if that one is busy at launch. That makes it "usually the same". A bookmark, or a
page of links, needs "always the same". Storing the port removes the guesswork.

## Decisions

### 1. The index page (option 4 of four considered)

`dax` writes a local HTML page listing every registered env, grouped by tenant like
`dax env list`, with running envs marked. The user bookmarks it once, anywhere, in any browser.

Rejected:

- a Firefox policy file (read only at startup, read-only folder, wiped by updates);
- writing Firefox's bookmark database (locked while Firefox runs; corruption risk);
- a Firefox extension (far too much machinery).

### 2. Ports are assigned once and stored

Each env's port is stored in its entry in `~/.dax.yaml`. The hash of the folder path is only
the starting guess when a port is assigned. It moves up past ports already assigned to other
envs, so ports are unique across envs by construction. There is no ports file: the stored value
is the truth.

### 3. Existing envs get ports in a one-time pass, re-runnable

A single command assigns ports to all envs that lack one. It can be run again after changing
the base port. Ports are written in exactly two places: the creation wizard (inside `dax run`,
which already writes the new env's entry) and `dax env ports --assign`. An ordinary launch of an
existing env never writes a port.

### 4. The pass is `dax env ports --assign`

- `dax env ports` prints every env with its stored port, or "none". It only reads.
- `--assign` gives a port to every env without one, renumbers every env whose port lies
  outside the current base range (the case after a base change), and leaves the rest alone.
  Running it twice with nothing changed does nothing.
- It shows the changes and asks before writing, keeps `~/.dax.yaml.prev`, and runs on the host
  only, like `dax config`.
- It lists any renumbered env that is running, since that container keeps its old port until
  restarted.

### 5. The key is the env entry's `webpreview: {port: N}`; the per-folder override is retired

One place to look for an env's port. A `webpreview:` block in an env folder's own `.dax.yaml`
is ignored, with a warning on every `dax run`, the way other retired keys are handled. The
nested form names what the port is for and leaves room for other webpreview settings.

### 6. At launch, a busy port means the env does not start

If an env's stored port is in use, `dax run` stops before Docker starts and names the port and
the env. There is no automatic move to another port; the user has never hit a collision in
practice, and a silent move would put the env on a port the config and the index page don't
know about. The hash-and-move-up search remains only inside port assignment.

An env with no stored port also doesn't start; the message says to run
`dax env ports --assign` (confirmed).

### 7. Restored envs get a port at restore

Restore registers a new env, so it assigns a port the same way the wizard does.

### 8. The base is `defaults.webpreview_base_port`

Default 8000, with a 1000-port range. Changing it renumbers nothing by itself; the next
`dax env ports --assign` moves envs whose ports fall outside the new range.

### 9. The index page

- At `~/.local/state/dax/envs.html` on the host. Every registered env, grouped by tenant in
  `dax env list` order, hidden tenants left out, each with a link to
  `http://localhost:<port>/`.
- Rewritten by the creation wizard, by every non-preview `dax run`, by `dax env list`, and by
  `dax env ports --assign`. `dax -t run` writes nothing.
- **Running is marked live:** a few lines of script in the page try each port when the page is
  opened, and mark each row by whether something answered. The mark doesn't depend on the file
  being rewritten. The list itself can go stale only between config changes, which are what
  rewrite it.

Work is on the branch `feature/webpreview-index`.
