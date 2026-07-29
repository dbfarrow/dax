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
    feature_webpreview,
    feature_claude_tenant_state,
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


def test_feature_ssh_returns_agent_forwarding(tmp_path, monkeypatch):
    sock = tmp_path / 'ssh-auth.sock'
    sock.touch()
    monkeypatch.setenv('SSH_AUTH_SOCK', str(sock))
    opts = feature_ssh({})
    assert any('ssh-agent' in o for o in opts)
    assert '-e' in opts
    assert 'SSH_AUTH_SOCK=/ssh-agent' in opts


def test_feature_ssh_warns_if_no_socket(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('SSH_AUTH_SOCK', raising=False)
    monkeypatch.setattr('dax._DOCKER_DESKTOP_SSH_SOCK', str(tmp_path / 'missing.sock'))
    opts = feature_ssh({})
    assert opts == []


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


# --- feature_claude_tenant_state --------------------------------------------
#
# Unlike every other feature_* function above, this one genuinely needs a real
# directory tree (it calls dax_creds.tenant.all_tenant_projects, which scans
# for .dax-tenant files) - synthetic in-memory config dicts alone aren't
# enough, hence tmp_path here where the rest of this file doesn't need it.
# The resolution logic itself is fully covered in tests/test_dax_creds_tenant.py;
# these tests are about the argv/env-var construction on top of it.

def _declare_tenant(path, tenant):
    path.mkdir(parents=True, exist_ok=True)
    (path / '.dax-tenant').write_text(tenant)


def test_feature_claude_tenant_state_single_tenant_undeclared_mounts_nothing(
        tmp_path, monkeypatch):
    # No automatic unattributed/<reponame> fallback (dropped 2026-07-28) -
    # dax run's classification step should always resolve this before mounts
    # are computed, so an undeclared repo reaching here mounts nothing at
    # all rather than a default location.
    monkeypatch.setenv('HOME', str(tmp_path / 'realhome'))
    repo = tmp_path / 'doot'
    repo.mkdir()
    config = {
        'cwd': str(repo),
        'workdir_name': 'doot',
        '_container_home': '/home/dfarrow',
    }

    opts = feature_claude_tenant_state(config)

    assert 'DAX_TENANT_STATE=1' in opts
    assert 'DAX_PROJECT_NAME=doot' in opts
    assert not any(o.startswith('DAX_MULTI_TENANT') for o in opts)
    assert not any(o.startswith('--volume=') for o in opts)


def test_feature_claude_tenant_state_single_tenant_declared(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'realhome'))
    repo = tmp_path / 'fabric'
    _declare_tenant(repo, 'personal')
    config = {
        'cwd': str(repo),
        'workdir_name': 'fabric',
        '_container_home': '/home/dfarrow',
    }

    opts = feature_claude_tenant_state(config)

    host_dir = tmp_path / 'realhome' / '.local' / 'state' / 'dax' / 'tenants' / 'personal' / 'fabric'
    container_dir = '/home/dfarrow/.local/state/dax/tenants/personal/fabric'
    assert '--volume={}:{}'.format(host_dir, container_dir) in opts


def test_feature_claude_tenant_state_multi_tenant_mounts_each_mapped_child(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'realhome'))
    repo = tmp_path / 'discernment'
    _declare_tenant(repo / 'project_ysecurity_comp_model', 'ysecurity')
    _declare_tenant(repo / 'project_augment', 'augment')
    config = {
        'cwd': str(repo),
        'workdir_name': 'discernment',
        '_container_home': '/home/dfarrow',
        'multi_tenant': True,
    }

    opts = feature_claude_tenant_state(config)

    assert 'DAX_MULTI_TENANT=1' in opts
    for tenant, project in [('ysecurity', 'project_ysecurity_comp_model'),
                            ('augment', 'project_augment')]:
        host_dir = (tmp_path / 'realhome' / '.local' / 'state' / 'dax' / 'tenants'
                    / tenant / project)
        container_dir = '/home/dfarrow/.local/state/dax/tenants/{}/{}'.format(tenant, project)
        assert '--volume={}:{}'.format(host_dir, container_dir) in opts


def test_feature_claude_tenant_state_multi_tenant_with_tenant_subdir(tmp_path, monkeypatch):
    # discernment's actual tenant subdirectories sit under processes/, not
    # directly under the repo root - config['tenant_subdir'] is how that
    # reaches all_tenant_projects(), and DAX_TENANT_SUBDIR is how it reaches
    # the wrapper/CLI in the container.
    monkeypatch.setenv('HOME', str(tmp_path / 'realhome'))
    repo = tmp_path / 'discernment'
    _declare_tenant(repo / 'processes' / 'project_ysecurity_comp_model', 'ysecurity')
    _declare_tenant(repo / 'project_at_root_should_be_ignored', 'should-not-count')
    config = {
        'cwd': str(repo),
        'workdir_name': 'discernment',
        '_container_home': '/home/dfarrow',
        'multi_tenant': True,
        'tenant_subdir': 'processes',
    }

    opts = feature_claude_tenant_state(config)

    assert 'DAX_MULTI_TENANT=1' in opts
    assert 'DAX_TENANT_SUBDIR=processes' in opts
    host_dir = (tmp_path / 'realhome' / '.local' / 'state' / 'dax' / 'tenants'
                / 'ysecurity' / 'project_ysecurity_comp_model')
    container_dir = '/home/dfarrow/.local/state/dax/tenants/ysecurity/project_ysecurity_comp_model'
    assert '--volume={}:{}'.format(host_dir, container_dir) in opts
    assert not any('should-not-count' in o for o in opts)


def test_feature_claude_tenant_state_multi_tenant_no_mounts_when_nothing_labeled(
        tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'realhome'))
    repo = tmp_path / 'discernment'
    (repo / 'project_new').mkdir(parents=True)
    config = {
        'cwd': str(repo),
        'workdir_name': 'discernment',
        '_container_home': '/home/dfarrow',
        'multi_tenant': True,
    }

    opts = feature_claude_tenant_state(config)

    assert not any(o.startswith('--volume=') for o in opts)
    assert 'DAX_TENANT_STATE=1' in opts


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
