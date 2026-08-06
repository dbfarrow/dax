import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import (
    feature_workdir,
    feature_optdir,
    feature_aws,
    feature_ssh,
    _resolve_ssh_agent_sock,
    feature_dotfiles,
    feature_ports,
    feature_mounts,
    feature_substrate,
    feature_msf,
    feature_ovpn,
    feature_X11,
    feature_webpreview,
    _find_preview_port,
    _format_docker_cmd,
)


def test_feature_workdir_mounts_cwd():
    config = {
        'cwd': '/Users/davefarrow/work/project',
        'workdir_name': 'project',
        '_container_home': '/home/davefarrow',
    }
    opts = feature_workdir(config)
    assert '--volume=/Users/davefarrow/work/project:/home/davefarrow/project' in opts


def test_feature_workdir_mount_name_handles_dots_and_spaces():
    # workdir_name itself is computed by dir_basename() in load_config() (see
    # test_config.py); this just confirms feature_workdir uses it verbatim.
    config = {
        'cwd': '/Users/davefarrow/work/my.project (copy)',
        'workdir_name': 'my.project (copy)',
        '_container_home': '/home/davefarrow',
    }
    opts = feature_workdir(config)
    assert '--volume=/Users/davefarrow/work/my.project (copy):/home/davefarrow/my.project (copy)' in opts


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


