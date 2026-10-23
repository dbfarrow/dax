# dax

Dax wraps Docker to give you reproducible, isolated development environments that feel like a local shell. The current directory is always mounted as your work directory, dotfiles follow you in, and credentials are brokered securely from the host — no secrets ever baked into images.

The core idea: your environment should be cattle, not pets. Spin one up, do the work, tear it down. Everything that matters lives on the host.

---

## Contents

1. [Install & Build](#install--build)
2. [Quick Start](#quick-start)
3. [Configuration](#configuration)
4. [Credential Management](#credential-management)
5. [MCP Servers](#mcp-servers)
6. [Environment Management](#environment-management)
7. [Features Reference](#features-reference)
8. [Adding Features](#adding-features)

---

## Install & Build

**Prerequisites:** a Mac (credentials live in the macOS Keychain), Python 3.7+, Docker Desktop

```bash
git clone https://github.com/dbfarrow/dax.git
cd dax
pip install -e .           # also installs keyring, for the Keychain
```

Build the image:

```bash
dax build
```

Use `-c` to force a clean build (no layer cache):

```bash
dax build -c
```

The build prompts for a container password unless a `.daxpw` file exists with `0600` permissions.

### Versions

dax uses semantic versioning; the current version is in `VERSION`, and `dax --version` prints it. Each `dax build` labels the image with the version it was built from, and `dax run` warns when the image it's about to launch was built by a different version — the dax code inside the image only changes on a rebuild, so that warning means it's time for `dax build`. What changed in each release is in [CHANGELOG.md](CHANGELOG.md), including what to change in `~/.dax.yaml` when coming from 2.x.

---

## Quick Start

dax needs a `~/.dax.yaml`, and never creates one itself. A minimal one:

```yaml
defaults:
  image: dax:latest
projects: {}
```

`dax build` tags the image `dax:<version>` and `dax:latest`. (`dax-base`, which older configs carry, still works as another name for `dax:latest`.)

Then make a folder for the env and run it there:

```bash
mkdir ~/work/my-project && cd ~/work/my-project
dax run
```

The first `dax run` in a folder that isn't registered yet starts a short wizard: image, tenant, which of your existing credentials to attach, and whether this is a plain coding project or a substrate-backed process (see [`substrate`](#features-reference)). It shows a summary, registers the env (named after its folder) on confirm, and launches straight into it. Every later `dax run` there just launches.

You're in a container with your project mounted, dotfiles present, Claude Code set up with this env's own state and credential, and any credentials you attached available.

---

## Configuration

All configuration lives in `~/.dax.yaml`. Edit it with `dax config`, which opens it in `$VISUAL`/`$EDITOR`/`vi`, keeps the version from before your edit as `~/.dax.yaml.prev`, and checks the result (syntax, duplicate keys, shape, unknown keys) before you leave. A `.dax.yaml` in the folder you run `dax run` from can add `features:` for launches from there, and override any top-level setting, such as `ports:`.

### Example

```yaml
defaults:
  image: dax:latest
  relogin_cooldown_hours: 24

dotfiles:
  ro:
    - ~/.zshrc
    - ~/.vimrc
    - ~/.tmux.conf
  rw:
    - ~/.gitconfig

backup_dir: ~/backups/dax       # where `dax env archive` writes (optional)
substrate: ~/src/virgil         # the one substrate every process shares (optional)

credential_defaults:
  claude:
    browser: chrome
    chrome_profile: Profile 2

tenants:                        # per-tenant overrides of credential_defaults (optional)
  ysecurity:
    claude:
      chrome_profile: Profile 5

credentials:
  github-dfarrow:
    provider: github
    browser: firefox
  ssh-id-ed25519:
    provider: ssh
    key: ~/.ssh/id_ed25519

projects:
  my-project:
    dir: /Users/dfarrow/work/my-project
    tenant: personal
    creds:
      - github-dfarrow
      - ssh-id-ed25519
  some-process:
    dir: /Users/dfarrow/work/some-process
    tenant: ysecurity
    features: [substrate]
    mounts: [~/discernment]
```

### Top-level keys

| Key | Description |
| --- | --- |
| `image` | Image for envs that don't set their own (wins over `defaults.image`) |
| `defaults` | `image` (fallback image), `timezone` (used only when the host's can't be detected), `relogin_cooldown_hours` (how often `dax run` refreshes an env's Claude login; default 24) and `webpreview_base_port` (the start of the 1000-port range webpreview ports are assigned from; default 8000) |
| `githubdir`, `awsdir`, `optdir`, `msfdir` | What the `github`, `aws`, `optdir` and `msf` features mount, each as `{mount: ~/.aws}` (same path inside) or `{host: ~/x, container: ~/y}`; required only by the feature that uses it |
| `ports` | Extra `host:container` port mappings for the `ports` feature, e.g. `["8080:80"]` |
| `dotfiles` | Files to mount into every container (`ro` / `rw`) |
| `backup` | Host paths `dax backup` copies into `backup/` in this checkout (gitignored, local only) |
| `backup_dir` | Where `dax env archive` writes and `dax env restore` reads; defaults to `backup/` in this checkout |
| `substrate` | The one substrate repo every process env shares (see the `substrate` feature) |
| `credential_defaults` | Per-provider browser settings for derived credentials, e.g. every env's Claude login |
| `tenants` | Per-tenant settings: which browser a tenant's Claude logins open (merged over `credential_defaults`), and `hidden: true` to leave a tenant out of `dax env list` unless `--all`. Its order is the order `dax env list` shows tenants in |
| `credentials` | Named credential definitions (managed by `dax creds`) |
| `projects` | Registered environments (created by `dax run`'s wizard, edited with `dax config`) |

### Env keys

| Key | Required | Description |
| --- | --- | --- |
| `dir` | yes | Absolute path to the env's folder on the host |
| `tenant` | yes | Grouping label. Names the env's Claude credential (`claude-<tenant>-<env>`) and its state tree (`~/.local/state/dax/tenants/<tenant>/<env>/`) |
| `image` | no | Docker image for this env (the wizard fills in `dax:latest`) |
| `creds` | no | Credentials to make available. Claude's is implied; don't list it |
| `features` | no | Opt-in features beyond the always-on ones, e.g. `[substrate, auto_claude]` |
| `webpreview` | yes, to launch | `{port: N}`, the env's webpreview port. Written by the wizard, restore and `dax env ports --assign`; change it by hand with `dax config` |
| `mounts` | no | Extra host paths, each mounted read-write at `$HOME/<name>`. `~/x:other` mounts it as `$HOME/other`, and a trailing `:ro` (`~/x:other:ro`, or `~/x::ro` to keep its name) makes it read-only. A non-empty list turns mounting on by itself |

A list key that would be empty can just be left out, or left as `creds:` with nothing after it. `dax run` warns about any key it doesn't recognize, and about entries that are no longer needed: `claude`, `claude_tenant_state`, `auggie` or `mounts` in `features:`; Claude or auggie credentials in `creds:`; a per-env `substrate:`, `multi_tenant` or `tenant_subdir`; and a `webpreview:` block in a folder's own `.dax.yaml` or at the top level of `~/.dax.yaml`. CHANGELOG.md's "Updating `~/.dax.yaml` from 2.x" lists what to do about each.

---

## Credential Management

Dax includes a credential brokering system so tools inside the container can authenticate without secrets ever being written into the image or passed on the command line.

**How it works:** When `dax run` starts, a credential daemon launches on the host. The container connects to it and requests tokens by name. Each credential type has its own provider that handles storage and acquisition. Secrets live in macOS Keychain.

### Providers

#### ssh

Loads an SSH private key from macOS Keychain into an ephemeral SSH agent. The agent socket is forwarded into the container. Private keys never touch the container filesystem.

```yaml
credentials:
  ssh-id-rsa:
    provider: ssh
    key: ~/.ssh/id_rsa
```

#### github

Authenticates via GitHub's OAuth device flow (the same flow as `gh auth login`). The resulting token is stored in macOS Keychain. Inside the container, the `gh` CLI wrapper fetches the token from the daemon and sets `GH_TOKEN` automatically — so `gh` commands use the right account for the project, regardless of what's in `~/.config/gh/hosts.yml`.

```yaml
credentials:
  github-dfarrow:
    provider: github
    browser: firefox        # browser used for the OAuth flow
  github-work:
    provider: github
    browser: chrome
    chrome_profile: Profile 3
```

#### claude

Every env has its own Claude credential, named `claude-<tenant>-<env>`, created by its own login so it's an independent OAuth grant: a token refresh in one env can never invalidate another's. You never list it in `creds:`; it's implied by the env's `tenant`. Dax does **not** use Anthropic API keys.

The login runs in a throwaway container against a private temporary folder (never your host `~/.claude`), and the token goes straight into Keychain. On each launch the `claude` wrapper writes it into the env's own state tree, and on container stop dax syncs the refreshed token back to Keychain and removes it from disk. `dax run` reruns the login before launch when the last one is older than `defaults.relogin_cooldown_hours`.

Browser and Chrome profile for these logins come from `credential_defaults.claude`, with the env's tenant's own `tenants.<tenant>.claude` merged over it. So a client's envs can log in through that client's Chrome profile while everything else uses your default:

```yaml
credential_defaults:
  claude:
    browser: chrome
    chrome_profile: Profile 2
tenants:
  ysecurity:
    claude:
      chrome_profile: Profile 5
```

A tenant that sets `browser` replaces the default's `chrome_profile` too, unless it sets its own. A `chrome_profile` only takes effect with `browser: chrome`; set on its own, as above, it relies on the default's browser being Chrome.

The `dax run` wizard asks which browser to use the first time it sees a new tenant ("Same as default" writes nothing). `dax env show` says which browser an env's login opens and where that setting came from. `dax run` and `dax config` warn about a `tenants:` entry that no env uses, or one that can't take effect.

#### gmail

Authenticates with Google via OAuth 2.0 using a Desktop app client you create in Google Cloud Console. The refresh token is stored in macOS Keychain. Inside the container, the credential daemon exchanges it for a short-lived access token on demand.

Dax does **not** use a Google service account or API key. The OAuth flow issues tokens scoped to exactly the permissions you specify — for read-only inbox access, use `gmail.readonly`.

```yaml
credentials:
  gmail-personal:
    provider: gmail
    client_id: 1234567890-abc.apps.googleusercontent.com
    client_secret: GOCSPX-...
    scopes:
      - gmail.readonly
    browser: default
```

**Setup:** Create a GCP project, enable the Gmail API, and create an OAuth 2.0 client ID of type *Desktop app*. Under *OAuth consent screen*, add your Google account as a test user (required while the app is in testing status). Pass the client ID and secret to `dax creds add`.

**Scope aliases:** `gmail.readonly`, `gmail`, `gmail.send`, `gmail.compose`, `gmail.insert`, `gmail.labels`, `gmail.metadata`, `gmail.modify` expand to their full `https://www.googleapis.com/auth/...` URLs automatically.

**Environment variables set in the container:**

| Variable | Value |
| --- | --- |
| `DAX_CREDS_NAMES` | Comma-separated list of all credential names available |
| `DAX_CREDS_GMAIL` | Name of the first Gmail credential (e.g. `gmail-personal`) |

### Commands

#### `dax creds add`

Interactively define a credential: provider first, then provider-specific options. For Claude it asks which env the credential is for, derives the name, and hands off to `dax creds login`. It never copies an existing token off disk, since that would share a grant.

#### `dax creds list`

Show every credential, registered and derived (each env's Claude credential), with status:

```
  name                          provider  stored  detail                        used by                         relogin     warnings
  ----------------------------  --------  ------  ----------------------------  ------------------------------  ----------  ------------------------
  github-me                     github    yes     Firefox                       my-project                      -
  ssh-id-ed25519                ssh       yes     ~/.ssh/id_ed25519             my-project                      -
  claude-personal-my-project    claude    yes     default browser               my-project                      3h
```

| Column | Meaning |
| --- | --- |
| stored | Whether the secret is in macOS Keychain |
| detail | Key path (ssh) or browser config |
| used by | Envs that use this credential |
| relogin | For Claude: when its login last ran, and `due` once past the cooldown |
| warnings | A plaintext copy of the token on disk (see below) |

#### `dax creds update [name]`

Update credential metadata (browser, Chrome profile, key path). Omit `name` to pick interactively.

#### `dax creds login <name>`

Run the first-party auth flow for a credential and store the result in Keychain:

- **ssh**: loads the key via `ssh-add --apple-use-keychain`
- **github**: the OAuth device flow in the configured browser
- **claude**: the Claude OAuth flow in the configured browser, into a private temporary folder
- **gmail**: the Google OAuth consent screen; stores the refresh token

#### `dax creds remove <name>`

Remove a credential from both Keychain and `~/.dax.yaml`. Refuses while any env still uses it. A derived Claude credential has no config entry, so only its Keychain secret is removed (the env is offered a fresh login on its next `dax run`).

### Warnings

`dax creds list` flags credentials where a plaintext copy of the token exists on disk:

- **`[!] no passphrase`** (ssh): the key file isn't passphrase-protected.
- **`[!] token on disk`** (github): `~/.config/gh/hosts.yml` contains an `oauth_token`. Written by `gh auth login`; the copy in Keychain is what dax uses.
- **`[!] key on disk`** (claude): the env's state tree holds a `.credentials.json`. Normal while its container runs; after it stops, it means a teardown didn't get to sync it back to Keychain and clear it.

---

## MCP Servers

Dax containers include MCP (Model Context Protocol) servers that give Claude Code inside the container access to external services. Servers are configured in `~/.mcp.json` (baked into the image) and loaded automatically when Claude Code starts — no manual setup required.

### dax-gmail

Gives Claude Code read-only access to Gmail. Requires a `gmail` credential with `gmail.readonly` scope to be available in the container (`DAX_CREDS_GMAIL` must be set).

#### Tools

**`gmail_search`** — Search Gmail threads using the same query syntax as the Gmail search box.

```
query        Gmail search string, e.g. "subject:proposal from:alice after:2025/01/01"
max_results  Max threads to return (default 10, max 50)
account      dax-creds credential name (defaults to DAX_CREDS_GMAIL)
```

Returns a list of `{id, snippet}` objects. Pass an `id` to `gmail_get_thread` to read the full thread.

**`gmail_get_thread`** — Fetch the full message chain for a thread, including all replies.

```
thread_id    Thread ID from gmail_search
account      dax-creds credential name (defaults to DAX_CREDS_GMAIL)
```

Returns each message with `date`, `from`, `to`, `subject`, `body`, and an `attachments` list. Each attachment entry includes `filename`, `mimeType`, `size`, `attachmentId`, and `messageId` — pass the last two to `gmail_get_attachment`.

**`gmail_get_attachment`** — Download an attachment from a Gmail message.

```
message_id     messageId from the attachment entry
attachment_id  attachmentId from the attachment entry
mime_type      mimeType from the attachment entry
filename       filename from the attachment entry
account        dax-creds credential name (defaults to DAX_CREDS_GMAIL)
```

Returns the attachment in a format Claude can read directly:

| Type | Returned as |
| --- | --- |
| `image/*` | Image content (Claude can view) |
| `text/*` | Plain text |
| `application/pdf`, Office docs | Embedded blob (Claude can read) |
| zip, binary | Metadata note only |

---

## Environment Management

A dax **environment** is a folder registered in `~/.dax.yaml`, named after the folder. Each one has its own Claude state tree and credential.

### `dax run`

Launch a container for the current folder, creating the env first with a short wizard if the folder isn't registered yet.

```bash
dax run                    # launch (or create, then launch)
dax run -f github          # add features on the fly
dax run -p 8080:80         # expose an extra port
dax -t run                 # preview: print the docker command instead of launching
```

`-t` changes nothing: in a folder that isn't registered it refuses rather than starting the wizard, and it never creates or seeds a state tree.

Run it from an env's own folder: from a subfolder of a registered env it refuses and tells you where the env's root is.

### `dax env list [--all]`

List registered envs, plus any Claude state tree no registered env claims:

```
       tenant    name         kind        dir                 creds                      state  used  archived
       --------  -----------  ----------  ------------------  -------------------------  -----  ----  --------
  [*]  acme      client-work  code        ~/work/client-work  (none)                     84K    now   never

  [ ]  personal  job-offer    commitment  ~/work/job-offer    (none)                     310K   2d    2d
  [ ]  personal  my-project   code        ~/work/my-project   github-me, ssh-id-ed25519  2.1M   3d    1w
  [!]  personal  old-thing    code        ~/work/old-thing    (none)                     -      -     5w
```

`kind` is `code` for a code project. Anything else is a substrate-backed process, shown by its type, which is read from the `type:` line at the top of the process's own `context.md` (written by `new-process.sh`), or `?` if that's missing.

Envs are grouped by tenant, with a blank line between tenants, and sorted by name within each. An orphaned state tree sits with its tenant. Tenants appear in the order the top-level `tenants:` block lists them, then any it doesn't list, alphabetically. To order a tenant that has no settings, list just its name:

```yaml
tenants:
  personal:
  ysecurity:
    claude:
      chrome_profile: Profile 5
  acme:
  parked:
    hidden: true
```

A tenant with `hidden: true` is left out of `dax env list`; a footer line counts what's hidden. `dax env list --all` shows it again, together with archive-only envs. Hiding only affects the listing: every other command, and every picker, still sees those envs.

`[*]` running, `[!]` folder missing, `[?]` a state tree with no env (orphaned), `[a]` an env that exists only as an archive (with `--all`).

`used` is how long ago the env's Claude state last changed. `archived` is how long ago its newest archive was written by `dax env archive` to `backup_dir/daily/`, or `never`. An archive saved to a file elsewhere isn't counted. If `backup_dir` isn't there (an unmounted drive, say), the column shows `?` and a note.

An env with no `~/.dax.yaml` entry and no state tree, only archives, is counted in the same footer line. `dax env list --all` lists each one under the tenant `(archive-only)`, after every other group. Its real tenant is recorded inside the archive, but dax doesn't open archives to find it: on OneDrive, opening one can download the whole file. To bring one back, `cd` into an empty folder with its name and run `dax env restore`.

### `dax env ports [--assign]`

Each env's webpreview port is stored on its entry, so it is the same every run and no two envs share one. New envs get a port from the wizard, and restored envs from restore. `dax env ports` lists every env's port. `dax env ports --assign` gives a port to every env that has none, or whose port is outside the range after you change `defaults.webpreview_base_port`; it shows the changes and asks before writing, keeps `~/.dax.yaml.prev`, and says which renumbered envs are running (they keep their old port until restarted). It runs on the host only.

`dax run` won't start an env without a stored port, or one whose port something else is using; it says which.

#### The env index page

dax keeps a page listing every env with a link to its webpreview at `~/.local/state/dax/envs.html`. Bookmark it once in your browser. `dax run`, `dax env list`, restore and `dax env ports --assign` rewrite it. Each time you open it, the page checks which envs are running.

### `dax env show [name]`

One env's settings plus what isn't in `~/.dax.yaml`: what kind of env it is (a code project, or a process with its type, skill and title from its `context.md`), its implied Claude credential and which browser its login opens, whether the folder exists, the container's state, and its state tree's size and age. Defaults to the env containing the current folder.

### `dax env archive [name ...]`

Snapshot envs (folder, Claude state tree, registry entry) for backup. A short wizard: which envs, then (for one env) dax's managed backups under `backup_dir/daily/` or a specific file, then for managed backups whether to remove the envs afterwards. Removal happens only once every archive has been read back successfully. Credentials are never archived. `dax env archive --explain` describes it in full.

### `dax env restore [name|path]`

Recover one archived env into the current folder, which must be empty and not a registered env. Restore never deletes or overwrites anything. The env is named after the folder, so restoring `fabric` from `~/fabric-check` registers `fabric-check` next to the live `fabric`. Pick dax's managed backups (env, then date, newest first) or a specific file; a name or path argument skips straight ahead. To roll a live env back: `dax env destroy` it, then restore into an empty folder. `dax env restore --explain` describes it in full.

### `dax env destroy [name ...]`

Permanently remove envs: folder, Claude state tree, derived credential, and registry entry. Refuses while an env's container is running, and asks you to type `DESTROY`. With no names, shows a picker.

### `dax backup [--explain]`

Copies your `dotfiles:` and `backup:` paths into `backup/` in this checkout (gitignored), writing only files that changed. `dax run` does the same quietly on every launch. Env archives are a separate command, `dax env archive`.

### `dax config`

Edit `~/.dax.yaml` safely (see [Configuration](#configuration)). Runs on the host only: inside a dax container the file is a single-file bind mount that some editors' saves silently detach.

**Changing an env's tenant.** The tenant names the env's Claude state tree (`~/.local/state/dax/tenants/<tenant>/<env>/`) and its Claude credential (`claude-<tenant>-<env>`). So when you change an env's `tenant:` and save, `dax config` offers to move both:
- **State tree:** moved to the new tenant's folder.
- **Credential:** renamed in Keychain. It's the same login, so there's nothing to redo, and no old secret is left behind.
- **Login record:** moved along with the credential.

The env's own folder doesn't move. The env's container has to be stopped first. If either new name already exists, nothing moves and dax prints the steps to do it by hand.

---

## Features Reference

`dax features` lists every feature with a short note on each.

Always on for every env: `workdir`, `dotfiles`, `webpreview`, `claude`.

| Feature | Description |
| --- | --- |
| `workdir` | Mounts the env's folder into the container, at a path named after the folder |
| `dotfiles` | Mounts dotfiles from `~/.dax.yaml` into the container (`ro` or `rw`) |
| `webpreview` | Starts `dax-preview`, a rendered file browser served on a stable port (8000–8999, derived from the env's folder) |
| `claude` | Mounts the env's own Claude state tree at `~/.claude`, seeds your global `CLAUDE.md`/settings into it once, and wires in its implied credential |
| `mounts` | Mounts each path in the env's `mounts:` list at `$HOME/<name>`. On whenever that list is non-empty |
| `substrate` | Opt in per env, for a *substrate-backed process*: an env scaffolded by the substrate's own `shared/new-process.sh` (which the `dax run` wizard runs for you) and worked with its skills. Mounts the top-level `substrate:` repo and sets `$SUBSTRATE_ROOT`. Claude refuses to start unless the substrate's `shared/wire.sh --check` passes |
| `auto_claude` | Opt in per env: the container starts straight into `claude` in tmux instead of a shell |
| `github` | Mounts `githubdir` (e.g. `~/.config/gh`) for gh CLI config |
| `aws` | Mounts `awsdir` (e.g. `~/.aws`; warns that credentials are exposed) |
| `ports` | Exposes additional ports; configure in `~/.dax.yaml` or pass `-p host:container` |
| `optdir` | Mounts `optdir`, a directory of additional tools |
| `X11` | Forwards X11 display to the host |
| `msf` | Mounts the configured `msfdir` and publishes port 4444, for Metasploit |
| `ovpn` | Adds the capabilities a VPN client needs (not fully implemented) |

SSH needs no feature: attaching an `ssh` credential forwards an agent holding that key.

### webpreview

`dax-preview` serves the working directory over HTTP with GitHub-style directory listings, rendered Markdown, and a collapsible file tree sidebar, at `http://localhost:<port>/`. The port is stored on the env's entry (see `dax env ports`):

```yaml
projects:
  myenv:
    dir: /Users/me/myenv
    tenant: personal
    webpreview:
      port: 8731
```

---

## Adding Features

Implement a function in `dax.py` named `feature_<name>` that takes the config dict and returns a list of `docker run` arguments:

```python
def feature_hosttmp(config):
    return ['--volume', '/tmp:/tmp/host']
```

The feature is then available by name in config files and on the command line:

```bash
dax run -f hosttmp
```
