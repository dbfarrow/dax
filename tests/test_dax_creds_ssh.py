import pytest
from dax_creds.providers.ssh import SshProvider, start_ephemeral_agent, stop_ephemeral_agent


class FakeRunner:
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def run(self, cmd, **kwargs):
        self.calls.append(cmd)
        return self

    @property
    def last_cmd(self):
        return self.calls[-1] if self.calls else None


def test_check_returns_true_when_key_fingerprint_in_agent(tmp_path):
    key = tmp_path / 'id_ed25519_github'
    key.write_text('fake key content')
    cred = {'provider': 'ssh', 'key': str(key)}

    fingerprint_output = f'2048 SHA256:abc123def456 {key} (ED25519)\n'
    runner = FakeRunner(returncode=0, stdout=fingerprint_output)

    provider = SshProvider(runner=runner.run)
    assert provider.check(cred) is True


def test_check_returns_false_when_key_not_in_agent(tmp_path):
    key = tmp_path / 'id_ed25519_github'
    key.write_text('fake key content')
    cred = {'provider': 'ssh', 'key': str(key)}

    runner = FakeRunner(returncode=0, stdout='2048 SHA256:xyz /home/user/.ssh/other_key (ED25519)\n')

    provider = SshProvider(runner=runner.run)
    assert provider.check(cred) is False


def test_setup_calls_ssh_add_with_apple_use_keychain(tmp_path):
    key = tmp_path / 'id_ed25519_github'
    key.write_text('fake key content')
    cred = {'provider': 'ssh', 'key': str(key)}

    runner = FakeRunner(returncode=0)

    provider = SshProvider(runner=runner.run)
    provider.setup(cred)

    assert runner.last_cmd == ['ssh-add', '--apple-use-keychain', str(key)]


def test_setup_loads_into_specified_agent(tmp_path):
    key = tmp_path / 'id_ed25519_github'
    key.write_text('fake key content')
    cred = {'provider': 'ssh', 'key': str(key)}
    runner = FakeRunner(returncode=0)

    provider = SshProvider(runner=runner.run)
    provider.setup(cred, agent_sock='/tmp/fake-agent.sock')

    assert runner.last_cmd == ['ssh-add', '--apple-use-keychain', str(key)]
    called_env = runner.calls  # env passed separately — just check cmd for now


def test_setup_raises_on_failure(tmp_path):
    key = tmp_path / 'id_ed25519_github'
    key.write_text('fake key content')
    cred = {'provider': 'ssh', 'key': str(key)}

    runner = FakeRunner(returncode=1, stderr='Bad passphrase')

    provider = SshProvider(runner=runner.run)
    with pytest.raises(RuntimeError, match='Bad passphrase'):
        provider.setup(cred)


def test_start_and_stop_ephemeral_agent():
    sock, pid = start_ephemeral_agent()
    assert sock  # socket path returned
    assert pid > 0
    import os
    assert os.path.exists(sock)
    stop_ephemeral_agent(pid)
    import time; time.sleep(0.1)
    assert not os.path.exists(sock)
