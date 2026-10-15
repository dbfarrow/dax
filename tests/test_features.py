import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from dax import (
    feature_workdir,
    feature_optdir,
    feature_aws,
    feature_dotfiles,
    feature_ports,
    feature_mounts,
    feature_substrate,
    feature_msf,
    feature_ovpn,
    feature_X11,
    feature_webpreview,
    feature_auto_claude,
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


def _make_wired_substrate(root):
    """A substrate repo real enough to pass feature_substrate's own
    availability check: shared/wire.sh present and executable."""
    wire_sh = root / 'shared' / 'wire.sh'
    wire_sh.parent.mkdir(parents=True, exist_ok=True)
    wire_sh.write_text('#!/bin/sh\n')
    wire_sh.chmod(0o755)


def test_feature_mounts_mounts_each_path_as_a_home_sibling(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'discernment').mkdir()
    other = tmp_path / 'fatsec' / 'some-process'
    other.mkdir(parents=True)
    config = {
        'mounts': ['~/discernment', str(other)],
        '_container_home': '/home/dfarrow',
    }
    opts = feature_mounts(config)
    assert '--volume={}/discernment:/home/dfarrow/discernment'.format(tmp_path) in opts
    assert '--volume={}:/home/dfarrow/some-process'.format(other) in opts


def test_feature_mounts_not_nested_under_the_workdir_mount(tmp_path, monkeypatch):
    """The whole point: a sibling under $HOME, never a path inside another
    mount — that nesting is the class of bug sync_claude_shared_files exists
    to route around."""
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'discernment').mkdir()
    config = {'mounts': ['~/discernment'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume={}/discernment:/home/dfarrow/discernment'.format(tmp_path)]


def test_feature_mounts_is_not_fooled_by_a_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'discernment').mkdir()
    config = {'mounts': ['~/discernment/'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert '--volume={}/discernment/:/home/dfarrow/discernment'.format(tmp_path) in opts


def test_feature_mounts_warns_if_none_defined(capsys):
    opts = feature_mounts({})
    assert opts == []
    assert 'no mounts defined' in capsys.readouterr().out


def test_feature_mounts_refuses_a_missing_source(tmp_path, monkeypatch, capsys):
    """Docker auto-creates a missing bind-mount source as an empty directory
    rather than failing, so without this check a mount that's supposed to
    come from elsewhere (another machine, a network share) but isn't
    currently there would start the container fine with nothing behind it.
    Found live, 2026-08-31, for feature_substrate's identical case."""
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {'mounts': ['~/not-actually-there'], '_container_home': '/home/dfarrow'}

    with pytest.raises(SystemExit):
        feature_mounts(config)

    assert 'is it mounted' in capsys.readouterr().out


def test_feature_mounts_supports_an_explicit_container_name(tmp_path, monkeypatch):
    """host:container_name — the escape hatch for mounting a host directory
    under a name other than its own basename, e.g. the host's real ~/.claude
    mounted alongside (not instead of) an env's claude_tenant_state tree,
    which already owns ~/.claude itself."""
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.claude').mkdir()
    config = {'mounts': ['~/.claude:host-claude'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume={}/.claude:/home/dfarrow/host-claude'.format(tmp_path)]


def test_feature_mounts_explicit_name_does_not_affect_other_entries(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.claude').mkdir()
    (tmp_path / 'discernment').mkdir()
    config = {
        'mounts': ['~/.claude:host-claude', '~/discernment'],
        '_container_home': '/home/dfarrow',
    }
    opts = feature_mounts(config)
    assert '--volume={}/.claude:/home/dfarrow/host-claude'.format(tmp_path) in opts
    assert '--volume={}/discernment:/home/dfarrow/discernment'.format(tmp_path) in opts


def test_feature_mounts_defaults_to_read_write(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'discernment').mkdir()
    config = {'mounts': ['~/discernment'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume={}/discernment:/home/dfarrow/discernment'.format(tmp_path)]
    assert not opts[0].endswith(':ro')


def test_feature_mounts_ro_with_default_container_name(tmp_path, monkeypatch):
    """Empty middle field (host::ro) keeps the default basename-derived name
    while still marking it read-only — the container name and the ro flag
    are two independent, positionally-separate fields."""
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'discernment').mkdir()
    config = {'mounts': ['~/discernment::ro'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume={}/discernment:/home/dfarrow/discernment:ro'.format(tmp_path)]


def test_feature_mounts_ro_with_explicit_container_name(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.claude').mkdir()
    config = {'mounts': ['~/.claude:host-claude:ro'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume={}/.claude:/home/dfarrow/host-claude:ro'.format(tmp_path)]


def test_feature_mounts_ro_does_not_leak_into_other_entries(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / '.claude').mkdir()
    (tmp_path / 'discernment').mkdir()
    config = {
        'mounts': ['~/.claude:host-claude:ro', '~/discernment'],
        '_container_home': '/home/dfarrow',
    }
    opts = feature_mounts(config)
    assert '--volume={}/.claude:/home/dfarrow/host-claude:ro'.format(tmp_path) in opts
    assert '--volume={}/discernment:/home/dfarrow/discernment'.format(tmp_path) in opts
    assert not any(o.endswith('discernment:ro') for o in opts)


def test_feature_mounts_only_the_literal_ro_suffix_marks_read_only(tmp_path, monkeypatch):
    """A container name that happens to end up in the third field but isn't
    literally 'ro' must not be silently treated as read-only."""
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'discernment').mkdir()
    config = {'mounts': ['~/discernment:mydir:rw'], '_container_home': '/home/dfarrow'}
    opts = feature_mounts(config)
    assert opts == ['--volume={}/discernment:/home/dfarrow/mydir'.format(tmp_path)]


def test_feature_substrate_mounts_and_sets_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _make_wired_substrate(tmp_path / 'virgil')
    config = {'substrate': '~/virgil', '_container_home': '/home/dfarrow'}
    opts = feature_substrate(config)
    assert '--volume={}/virgil:/home/dfarrow/virgil'.format(tmp_path) in opts
    assert '-e' in opts
    assert 'SUBSTRATE_ROOT=/home/dfarrow/virgil' in opts


def test_feature_substrate_is_not_fooled_by_a_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _make_wired_substrate(tmp_path / 'virgil')
    config = {'substrate': '~/virgil/', '_container_home': '/home/dfarrow'}
    opts = feature_substrate(config)
    assert '--volume={}/virgil/:/home/dfarrow/virgil'.format(tmp_path) in opts


def test_feature_substrate_warns_if_none_configured(capsys):
    opts = feature_substrate({})
    assert opts == []
    assert 'no substrate configured' in capsys.readouterr().out


def test_feature_substrate_refuses_a_missing_mount(tmp_path, monkeypatch, capsys):
    """The real-world case this was built for: a substrate mounted from
    another machine (over a network share, a mount that isn't always up)
    isn't currently there. Docker would otherwise auto-create an empty
    directory and start the container fine with nothing behind it —
    entrypoint.sh's own wire.sh-missing check only catches this *after*
    boot. Found live, 2026-08-31."""
    monkeypatch.setenv('HOME', str(tmp_path))
    (tmp_path / 'virgil').mkdir()  # exists, but no shared/wire.sh inside
    config = {'substrate': '~/virgil', '_container_home': '/home/dfarrow'}

    with pytest.raises(SystemExit):
        feature_substrate(config)

    assert 'is the mount up' in capsys.readouterr().out.lower()


def test_feature_substrate_refuses_a_non_executable_wire_sh(tmp_path, monkeypatch):
    """Present but not executable is exactly as broken as absent — matches
    entrypoint.sh's own `[ -x ... ]` check, not just `[ -e ... ]`."""
    monkeypatch.setenv('HOME', str(tmp_path))
    root = tmp_path / 'virgil'
    wire_sh = root / 'shared' / 'wire.sh'
    wire_sh.parent.mkdir(parents=True)
    wire_sh.write_text('#!/bin/sh\n')
    wire_sh.chmod(0o644)  # not executable
    config = {'substrate': '~/virgil', '_container_home': '/home/dfarrow'}

    with pytest.raises(SystemExit):
        feature_substrate(config)


# `ssh` is no longer a feature at all (removed 2026-08): forwarding turns on
# purely because a project's `creds:` resolves an `ssh`-provider credential,
# folded directly into cmd_run's existing credential-resolution flow rather
# than a separate feature_ssh/_resolve_ssh_agent_sock pair. See
# test_dax_cmd_run_ssh_bridge.py for that behavior.


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
    assert 'DAX_PREVIEW_PORT=9000' in config['_shell_cmd_prefix']
    assert 'DAX_PREVIEW_DIR=/home/user/project' in config['_shell_cmd_prefix']
    # webpreview no longer decides the foreground process itself — that's
    # cmd_run's job, composing this prefix with whatever _final_exec is (or
    # the plain shell, by default).
    assert '_final_exec' not in config
    assert 'exec' not in config['_shell_cmd_prefix']


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
    assert 'DAX_PREVIEW_DIR=/home/user/project' in config['_shell_cmd_prefix']


def test_feature_auto_claude_sets_final_exec_to_tmux_running_claude():
    config = {'workdir_name': 'myproject'}
    opts = feature_auto_claude(config)
    assert opts == []
    assert config['_final_exec'] == "tmux new-session -n myproject claude"


def test_feature_auto_claude_quotes_a_workdir_name_with_spaces():
    # workdir_name can contain spaces/parens (see feature_workdir's own
    # tests) — this has to survive being embedded in a single shell string.
    config = {'workdir_name': 'my.project (copy)'}
    opts = feature_auto_claude(config)
    assert config['_final_exec'] == "tmux new-session -n 'my.project (copy)' claude"


def test_feature_auto_claude_does_not_clobber_webpreviews_prefix():
    """Both features run in the same cmd_run pass — webpreview (baseline)
    always runs first, so this must add to what it already set, not replace
    the whole shell-command story wholesale."""
    config = {'workdir_name': 'myproject', '_shell_cmd_prefix': 'dax-preview & '}
    feature_auto_claude(config)
    assert config['_shell_cmd_prefix'] == 'dax-preview & '
    assert config['_final_exec'] == "tmux new-session -n myproject claude"


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
