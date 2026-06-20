# dax-creds: Credential Brokering for dax Containers

Design notes for first-class credential management in dax.
Originated from a design session on 2026-06-18/19.

---

## The Problem

OAuth refresh tokens stored as files on disk are portable credentials — steal the
file, use it anywhere, no passphrase required. This is meaningfully worse than a
passphrase-protected SSH private key, where the file alone is useless. Any tool
running inside a dax container that needs OAuth access (Gmail, GitHub, Claude, etc.)
shouldn't have to store a refresh token on the container filesystem.

## The Pattern: ssh-agent Analog

dax already solves this for SSH: it forwards `SSH_AUTH_SOCK` into containers so
tools inside can use SSH without the private key ever entering the container. The
same pattern applies to OAuth tokens.

dax-creds extends this to arbitrary credentials.

---

## Architecture

### Per-instance daemon (macOS host)

Each `dax run` starts its own credential daemon subprocess on the macOS host.
The daemon is a child of the dax launch script and dies when the container exits.

Responsibilities:
- Performs a single Touch ID check at startup via the macOS LocalAuthentication
  framework — no per-request biometrics, but no silent background access either
- After Touch ID succeeds, fetches declared refresh tokens from macOS Keychain
  into process memory
- Listens on a unique Unix socket: `~/.dax/creds-<workdir-hash>.sock`
- On token requests: exchanges the in-memory refresh token for a fresh access
  token, caches it for the token's TTL (~1hr), returns it to the caller
- Refresh tokens never cross the socket boundary — callers only receive
  short-lived access tokens
- Only serves credentials declared in the project's config — requests for
  undeclared services are rejected

No shared daemon, no launchd, no cross-container state.

### dax container launch changes

When starting a container, dax starts the daemon subprocess first, then mounts
its socket:

```python
daemon = start_creds_daemon(project_creds, socket_path)
docker_args += ['-v', f'{socket_path}:/run/dax-creds.sock']
docker_args += ['-e', 'DAX_CREDS_SOCK=/run/dax-creds.sock']
```

If no credentials are declared, no daemon is started and no socket is mounted.

### Socket protocol

Simple JSON over a Unix socket. Request/response, one exchange per connection.

Request:
```json
{"credential": "gmail-readonly"}
```

Response (success):
```json
{"token": "ya29.xxx", "expires_at": "2026-06-18T14:30:00Z"}
```

Response (error):
```json
{"error": "not_configured", "message": "No credential named gmail-readonly"}
```

### Client library (container-side)

A small Python module included in dax container images:

```python
from dax.creds import get_token

token = get_token('gmail-readonly')
# Returns a valid access token string, or raises CredentialNotAvailable
```

Tools check for `DAX_CREDS_SOCK` and fall back to file-based auth if not present,
so they work in both dax and non-dax environments.

---

## Bridging to Existing Clients

Tools like `gh` and `claude` won't call `get_token()` — they have their own auth
mechanisms. The bridge is **wrapper scripts** shipped in the dax container image.
Each wrapper fetches a fresh token from the daemon immediately before exec'ing the
real binary:

```bash
#!/bin/bash
# /usr/local/bin/gh (replaces real gh in the container image)
if [ -n "$DAX_CREDS_SOCK" ]; then
    export GH_TOKEN=$(dax-creds get github)
fi
exec /usr/local/bin/gh-real "$@"
```

```bash
#!/bin/bash
# /usr/local/bin/claude
if [ -n "$DAX_CREDS_SOCK" ]; then
    export ANTHROPIC_API_KEY=$(dax-creds get claude)
fi
exec /usr/local/bin/claude-real "$@"
```

The token is fetched per-invocation, so there is no stale token problem regardless
of how long the shell has been open. The daemon's in-memory cache means this is
fast — no network call when the access token is still valid; a silent token
exchange when it has expired.

This also cascades correctly to git: `gh auth setup-git` configures git to call
`gh auth token` as a credential helper, which invokes the `gh` wrapper, which
fetches a fresh token from the daemon. Git HTTPS operations stay fresh through
the same chain.

