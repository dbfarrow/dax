# dax

Dax wraps Docker to give you reproducible, isolated development environments that feel like a local shell. The current directory is always mounted as your work directory, dotfiles follow you in, and credentials are brokered securely from the host — no secrets ever baked into images.

The core idea: your environment should be cattle, not pets. Spin one up, do the work, tear it down. Everything that matters lives on the host.

---

## Contents

1. [Install & Build](#install--build)
2. [Quick Start](#quick-start)
3. [Configuration](#configuration)
4. [Credential Management](#credential-management)
5. [Environment Management](#environment-management)
6. [Features Reference](#features-reference)
7. [Adding Features](#adding-features)

---

## Install & Build

**Prerequisites:** Python 3.7+, Docker Desktop

```bash
git clone https://github.com/dbfarrow/dax.git
cd dax
pip install -e .
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

---

## Quick Start

Register the current directory as a dax project:

```bash
cd ~/work/my-project
dax init
```

`dax init` asks for an image, lets you select or define credentials, and writes the project to `~/.dax.yaml`. After that:

```bash
dax run
```

That's it. You're in a container with your project mounted, dotfiles present, and credentials available.

---

## Configuration

All configuration lives in `~/.dax.yaml`. A local `.dax.yaml` in the current directory can override or extend it.

### Minimal example

```yaml
image: dax:latest

features:
  - workdir
  - dotfiles
  - ssh

workdir:
  container: work

dotfiles:
  ro:
    - ~/.zshrc
    - ~/.vimrc
    - ~/.tmux.conf
  rw:
    - ~/.gitconfig
```

### Full example

```yaml
image: dax:latest

features:
  - workdir
  - dotfiles
  - ssh
  - claude
  - auggie
  - webpreview

workdir:
  container: work

dotfiles:
  ro:
    - ~/.zshrc
    - ~/.vimrc
    - ~/.vim/
    - ~/.tmux.conf
  rw:
    - ~/.dax.yaml
    - ~/.gitconfig
    - ~/.claude.json

claudedir:
  mount: ~/.claude

auggiedir:
  mount: ~/.augment

awsdir:
  mount: ~/.aws

backup:
  - ~/.claude/CLAUDE.md
  - ~/.claude/settings.json

credentials:
  ssh-id-rsa:
    provider: ssh
    key: ~/.ssh/id_rsa
  github-dfarrow:
    provider: github
    browser: firefox
  claude-work:
    provider: claude
    browser: default

projects:
  my-project:
    dir: /Users/dfarrow/work/my-project
    image: dax:latest
    creds:
      - ssh-id-rsa
      - github-dfarrow
      - claude-work
```

### Top-level keys

| Key | Description |
| --- | --- |
| `image` | Default Docker image to use |
| `features` | Default feature list for `dax run` |
| `dotfiles` | Files to mount into the container (`ro` / `rw`) |
| `workdir` | Container path for the mounted working directory |
| `credentials` | Named credential definitions (managed by `dax creds`) |
| `projects` | Registered environments (managed by `dax init`) |
| `backup` | Host paths to copy into `backup/` when running `dax backup` |

---

## Credential Management

Dax includes a credential brokering system so tools inside the container can authenticate without secrets ever being written into the image or passed on the command line.

**How it works:** When `dax run` starts, a credential daemon launches on the host. The container connects to it over a Unix socket and requests tokens by name. Each credential type has its own provider that handles storage and acquisition.

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

Authenticates using Claude's OAuth flow — the same credentials that Claude Code uses, stored in `~/.claude/credentials.json`. Dax does **not** use Anthropic API keys. API keys are static long-lived secrets; the OAuth flow issues short-lived access tokens backed by a refresh token, which is more secure and consistent with how Claude Code itself authenticates.

The `claude` wrapper inside the container fetches the credentials blob from the daemon and writes it to `~/.claude/credentials.json`. Claude Code then authenticates natively and handles token refresh on its own.

```yaml
credentials:
  claude-work:
    provider: claude
    browser: default
```

### Commands

#### `dax creds add`

Interactively define a new credential: name, provider, browser. Imports an existing token from disk into Keychain if one is found (e.g. from `~/.config/gh/hosts.yml` for GitHub, or `~/.claude/credentials.json` for Claude).

#### `dax creds list`

Show all credentials with status:

```
  name            provider  stored  detail        used by     warnings
  --------------  --------  ------  ------------  ----------  ----------------
  ssh-id-rsa      ssh       yes     ~/.ssh/id_rsa my-project
  github-dfarrow  github    yes     firefox       my-project
  claude-work     claude    yes     default       my-project  [!] key on disk
```

| Column | Meaning |
| --- | --- |
| stored | Whether the token is in macOS Keychain |
| detail | Key path (ssh) or browser config (github/claude) |
| used by | Projects that reference this credential |
| warnings | Plaintext copy of the token exists on disk (see below) |

#### `dax creds update [name]`

Update credential metadata (browser, Chrome profile, key path). Omit `name` to pick interactively. Shows current configuration before prompting.

#### `dax creds login <name>`

Run the first-party auth flow for a credential and store the result in Keychain:

- **ssh** — loads the key via `ssh-add --apple-use-keychain`
- **github** — opens the OAuth device flow in the configured browser; polls until authorized
- **claude** — opens the Claude OAuth flow in the configured browser

#### `dax creds remove <name>`

Remove a credential's token from Keychain. Does not delete the credential definition from `~/.dax.yaml`.

### Warnings

`dax creds list` flags credentials where a plaintext copy of the token exists on disk:

- **`[!] token on disk`** (github) — `~/.config/gh/hosts.yml` contains an `oauth_token`. Written by `gh auth login` and persists until explicitly removed. The token in Keychain is what dax uses; the one in `hosts.yml` is redundant and a minor security risk.
- **`[!] key on disk`** (claude) — `~/.anthropic/api_key` or `~/.claude/credentials.json` exists. The `credentials.json` file is written by Claude Code's own auth flow and is expected to be present on the host; the warning is a reminder that it contains live credentials.

---

## Environment Management

A dax **environment** (or project) is a directory registered in `~/.dax.yaml` with an image and a set of credentials.

#### `dax init`

Run from any directory to register it as a dax project. Prompts for image and credentials (checkbox — space to toggle, enter to confirm). Re-running `dax init` in an already-registered directory lets you update the image or change which credentials are attached.

#### `dax envs list`

List all registered environments:

```
  [ ]  my-project   dax:latest  /Users/dfarrow/work/my-project   ssh-id-rsa, github-dfarrow
  [*]  client-work  dax:latest  /Users/dfarrow/work/client-work  ssh-id-rsa, github-work
  [!]  old-thing    dax:latest  /Users/dfarrow/work/old-thing    (none)
```

| Status | Meaning |
| --- | --- |
| `[ ]` | Directory exists, container not running |
| `[*]` | Container currently running |
| `[!]` | Directory does not exist |

#### `dax run`

Launch a container for the current directory. Dax looks up the project in `~/.dax.yaml`, starts the credential daemon, and assembles the `docker run` command from the configured features and credentials.

```bash
dax run                    # use project config
dax run -f ssh,webpreview  # add features on the fly
dax run -p 8080:80         # expose an extra port
```

---

## Features Reference

| Feature | Description |
| --- | --- |
| `workdir` | Mounts the current host directory as the working directory inside the container |
| `dotfiles` | Mounts dotfiles from `~/.dax.yaml` into the container (`ro` or `rw`) |
| `ssh` | Forwards the SSH agent into the container; keys stay on the host |
| `claude` | Mounts `~/.claude` into the container for Claude Code config |
| `auggie` | Mounts `~/.augment` into the container for Augment Code |
| `github` | Mounts `~/.config/gh` into the container for gh CLI config |
| `aws` | Mounts `~/.aws` into the container (warns that credentials are exposed) |
| `webpreview` | Starts `dax-preview` — a rendered file browser served on a stable port (8000–8999, derived from the project directory) |
| `ports` | Exposes additional ports; configure in `~/.dax.yaml` or pass `-p host:container` |
| `optdir` | Mounts a directory of additional tools into the container |

### webpreview

`dax-preview` serves the working directory over HTTP with GitHub-style directory listings, rendered Markdown, and a collapsible file tree sidebar. The port is deterministic per project directory (same port every run) and can be overridden:

```yaml
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
