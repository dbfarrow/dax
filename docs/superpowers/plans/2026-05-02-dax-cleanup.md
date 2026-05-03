# DAX Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor dax from two separate scripts into a single `dax` CLI with subcommands, externalize the Dockerfile as a template, migrate config to YAML, replace SSH key mounting with agent forwarding, add runtime dotfile mounting with ro/rw support, and add Claude CLI to the image.

**Architecture:** A single `dax.py` entry point uses argparse subcommands (`build`, `run`, `features`) to replace `daxbuild.py` and `daxrun.py`. The Dockerfile is kept as `Dockerfile.tmpl` (generated `Dockerfile` is gitignored). Config is YAML throughout. Features are pure functions returning lists of docker args — the pattern stays the same but is cleaned up.

**Tech Stack:** Python 3.7+, argparse, pyyaml, subprocess, Docker Desktop for Mac

---

## File Map

**Create:**
- `dax.py` — single CLI entry point with `build`, `run`, `features` subcommands
- `Dockerfile.tmpl` — Dockerfile template with `%%USER%%`, `%%SHELL%%`, `%%PASSWD%%` tokens
- `tests/test_config.py` — unit tests for config loading and merging
- `tests/test_features.py` — unit tests for feature arg generation
- `tests/test_build.py` — unit tests for Dockerfile template rendering

**Modify:**
- `dax-defaults.yaml` — update to be the canonical defaults file, add `dotfiles` section
- `.gitignore` — add `Dockerfile` (the generated artifact)
- `util.py` — no changes needed

**Delete** (after `dax.py` is complete and tested):
- `daxbuild.py`
- `daxrun.py`
- `dax-defaults.json`
- `dax.json`

---

## Task 1: Project scaffolding

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`
- Modify: `.gitignore`

- [ ] **Step 1: Add Dockerfile to .gitignore**

`.gitignore` currently has no content (or may not exist). Append:

```
Dockerfile
```

Run:
```bash
echo "Dockerfile" >> /home/developer/repos/dax/.gitignore
cat /home/developer/repos/dax/.gitignore
```

Expected: `Dockerfile` appears in the file.

- [ ] **Step 2: Create the tests package**

```bash
mkdir -p /home/developer/repos/dax/tests
touch /home/developer/repos/dax/tests/__init__.py
```

- [ ] **Step 3: Write a placeholder test to verify pytest works**

Create `tests/test_config.py`:

```python
def test_placeholder():
    assert True
```

- [ ] **Step 4: Run tests**

```bash
cd /home/developer/repos/dax && python -m pytest tests/ -v
```

Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
cd /home/developer/repos/dax
git add .gitignore tests/
git commit -m "chore: add test scaffolding and gitignore Dockerfile"
```

---

## Task 2: Externalize the Dockerfile as a template

**Files:**
- Create: `Dockerfile.tmpl`

The Dockerfile is currently generated inline in `daxbuild.py:build_dockerfile()`. We extract it to a template file using `%%TOKEN%%` placeholders to avoid any conflict with Dockerfile's own `$VAR` shell syntax or Python f-string `{var}` syntax.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_build.py`:

```python
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import render_dockerfile

def test_render_dockerfile_substitutes_user():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'testuser' in content
    assert '%%USER%%' not in content

def test_render_dockerfile_substitutes_shell():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert '/bin/zsh' in content
    assert '%%SHELL%%' not in content

def test_render_dockerfile_substitutes_passwd():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'testuser:testpass' in content
    assert '%%PASSWD%%' not in content

def test_render_dockerfile_contains_key_tools():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'nmap' in content
    assert 'tmux' in content
    assert 'claude' in content.lower() or 'claude-code' in content
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/developer/repos/dax && python -m pytest tests/test_build.py -v
```