Tokens are never placed in the environment at shell startup and never appear in
`env` output or `/proc/self/environ`. An adversary in the container sees `gh` and
`claude` as ordinary binaries. Discovering that they are wrappers calling a Unix
socket, locating that socket, understanding the protocol, and constructing a valid
request is a meaningfully longer chain than reading an env var or a credential file.

For Gmail, the custom tool already uses `DAX_CREDS_SOCK` directly via the client
library and does not need a wrapper.

### Known limitation: long-running processes

A process that reads a credential env var at startup and holds it for hours (e.g.,
a server) cannot benefit from the wrapper approach. Such a process would need to
use the `dax.creds` client library directly to fetch fresh tokens on demand, or be
restarted when its token expires. This does not apply to any current use case
(all current consumers are short-lived CLI invocations).

---

## Configuration

Everything lives in `~/.dax.yaml`. No local `.dax.yaml` files — all
project config is in one global location.

```yaml
defaults:
  image: dax-base

credentials:
  claude-work:
    provider: claude
  claude-personal:
    provider: claude
  github-dfarrow:
    provider: github
  github-someorg:
    provider: github
  ssh-work:
    provider: ssh
  ssh-personal:
    provider: ssh
  gmail-readonly:
    provider: gmail
    scope: readonly

projects:
  work-tool:
    dir: ~/work/work-tool
    creds: [claude-work, github-dfarrow, ssh-work]
    volumes: [~/.aws]

  personal-gmail:
    dir: ~/work/gmail-tool
    creds: [claude-personal, gmail-readonly]
```

`credentials` is a global registry of named credentials. Projects reference
credentials by name. Multiple accounts for the same service are just multiple
named entries. Keychain items are keyed by credential name (`dax-creds/<name>`),
so they coexist without collision.

`dax run` (no args) looks up the cwd in the projects list. If no match is
found, it fails with a clear message directing the user to run `dax init`.

---

## Credential Providers

Each provider implements three operations: `check` (is this in Keychain?),
`import_from_disk` (is there an existing working token on disk?), and `acquire`
(run the full auth flow from scratch). `dax init` calls them in that order,
stopping at the first success.

### `claude`
- **Import from disk**: reads existing session/API key from Claude Code's local
  credential store
- **Acquire**: Google OAuth 2.0 browser redirect via a specific Chrome profile
  (see Chrome profile routing below); stores refresh token in Keychain
- **Bridge**: container image ships a `claude` wrapper script

**Caveat**: Claude Code is a long-running process, not a short-lived CLI invocation.
The wrapper sets `ANTHROPIC_API_KEY` once at startup. If Claude Code uses `claude
login` session management with its own built-in refresh logic, this is fine — the
env var bootstraps the session and Claude Code handles renewal internally. If it
relies on `ANTHROPIC_API_KEY` as a static value, the token will go stale after
~1hr. This needs to be verified before shipping the `claude` wrapper.

If session management does not handle renewal, the fallback is to have Claude Code
call `dax-creds get claude` on demand via a credential helper or plugin, rather
than receiving a one-shot value at startup. That is a more invasive change and
should only be pursued if the simpler wrapper approach proves insufficient.

### `github`
- **Import from disk**: reads `oauth_token` from `~/.config/gh/hosts.yml`
- **Acquire**: device flow — prints URL and code to terminal; user approves in
  host browser; stores token in Keychain
- **Bridge**: container image ships a `gh` wrapper script; cascades to git via
  `gh auth setup-git`

### `gmail`
- **Import from disk**: reads refresh token from `token-readonly.json` (or
  configured path) in the project directory
- **Acquire**: Google OAuth 2.0 browser redirect via a specific Chrome profile
  (see Chrome profile routing below); `scope` field controls access level;
  stores refresh token in Keychain
- **Bridge**: tool uses `DAX_CREDS_SOCK` directly via client library; no wrapper needed

