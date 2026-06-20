import os
import re
import subprocess
from pathlib import Path


def _default_runner(cmd, **kwargs):
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


class SshProvider:
    def __init__(self, runner=None):
        self._run = runner or _default_runner

    def check(self, credential_def, agent_sock=None):
        key_path = Path(credential_def['key']).expanduser()
        env = dict(os.environ)
        if agent_sock:
            env['SSH_AUTH_SOCK'] = agent_sock
        result = self._run(['ssh-add', '-l'], env=env)
        return str(key_path) in result.stdout

    def setup(self, credential_def, agent_sock=None):
        key_path = Path(credential_def['key']).expanduser()
        env = dict(os.environ)
        if agent_sock:
            env['SSH_AUTH_SOCK'] = agent_sock
        result = self._run(['ssh-add', '--apple-use-keychain', str(key_path)], env=env)
        if result.returncode != 0:
            raise RuntimeError(f'ssh-add failed: {result.stderr.strip()}')


def start_ephemeral_agent():
    output = subprocess.check_output(['ssh-agent', '-s']).decode()
    sock = re.search(r'SSH_AUTH_SOCK=([^;]+)', output).group(1)
    pid = int(re.search(r'SSH_AGENT_PID=(\d+)', output).group(1))
    return sock, pid


def stop_ephemeral_agent(pid):
    subprocess.run(['kill', str(pid)], capture_output=True)