Expected: FAIL with `ImportError: cannot import name 'render_dockerfile' from 'dax'` (dax.py doesn't exist yet)

- [ ] **Step 3: Create `Dockerfile.tmpl`**

Extract the heredoc from `daxbuild.py:build_dockerfile()` verbatim, replacing `{user}` → `%%USER%%`, `{shell}` → `%%SHELL%%`, `{passwd}` → `%%PASSWD%%`, and add the Claude CLI install block before `USER %%USER%%`.

Create `Dockerfile.tmpl`:

```dockerfile
#
# Inspired by justinsteven's jax... a replacement for my kali VM
#

FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
	apt-utils \
	apt-transport-https \
	build-essential \
	ca-certificates \
	curl \
	default-jdk \
	openjdk-8-jre \
	dnsutils \
	ftp \
	gdb \
	git \
	golang \
	inetutils-ping \
	iproute2 \
    jq \
	locales \
	lsb-release \
	netcat \
	net-tools \
	nmap \
	man \
	pass \
	procps \
	python3-pip \
	python3-virtualenv \
	rbenv \
	ruby-full \
	socat \
	sudo \
	tcpdump \
	tmux \
	unzip \
	vim \
	virtualenv \
	wget \
	zsh

# install AWS CLI v2
RUN apt-get install -y libssl-dev libffi-dev \
    && cd /tmp \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install

# install Node.js and Claude CLI
RUN curl -fsSL https://deb.nodesource.com/setup_lts.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g @anthropic-ai/claude-code

# setup user
RUN echo 'en_US.UTF-8 UTF-8' >> /etc/locale.gen \
    && locale-gen \
	&& useradd -m -s %%SHELL%% %%USER%% \
	&& usermod -a -G sudo %%USER%% \
	&& echo %%USER%%:%%PASSWD%% | chpasswd \
	&& echo "done setting up user"

USER %%USER%%

# install rust
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs >> /tmp/sh.rustup.rs \
    && sh /tmp/sh.rustup.rs -y

WORKDIR /home/%%USER%%
CMD ["/bin/zsh"]
```

- [ ] **Step 4: Create `dax.py` with just `render_dockerfile`**

Create `dax.py`:

```python
#!/usr/bin/env python3

import os
import sys
from pathlib import Path


def _template_path():
    return Path(__file__).parent / 'Dockerfile.tmpl'


def render_dockerfile(user, shell, passwd):
    content = _template_path().read_text()
    content = content.replace('%%USER%%', user)
    content = content.replace('%%SHELL%%', shell)
    content = content.replace('%%PASSWD%%', passwd)
    return content
```

- [ ] **Step 5: Run tests**

```bash
cd /home/developer/repos/dax && python -m pytest tests/test_build.py -v
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
cd /home/developer/repos/dax
git add Dockerfile.tmpl dax.py tests/test_build.py
git commit -m "feat: externalize Dockerfile as Dockerfile.tmpl with render_dockerfile()"
```

---

## Task 3: Config loading with YAML

**Files:**
- Modify: `dax-defaults.yaml`
- Modify: `dax.py`
- Modify: `tests/test_config.py`

The existing `load_config()` in `daxrun.py` reads `~/.dax.json`. We rewrite it to read `~/.dax.yaml` and merge with an optional per-directory `.dax.yaml`.

- [ ] **Step 1: Update `dax-defaults.yaml` to be the canonical defaults**

Replace the contents of `dax-defaults.yaml` with:

```yaml
image: dfarrow/dax:latest

features:
  - workdir
  - optdir

workdir:
  container: /home/dfarrow/work

optdir:
  host: /Users/dfarrow/opt/dax-opt
  container: /home/dfarrow/opt

awsdir:
  host: /Users/dfarrow/.aws
  container: /home/dfarrow/.aws

gpgdir:
  host: /Users/dfarrow/.gnupg
  container: /home/dfarrow/.gnupg

msfdir:
  host: /Users/dfarrow/.msf4
  container: /home/dfarrow/.msf4

dotfiles:
  ro:
    - ~/.zshrc
    - ~/.vimrc
    - ~/.gitconfig
    - ~/.tmux.conf
  rw:
    - ~/.zsh_history
```

Note: `sshdir` is intentionally removed — SSH is handled via agent forwarding, not volume mounts.

- [ ] **Step 2: Write the failing tests**

Replace `tests/test_config.py` with:

```python
import os
import sys
import tempfile
import yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import load_config


def _write_yaml(path, data):
    with open(path, 'w') as f:
        yaml.dump(data, f)


def test_load_config_reads_home_defaults(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'work'
    workdir.mkdir()

    defaults = {
        'image': 'test/dax:latest',
        'features': ['workdir'],
        'workdir': {'container': '/home/test/work'},
    }
    _write_yaml(home / '.dax.yaml', defaults)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert config['image'] == 'test/dax:latest'
    assert 'workdir' in config['features']


def test_load_config_merges_local_config(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'project'
    workdir.mkdir()

    defaults = {
        'image': 'test/dax:latest',
        'features': ['workdir'],
        'workdir': {'container': '/home/test/work'},
    }
    local = {
        'features': ['aws'],
        'awsdir': {'host': '/Users/test/.aws', 'container': '/home/test/.aws'},
    }
    _write_yaml(home / '.dax.yaml', defaults)
    _write_yaml(workdir / '.dax.yaml', local)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert 'workdir' in config['features']
    assert 'aws' in config['features']


def test_load_config_sets_envname(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    workdir = tmp_path / 'home' / 'foo' / 'bar'
    workdir.mkdir(parents=True)

    _write_yaml(home / '.dax.yaml', {'image': 'x', 'features': []})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(workdir)

    config = load_config()

    assert config['envname'] == 'foo-bar'


def test_load_config_exits_if_outside_home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    outside = tmp_path / 'other'
    outside.mkdir()

    _write_yaml(home / '.dax.yaml', {'image': 'x', 'features': []})
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(outside)

    try:
        load_config()
        assert False, "should have exited"
    except SystemExit:
        pass
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /home/developer/repos/dax && python -m pytest tests/test_config.py -v
```

Expected: FAIL with `ImportError: cannot import name 'load_config' from 'dax'`

- [ ] **Step 4: Add `load_config` to `dax.py`**

Add the following to `dax.py` (after the existing imports and `render_dockerfile`):

```python
import yaml
from util import dax_print


def load_config():
    home = os.environ['HOME']
    cwd = os.getcwd()

    if not cwd.startswith(home):
        dax_print("[!] dax must be run from somewhere under your home dir")
        sys.exit(-1)

    with open(os.path.join(home, '.dax.yaml'), 'r') as f:
        defaults = yaml.safe_load(f)
    defaults['envname'] = 'DAX'

    defaults['cwd'] = cwd

    dax_print("[+] looking for config file in {}".format(cwd))
    local_cfg_path = os.path.join(cwd, '.dax.yaml')
    local = {}
    if os.path.isfile(local_cfg_path):
        dax_print("[-]   config file is in {}".format(local_cfg_path))
        with open(local_cfg_path, 'r') as f:
            local = yaml.safe_load(f) or {}

    defaults['cfgdir'] = cwd
    defaults['envname'] = cwd.replace(home, '').lstrip('/').replace('/', '-')

    local_features = local.pop('features', [])
    defaults.update(local)
    defaults['features'].extend(local_features)

    return defaults
```

- [ ] **Step 5: Run tests**

```bash
cd /home/developer/repos/dax && python -m pytest tests/test_config.py -v
```

Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
cd /home/developer/repos/dax
git add dax.py dax-defaults.yaml tests/test_config.py
git commit -m "feat: add load_config() with YAML support and test coverage"
```

---

## Task 4: Feature functions and tests

**Files:**
- Create: `tests/test_features.py`
- Modify: `dax.py`

Port all `feature_*` functions from `daxrun.py` to `dax.py`, then add tests for the ones with testable pure logic.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_features.py`:

```python
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import (
    feature_workdir,
    feature_optdir,
    feature_aws,
    feature_ssh,
    feature_dotfiles,
    feature_ports,
    feature_msf,
    feature_ovpn,
    feature_X11,
)


def test_feature_workdir_mounts_cwd():
    config = {
        'cwd': '/Users/dfarrow/work/project',
        'workdir': {'container': '/home/dfarrow/work'},
    }
    opts = feature_workdir(config)
    assert '--volume=/Users/dfarrow/work/project:/home/dfarrow/work' in opts


def test_feature_optdir_mounts_volume():
    config = {
        'optdir': {
            'host': '/Users/dfarrow/opt',
            'container': '/home/dfarrow/opt',
        }
    }
    opts = feature_optdir(config)
    assert '--volume=/Users/dfarrow/opt:/home/dfarrow/opt' in opts


def test_feature_aws_mounts_volume():
    config = {
        'awsdir': {
            'host': '/Users/dfarrow/.aws',
            'container': '/home/dfarrow/.aws',
        }
    }
    opts = feature_aws(config)
    assert '--volume=/Users/dfarrow/.aws:/home/dfarrow/.aws' in opts


def test_feature_dotfiles_ro(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {
        'dotfiles': {
            'ro': ['~/.zshrc', '~/.vimrc'],
            'rw': [],
        },
        '_container_home': '/home/dfarrow',
    }
    opts = feature_dotfiles(config)
    assert '--volume=/Users/dfarrow/.zshrc:/home/dfarrow/.zshrc:ro' in opts
    assert '--volume=/Users/dfarrow/.vimrc:/home/dfarrow/.vimrc:ro' in opts


def test_feature_dotfiles_rw(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {
        'dotfiles': {
            'ro': [],
            'rw': ['~/.zsh_history'],
        },
        '_container_home': '/home/dfarrow',
    }
    opts = feature_dotfiles(config)
    assert '--volume=/Users/dfarrow/.zsh_history:/home/dfarrow/.zsh_history' in opts
    # must NOT have :ro suffix
    assert '--volume=/Users/dfarrow/.zsh_history:/home/dfarrow/.zsh_history:ro' not in opts


def test_feature_ports_returns_port_flags():
    config = {'ports': ['8080:80', '8443:443']}
    opts = feature_ports(config)
    assert '-p' in opts
    assert '8080:80' in opts
    assert '8443:443' in opts


def test_feature_ssh_returns_agent_forwarding(tmp_path, monkeypatch):
    sock = tmp_path / 'ssh-auth.sock'
    sock.touch()
    monkeypatch.setattr('dax._SSH_AGENT_SOCK', str(sock))
    opts = feature_ssh({})
    assert any('ssh-agent' in o for o in opts)
    assert '-e' in opts
    assert 'SSH_AUTH_SOCK=/ssh-agent' in opts


def test_feature_ssh_warns_if_no_socket(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('dax._SSH_AGENT_SOCK', str(tmp_path / 'missing.sock'))
    opts = feature_ssh({})
    assert opts == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /home/developer/repos/dax && python -m pytest tests/test_features.py -v
```

Expected: FAIL with ImportError

- [ ] **Step 3: Add feature functions to `dax.py`**

Add the following to `dax.py`:

```python
import socket as _socket

# Docker Desktop on Mac exposes the SSH agent via this fixed path
_SSH_AGENT_SOCK = '/run/host-services/ssh-auth.sock'


def _get_username():
    return os.environ.get('USER') or os.environ.get('LOGNAME')


def _container_home(config):
    return config.get('_container_home', '/home/{}'.format(_get_username()))


def _add_volume(config, feature_key):
    return ['--volume={}:{}'.format(config[feature_key]['host'], config[feature_key]['container'])]


def feature_workdir(config):
    cwd = config['cwd']
    if not cwd.startswith(os.environ['HOME']):
        dax_print("[+] DAX is running outside home... mounting /tmp/dax as working dir")
        cwd = '/tmp/dax'
        os.makedirs(cwd, exist_ok=True)
    return ['--volume={}:{}'.format(cwd, config['workdir']['container'])]


def feature_optdir(config):
    return _add_volume(config, 'optdir')


def feature_aws(config):
    dax_print("[!]   WARNING: your AWS access tokens are available inside the dax container.")
    return _add_volume(config, 'awsdir')


def feature_msf(config):
    opts = _add_volume(config, 'msfdir')
    opts += ['-p', '4444:4444']
    return opts


def feature_ovpn(config):
    dax_print("[!]   WARNING: ovpn feature not fully implemented.")
    return [
        '--cap-add=NET_ADMIN',
        '--device=/dev/net/tun',
        '--sysctl net.ipv6.conf.all.disable_ipv6=0',
    ]


def feature_ssh(config):
    if not os.path.exists(_SSH_AGENT_SOCK):
        dax_print("[!] SSH agent socket not found at {}. Run ssh-add first.".format(_SSH_AGENT_SOCK))
        return []
    return [
        '--mount', 'type=bind,src={},target=/ssh-agent'.format(_SSH_AGENT_SOCK),
        '-e', 'SSH_AUTH_SOCK=/ssh-agent',
    ]


def feature_dotfiles(config):
    opts = []
    container_home = _container_home(config)
    for f in config.get('dotfiles', {}).get('ro', []):
        host_path = os.path.expanduser(f)
        dest = os.path.join(container_home, os.path.basename(f))
        opts.append('--volume={}:{}:ro'.format(host_path, dest))
    for f in config.get('dotfiles', {}).get('rw', []):
        host_path = os.path.expanduser(f)
        dest = os.path.join(container_home, os.path.basename(f))
        opts.append('--volume={}:{}'.format(host_path, dest))
    return opts


def feature_X11(config):
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return [
        '-e', 'DISPLAY={}:0'.format(ip),
        '--volume', '/tmp/.X11-unix:/tmp/.X11-unix',
    ]


def feature_ports(config):
    opts = []
    ports = config.get('ports', [])
    if not ports:
        dax_print("[!] no ports defined in config")
        return opts
    for port in ports:
        opts += ['-p', port]
    return opts


def _add_feature(feature, config):
    fn_name = 'feature_{}'.format(feature)
    fn = globals().get(fn_name)
    if fn is None:
        dax_print("[!] unknown feature: {}".format(feature))
        _print_features()
        sys.exit(-1)
    dax_print("[+] adding {}".format(feature))
    return fn(config)


def _print_features():
    dax_print("[!] available features:")
    for name in sorted(globals()):
        if name.startswith('feature_'):
            dax_print("\t{}".format(name[len('feature_'):]))
```

- [ ] **Step 4: Run tests**

```bash
cd /home/developer/repos/dax && python -m pytest tests/test_features.py -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/developer/repos/dax
git add dax.py tests/test_features.py
git commit -m "feat: add feature functions with SSH agent forwarding and dotfiles ro/rw support"
```

---

## Task 5: `dax build` subcommand

**Files:**
- Modify: `dax.py`

Port build logic from `daxbuild.py` into a `cmd_build()` function callable from the CLI.

- [ ] **Step 1: Add build helpers and `cmd_build` to `dax.py`**

Add the following to `dax.py`:

```python
def _get_version():
    with open(Path(__file__).parent / 'VERSION', 'r') as f:
        return f.readline().split()[0]


def _get_dax_passwd():
    pwfile = Path(__file__).parent / '.daxpw'
    try:
        mode = os.stat(pwfile).st_mode
        if oct(mode)[-4:] == '0600':
            dax_print("[-]   reading password from .daxpw")
            return pwfile.read_text().split()[0]
        else:
            dax_print("[!] .daxpw must have permissions 0600")
            sys.exit(-1)
    except FileNotFoundError:
        dax_print("[-]   if you tire of typing a password, put it in .daxpw with chmod 0600")
        return input("Enter a password for the dax container: ")


def _get_user_build_args():
    username = _get_username()
    return [
        '--build-arg', 'user={}'.format(username),
        '--build-arg', 'user_id={}'.format(os.geteuid()),
        '--build-arg', 'user_gid={}'.format(os.getgid()),
    ]


def _runcmd(cmd, test_only=False):
    dax_print("[-]   " + ' '.join(cmd))
    if not test_only:
        subprocess.run(cmd)


def cmd_build(args):
    dax_print("[+] building Dockerfile from template")
    user = _get_username()
    shell = os.environ.get('SHELL', '/bin/zsh')
    passwd = _get_dax_passwd()

    dockerfile = render_dockerfile(user, shell, passwd)
    with open('Dockerfile', 'w') as f:
        f.write(dockerfile)

    version = _get_version()
    image_tag = 'dfarrow/dax:{}'.format(version)
    latest_tag = 'dfarrow/dax:latest'

    dax_print("[+] building container")
    build_cmd = ['docker', 'build'] + _get_user_build_args() + ['--platform=linux/amd64']
    if args.clean:
        build_cmd.append('--no-cache')
    build_cmd += ['-t', image_tag, '.']
    _runcmd(build_cmd, args.test_only)

    dax_print("[+] tagging container")
    _runcmd(['docker', 'rmi', latest_tag], args.test_only)
    _runcmd(['docker', 'tag', image_tag, latest_tag], args.test_only)

    _runcmd(['/bin/rm', '-f', './Dockerfile'], args.test_only)
    dax_print("[+] Commence to take over the world...")
```

- [ ] **Step 2: Verify manually with `-t` (test-only) flag (no docker required)**

```bash
cd /home/developer/repos/dax
python dax.py build -t
```

Expected: prints the docker build command without running it, then removes Dockerfile

- [ ] **Step 3: Confirm `Dockerfile` was cleaned up**

```bash
ls /home/developer/repos/dax/Dockerfile 2>/dev/null || echo "not present (correct)"
```

Expected: `not present (correct)`

- [ ] **Step 4: Run all tests**

```bash
cd /home/developer/repos/dax && python -m pytest tests/ -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/developer/repos/dax
git add dax.py
git commit -m "feat: add dax build subcommand"
```

---

## Task 6: `dax run` and `dax features` subcommands + full CLI wiring

**Files:**
- Modify: `dax.py`

Port `launch_container()` from `daxrun.py` and wire up all three subcommands in `main()`.

- [ ] **Step 1: Add `cmd_run`, `cmd_features`, and `main` to `dax.py`**

Add the following to `dax.py`:

```python
import subprocess


def cmd_run(args):
    config = load_config()
    username = _get_username()
    config['_container_home'] = '/home/{}'.format(username)

    name = config['envname']
    cmd = [
        'docker', 'run', '-it', '--rm',
        '--platform=linux/amd64',
        '--name', name,
        '-h', '{}.fatsec.docker'.format(name),
    ]

    features = list(config['features'])
    if args.features:
        features.extend(args.features.split(','))

    if args.ports:
        config.setdefault('ports', []).extend(args.ports)
        if 'ports' not in features:
            features.append('ports')

    for feature in features:
        cmd.extend(_add_feature(feature, config))

    cmd.append(config['image'])

    dax_print("[+] running: " + ' '.join(cmd))
    if not args.test_only:
        subprocess.run(cmd)


def cmd_features(args):
    _print_features()


def main():
    parser = argparse.ArgumentParser(description='DAX - Docker-based environment manager')
    parser.add_argument('-t', dest='test_only', action='store_true',
                        help='test only - print the docker command and exit')

    subparsers = parser.add_subparsers(dest='command', required=True)

    build_p = subparsers.add_parser('build', help='Build the dax Docker image')
    build_p.add_argument('-c', dest='clean', action='store_true',
                         help='clean build - do not use cached images')

    run_p = subparsers.add_parser('run', help='Launch a dax container')
    run_p.add_argument('-f', dest='features', help='comma-separated features to include')
    run_p.add_argument('-p', dest='ports', action='append',
                       help='port mapping host:container (repeatable)')

    subparsers.add_parser('features', help='List available features')

    args = parser.parse_args()

    if args.command == 'build':
        cmd_build(args)
    elif args.command == 'run':
        cmd_run(args)
    elif args.command == 'features':
        cmd_features(args)


if __name__ == '__main__':
    main()
```

Also add `import argparse` and `import subprocess` to the top of `dax.py` if not already present.

- [ ] **Step 2: Verify `dax features` works**

```bash
cd /home/developer/repos/dax && python dax.py features
```

Expected: prints a list of feature names (workdir, optdir, aws, ssh, dotfiles, msf, ovpn, ports, X11)

- [ ] **Step 3: Verify `dax run -t` works (no docker required)**

```bash
cd /home/developer/repos/dax && python dax.py run -t
```

Expected: prints the docker run command without running it

- [ ] **Step 4: Run all tests**

```bash
cd /home/developer/repos/dax && python -m pytest tests/ -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/developer/repos/dax
git add dax.py
git commit -m "feat: add dax run and dax features subcommands; wire up main()"
```

---

## Task 7: Delete old files

**Files:**
- Delete: `daxbuild.py`, `daxrun.py`, `dax-defaults.json`, `dax.json`

Only do this after all tests pass and you've manually verified `dax build -t` and `dax run -t` produce correct output.

- [ ] **Step 1: Run full test suite one more time**

```bash
cd /home/developer/repos/dax && python -m pytest tests/ -v
```

Expected: all passed

- [ ] **Step 2: Delete old files**

```bash
cd /home/developer/repos/dax
git rm daxbuild.py daxrun.py dax-defaults.json dax.json
```

- [ ] **Step 3: Verify nothing imports the deleted files**

```bash
cd /home/developer/repos/dax && grep -r "daxbuild\|daxrun\|dax-defaults.json\|dax\.json" --include="*.py" .
```

Expected: no matches

- [ ] **Step 4: Run tests again**

```bash
cd /home/developer/repos/dax && python -m pytest tests/ -v
```

Expected: all passed

- [ ] **Step 5: Commit**

```bash
cd /home/developer/repos/dax
git commit -m "chore: remove daxbuild.py, daxrun.py, and stale JSON config files"
```

---

## Task 8: Update README

**Files:**
- Modify: `README.md`

The README still documents `daxbuild.py` and `daxrun.py` with the old flags. Update it to reflect the new CLI.

- [ ] **Step 1: Update the Building section**

Replace the building instructions in `README.md` to reflect `dax build`:

```markdown
## Building

Clone the repository and install dependencies:

    git clone https://github.com/dbfarrow/dax.git
    cd dax
    pip install -r requirements.txt

Build the image:

    python dax.py build

Use `-c` for a clean build (no cache):

    python dax.py build -c
```

- [ ] **Step 2: Update the Running / Usage section**

Replace the usage block with:

```markdown
## Running

    usage: dax.py [-h] [-t] {build,run,features} ...

    optional arguments:
      -h, --help   show this help message and exit
      -t           test only - print the docker command and exit

    subcommands:
      build        Build the dax Docker image
      run          Launch a dax container
      features     List available features

    dax.py run [-f FEATURES] [-p PORTS]
      -f FEATURES  comma-separated features to include
      -p PORTS     port mapping host:container (repeatable)
```

- [ ] **Step 3: Update the Dotfiles section**

Add a note that dotfiles are now mounted at runtime, not baked into the image:

```markdown
### Dotfiles

Dotfiles are mounted from the host at runtime rather than baked into the image.
Configure them in `~/.dax.yaml` under the `dotfiles` key:

    dotfiles:
      ro:
        - ~/.zshrc
        - ~/.vimrc
        - ~/.gitconfig
        - ~/.tmux.conf
      rw:
        - ~/.zsh_history

Files in `ro` are mounted read-only. Files in `rw` are mounted read-write.
The `dotfiles/` directory in the repo is no longer used.
```

- [ ] **Step 4: Update the SSH section**

```markdown
### SSH

SSH access inside the container uses agent forwarding via Docker Desktop's
host agent socket. No private keys are ever copied into the container.

Make sure your keys are loaded before launching:

    ssh-add ~/.ssh/id_rsa

Then use the `ssh` feature:

    python dax.py run -f ssh

From inside the container, `ssh -A user@host` will forward the agent onward.
```

- [ ] **Step 5: Commit**

```bash
cd /home/developer/repos/dax
git add README.md
git commit -m "docs: update README for new dax CLI, dotfiles, and SSH agent forwarding"
```

---

## Self-Review

**Spec coverage check:**

| Requirement | Task |
|---|---|
| Single `dax` CLI with subcommands | Task 6 |
| Externalize Dockerfile as `Dockerfile.tmpl` | Task 2 |
| YAML-only config | Task 3 |
| Drop Python < 3.7 shims | Tasks 5+6 (use `subprocess.run` only) |
| Tool list stays in Dockerfile template | Task 2 |
| Dotfiles as runtime volume mounts, ro/rw | Task 4 |
| SSH via agent forwarding, Mac Docker Desktop socket | Task 4 |
| Remove `sshdir` volume mount | Task 3 (removed from defaults), Task 4 (feature_ssh replaced) |
| Add Claude CLI | Task 2 (in Dockerfile.tmpl) |
| Delete old files | Task 7 |
| Update README | Task 8 |

All requirements covered. No placeholders found.