def test_feature_mounts_mounts_each_path_as_a_home_sibling(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {
        'mounts': ['~/discernment', '/Users/dfarrow/fatsec/some-process'],
        '_container_home': '/home/dfarrow',
    }
    opts = feature_mounts(config)
    assert '--volume=/Users/dfarrow/discernment:/home/dfarrow/discernment' in opts
    assert '--volume=/Users/dfarrow/fatsec/some-process:/home/dfarrow/some-process' in opts


def test_feature_mounts_not_nested_under_the_workdir_mount(monkeypatch):
    """The whole point: a sibling under $HOME, never a path inside another
    mount — that nesting is the class of bug sync_claude_shared_files exists
    to route around."""
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {'mounts': ['~/discernment'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume=/Users/dfarrow/discernment:/home/dfarrow/discernment']


def test_feature_mounts_is_not_fooled_by_a_trailing_slash(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {'mounts': ['~/discernment/'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert '--volume=/Users/dfarrow/discernment/:/home/dfarrow/discernment' in opts


def test_feature_mounts_warns_if_none_defined(capsys):
    opts = feature_mounts({})
    assert opts == []
    assert 'no mounts defined' in capsys.readouterr().out


def test_feature_mounts_supports_an_explicit_container_name(monkeypatch):
    """host:container_name — the escape hatch for mounting a host directory
    under a name other than its own basename, e.g. the host's real ~/.claude
    mounted alongside (not instead of) an env's claude_tenant_state tree,
    which already owns ~/.claude itself."""
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {'mounts': ['~/.claude:host-claude'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume=/Users/dfarrow/.claude:/home/dfarrow/host-claude']


def test_feature_mounts_explicit_name_does_not_affect_other_entries(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {
        'mounts': ['~/.claude:host-claude', '~/discernment'],
        '_container_home': '/home/dfarrow',
    }
    opts = feature_mounts(config)
    assert '--volume=/Users/dfarrow/.claude:/home/dfarrow/host-claude' in opts
    assert '--volume=/Users/dfarrow/discernment:/home/dfarrow/discernment' in opts


def test_feature_substrate_mounts_and_sets_env_var(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {'substrate': '~/virgil', '_container_home': '/home/dfarrow'}
    opts = feature_substrate(config)
    assert '--volume=/Users/dfarrow/virgil:/home/dfarrow/virgil' in opts
    assert '-e' in opts
    assert 'SUBSTRATE_ROOT=/home/dfarrow/virgil' in opts


def test_feature_substrate_is_not_fooled_by_a_trailing_slash(monkeypatch):
    monkeypatch.setenv('HOME', '/Users/dfarrow')
    config = {'substrate': '~/virgil/', '_container_home': '/home/dfarrow'}
    opts = feature_substrate(config)
    assert '--volume=/Users/dfarrow/virgil/:/home/dfarrow/virgil' in opts


def test_feature_substrate_warns_if_none_configured(capsys):
    opts = feature_substrate({})
    assert opts == []
    assert 'no substrate configured' in capsys.readouterr().out


def test_feature_ssh_returns_tcp_bridge_env_var():
    # feature_ssh no longer resolves or mounts anything itself — cmd_run
    # already started the bridge and stashed its port before the feature
    # loop runs (see test_dax_cmd_run_ssh_bridge.py for that wiring).
    opts = feature_ssh({'_ssh_bridge_port': 54321})
    assert opts == ['-e', 'DAX_SSH_AGENT_TCP_PORT=54321']
    assert not any('ssh-agent' in o for o in opts)


def test_feature_ssh_returns_nothing_without_a_bridge_port(capsys):
    # No _ssh_bridge_port means cmd_run either didn't have the `ssh` feature
    # active, found no socket, or the bridge failed to start — cmd_run
    # already reported why in each of those cases, so this stays silent
    # rather than repeating it.
    opts = feature_ssh({})
    assert opts == []
    assert capsys.readouterr().out == ''


def test_resolve_ssh_agent_sock_prefers_ephemeral_agent(tmp_path):
    ephemeral = tmp_path / 'ephemeral.sock'
    ephemeral.touch()
    config = {'_ephemeral_ssh_sock': str(ephemeral)}
    assert _resolve_ssh_agent_sock(config) == str(ephemeral)


def test_resolve_ssh_agent_sock_falls_back_to_env(tmp_path, monkeypatch):
    sock = tmp_path / 'ssh-auth.sock'
    sock.touch()
    monkeypatch.setenv('SSH_AUTH_SOCK', str(sock))
    assert _resolve_ssh_agent_sock({}) == str(sock)


def test_resolve_ssh_agent_sock_falls_back_to_docker_desktop_socket(tmp_path, monkeypatch):
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    dd_sock = tmp_path / 'docker-desktop.sock'
    dd_sock.touch()
    monkeypatch.setattr('dax._DOCKER_DESKTOP_SSH_SOCK', str(dd_sock))
    assert _resolve_ssh_agent_sock({}) == str(dd_sock)


def test_resolve_ssh_agent_sock_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    monkeypatch.setattr('dax._DOCKER_DESKTOP_SSH_SOCK', str(tmp_path / 'missing.sock'))
    assert _resolve_ssh_agent_sock({}) is None


def test_feature_webpreview_uses_explicit_port(monkeypatch):
    monkeypatch.setattr('dax._is_port_free', lambda p: True)
    config = {
        'cwd': '/home/user/project',
        'webpreview': {'port': 9000},
        '_container_home': '/home/user',
        'workdir_name': 'project',
    }
    opts = feature_webpreview(config)
    assert '-p' in opts
    assert '9000:9000' in opts
    assert 'DAX_PREVIEW_PORT=9000' in config['_shell_cmd']
    assert 'DAX_PREVIEW_DIR=/home/user/project' in config['_shell_cmd']


def test_feature_webpreview_auto_assigns_port(monkeypatch):
    monkeypatch.setattr('dax._is_port_free', lambda p: True)
    config = {
        'cwd': '/home/user/project',
        '_container_home': '/home/user',
        'workdir_name': 'project',
    }
    opts = feature_webpreview(config)
    assert '-p' in opts
    port_mapping = next(o for o in opts if ':' in o and o != '-p')
    host_port = int(port_mapping.split(':')[0])
    assert 8000 <= host_port < 9000
    assert 'DAX_PREVIEW_DIR=/home/user/project' in config['_shell_cmd']


def test_find_preview_port_is_deterministic():
    import dax
    orig = dax._is_port_free
    dax._is_port_free = lambda p: True
    try:
        p1 = _find_preview_port('/home/user/project-a')
        p2 = _find_preview_port('/home/user/project-a')
        assert p1 == p2
    finally:
        dax._is_port_free = orig


def test_find_preview_port_different_dirs_differ():
    import dax
    orig = dax._is_port_free
    dax._is_port_free = lambda p: True
    try:
        p1 = _find_preview_port('/home/user/project-a')
        p2 = _find_preview_port('/home/user/project-b')
        assert p1 != p2
    finally:
        dax._is_port_free = orig


# feature_claude_tenant_state moved to
# tests/test_dax_claude_tenant_state_mount.py when decision B changed what it
# builds. The five tests that lived here covered the multi-tenant shape: a
# directory scan for `.dax-tenant` files, one mount per (tenant, project) pair
# found, and the DAX_MULTI_TENANT/DAX_TENANT_SUBDIR env vars. None of that
# survives — each env is single-tenant, the tenant is a declared label on the
# env's ~/.dax.yaml entry, and the tree mounts at ~/.claude itself.


def test_find_preview_port_skips_busy_ports(monkeypatch):
    import hashlib
    cwd = '/home/user/project'
    digest = int(hashlib.md5(cwd.encode()).hexdigest(), 16)
    first = 8000 + (digest % 1000)
    second = 8000 + ((first - 8000 + 1) % 1000)

    monkeypatch.setattr('dax._is_port_free', lambda p: p != first)
    port = _find_preview_port(cwd)
    assert port == second


# --- _format_docker_cmd -----------------------------------------------------

def test_format_docker_cmd_puts_self_contained_tokens_on_their_own_line():
    lines = _format_docker_cmd(['docker', 'run', '-it', '--rm']).split('\n  ')
    assert lines == ['docker', 'run', '-it', '--rm']


def test_format_docker_cmd_pairs_two_token_flags_with_their_value():
    lines = _format_docker_cmd(['-e', 'DAX_TENANT_STATE=1', '--name', 'fabric']).split('\n  ')
    assert lines == ['-e DAX_TENANT_STATE=1', '--name fabric']


def test_format_docker_cmd_leaves_combined_volume_flag_alone():
    # --volume=host:container is already one token - must not be merged with
    # whatever token happens to come after it.
    lines = _format_docker_cmd(['--volume=/a:/b', '-e', 'X=1']).split('\n  ')
    assert lines == ['--volume=/a:/b', '-e X=1']


def test_format_docker_cmd_pairs_bare_volume_flag_with_its_value():
    # Some features build --volume/-v as two separate tokens instead of the
    # combined form - both shapes coexist in this codebase.
    lines = _format_docker_cmd(['--volume', '/a:/b', '-v', '/c:/d']).split('\n  ')
    assert lines == ['--volume /a:/b', '-v /c:/d']


def test_format_docker_cmd_pairs_workdir_flag_with_its_path():
    lines = _format_docker_cmd(['-w', '/home/dfarrow/fabric', 'dax:latest']).split('\n  ')
    assert lines == ['-w /home/dfarrow/fabric', 'dax:latest']


def test_format_docker_cmd_realistic_mixed_command():
    cmd = [
        'docker', 'run', '-it', '--rm', '--platform=linux/amd64',
        '--name', 'fatsec-fabric', '-h', 'fatsec-fabric.fatsec.docker',
        '--volume=/Users/dfarrow/fatsec/fabric:/home/dfarrow/fabric',
        '-e', 'DAX_TENANT_STATE=1', '-e', 'DAX_PROJECT_NAME=fabric',
        'dax:latest', '/bin/sh', '-c', 'exec /bin/zsh',
    ]
    lines = _format_docker_cmd(cmd).split('\n  ')
    assert lines == [
        'docker', 'run', '-it', '--rm', '--platform=linux/amd64',
        '--name fatsec-fabric', '-h fatsec-fabric.fatsec.docker',
        '--volume=/Users/dfarrow/fatsec/fabric:/home/dfarrow/fabric',
        '-e DAX_TENANT_STATE=1', '-e DAX_PROJECT_NAME=fabric',
        'dax:latest', '/bin/sh', '-c exec /bin/zsh',
    ]