### `ssh`
- **Import from disk**: imports existing private key file via
  `ssh-add --apple-use-keychain <key>`; prompts for passphrase once, stores in
  macOS Keychain; subsequent loads are silent (Touch ID if Keychain is locked)
- **Acquire**: same as import — there is no separate "generate" flow unless
  explicitly requested; the key file is specified by the `key` field in the
  credential definition
- **Bridge**: SSH agent forwarding (existing dax feature); dax-creds daemon not
  involved at runtime — all keys loaded into the agent are forwarded via the
  existing `feature_ssh` socket mount
- **Multiple keys**: multiple `ssh` credentials with different `key` paths are
  fully supported; `dax init` runs the ceremony for each; all loaded keys are
  available in the container simultaneously via the shared agent socket

SSH credentials require a `key` field identifying the key file:

```yaml
credentials:
  ssh-github:
    provider: ssh
    key: ~/.ssh/id_ed25519_github
  ssh-work:
    provider: ssh
    key: ~/.ssh/id_ed25519_work
```

**macOS SSH config** (one-time, outside dax): add to `~/.ssh/config`:
```
Host *
  UseKeychain yes
  AddKeysToAgent yes
```
This ensures keys load from Keychain automatically on agent start.

After a successful import from disk, `dax init` offers to delete or zero the
source file. This is the primary migration path for existing projects.

### Chrome profile routing

Google-backed credentials (`claude`, `gmail`) need to open a browser for OAuth
consent. Since different credentials belong to different Google accounts, each
credential can be tied to a specific Chrome profile that is already signed into
the right account.

Chrome supports profile-targeted launch on macOS:
```bash
open -na "Google Chrome" --args --profile-directory="Profile 1" "https://..."
```

Profile directory names (`Default`, `Profile 1`, `Profile 2`, …) are mapped to
human-readable names and signed-in emails in:
```
~/Library/Application Support/Google/Chrome/Local State
```

During `dax init`, when defining a Google-backed credential, dax reads this file
and presents a picker:
```
Which Chrome profile should handle this OAuth flow?
  1) Personal  (dave@gmail.com)        [Default]
  2) Customer A  (dave@customera.com)  [Profile 1]
  3) Customer B  (dave@customerb.com)  [Profile 2]
```

The selected profile directory name is stored in the credential definition:
```yaml
credentials:
  claude-customer-a:
    provider: claude
    chrome_profile: Profile 1
  gmail-personal:
    provider: gmail
    scope: readonly
    chrome_profile: Default
```

When running the OAuth flow, dax opens Chrome with the specified profile rather
than the system default browser. The OAuth localhost callback is unaffected —
it lands on a local HTTP server regardless of which profile handles the flow.

**Host packages required** for Google OAuth flows:
- `requests` — HTTP token exchange
- `google-auth-oauthlib` — Google OAuth 2.0 with localhost redirect
- `keyring` — Keychain storage
- `pyobjc-framework-LocalAuthentication` — Touch ID at daemon startup

These are host-side only (macOS). Container images do not need them.

### AWS (not a managed credential)

AWS SSO credentials are already short-lived and managed by AWS tooling. The
right approach is a volume mount:

```yaml
projects:
  my-aws-project:
    volumes: [~/.aws]
```

The AWS CLI runs inside the container. `aws sso login` prints a URL when it
cannot open a browser; the user opens it in the host browser; the SSO token
cache is written to `~/.aws/sso/cache/` which persists on the host via the
mount. All containers sharing the `~/.aws` mount share the cache.

`~/.aws/config` with SSO profile definitions must exist on the host beforehand
(one-time manual setup).

---

## `dax init`

Registers the current directory as a dax project. Can be invoked explicitly
(`dax init`) or triggered automatically by `dax run` when the current directory
is not yet registered.

Interactive prompts:

1. **Image** — which dax base image to use (default from `defaults.image`)
2. **Credentials** — for each credential to attach:
   - First offers existing named credentials from the registry (pick from list)
   - Option to define a new credential (prompts for name and provider)
   - For new credentials, in order: check if already set up → import from disk →
     fresh OAuth/keygen flow. Stops at first success.
   - For SSH: checks if the key is already loaded in the agent (`ssh-add -l`);
     if not, runs `ssh-add --apple-use-keychain <key>` and prompts for passphrase
   - For OAuth credentials: on successful import from disk, offers to delete the
     source file
   - Repeats until user is done adding credentials
3. **Volumes** — any additional mounts (e.g., `~/.aws`)
4. Appends the project entry and any new credential definitions to `~/.dax.yaml`

**Credential registry is cumulative.** Once `ssh-github` is defined and set up,
every future `dax init` just offers it as a selection — no ceremony. The full
setup flow (key file path, passphrase prompt, Keychain storage) only runs when
a credential is defined for the first time.

---

## `dax run` Flow

1. Look up cwd in `~/.dax.yaml` projects — if not registered, trigger `dax init`
   inline before proceeding
2. For each declared credential: check Keychain; if missing, run provider's
   acquire flow (same as `dax init` would do)
3. Start per-instance daemon subprocess; daemon prompts Touch ID once
4. `docker run` with daemon socket mounted at `/run/dax-creds.sock`
5. On container exit: daemon subprocess exits, in-memory tokens gone

---

## Security Properties

| | File-based token (today) | dax-creds |
|---|---|---|
| Token at rest | Plaintext on disk | macOS Keychain |
| Token in container | Yes (refresh token) | Access token only, fetched per-invocation by wrapper scripts |
| Token portability | Steal file → use anywhere | Refresh token never leaves host |
| Stolen token impact | Full access until revoked | ~1hr access token, then expired |
| Biometric | No | Touch ID at each session launch |
| Cross-container leakage | N/A | Impossible (per-instance daemon, scoped) |
| Token lifetime | Until revoked | Dies with container |
| Exfiltration via filesystem | Yes | No |

### What the biometric model does and doesn't cover

Touch ID fires once when the daemon starts, proving presence at session launch.
This does not protect against: an attacker who compromises a long-running
container after launch, waits for the access token to expire, and benefits from
the daemon silently refreshing it. The practical window for this attack is bounded
by session duration. The primary threat dax-creds addresses — refresh tokens
stolen from disk and used elsewhere — is fully covered.

---

## Implementation Notes

- Daemon: Python asyncio Unix socket server, started as a subprocess by dax
- Touch ID: `pyobjc-framework-LocalAuthentication` — one `LAContext.evaluatePolicy`
  call at startup; if it fails, daemon exits and `docker run` is aborted
- Keychain access: `keyring` library (standard macOS backend) for storing and
  retrieving refresh tokens; items are accessible to the daemon process without
  per-access Touch ID prompts (Touch ID is handled at the LA layer instead)
- Socket path: `~/.dax/creds-<sha256(workdir)[:12]>.sock` — unique per project,
  stable across runs, cleaned up on daemon exit
- Token caching: in-memory dict keyed by credential name, evicted on expiry
- Wrapper scripts: thin shell scripts in the container image that call
  `dax-creds get <name>` and exec the real binary; `dax-creds get` is a
  container-side CLI that makes a single socket request and prints the token
- No exotic dependencies beyond `keyring` and `pyobjc-framework-LocalAuthentication`
- dax's design goal (nothing installed on host except Python and Docker) is
  preserved — daemon runs in the host Python environment alongside dax itself

---

## Generalization

dax-creds is not service-specific. Any tool needing OAuth access defines a
credential name and provider in `~/.dax.yaml`. Current candidates:

- `gmail-readonly` — email-to-inbox tool
- `gmail-modify` — unsubscribe enforcement (future)
- `github-<account>` — any tool needing GitHub API access
- `claude-<account>` — Claude API / Claude Code auth
- `ssh-<name>` — SSH key per identity/context
