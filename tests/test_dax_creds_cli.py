"""Tests for the `dax-creds resolve-tenant` subcommand.

Exercises the real dax_creds.tenant module through cli.py's dispatch - the
module itself has full unit coverage in tests/test_dax_creds_tenant.py; this
file is about the CLI plumbing around it (argv/env handling, stdout/stderr
contract, exit codes) that tests/test_dax_creds_claude_wrapper.py stubs out
rather than exercises.
"""
import pytest

from dax_creds.cli import main


def _declare(path, tenant):
    path.mkdir(parents=True, exist_ok=True)
    (path / '.dax-tenant').write_text(tenant)


def test_resolve_tenant_prints_tenant_and_project_on_success(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('DAX_PROJECT_NAME', 'fabric')
    monkeypatch.delenv('DAX_MULTI_TENANT', raising=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    main()

    out = capsys.readouterr()
    assert out.out == 'TENANT=personal\nPROJECT=fabric\n'
    assert out.err == ''


def test_resolve_tenant_exits_nonzero_and_prints_detail_on_refuse(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    subdir = repo / 'src'
    subdir.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('DAX_PROJECT_NAME', 'fabric')
    monkeypatch.delenv('DAX_MULTI_TENANT', raising=False)
    monkeypatch.chdir(subdir)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0
    out = capsys.readouterr()
    assert out.out == ''
    assert 'project root' in out.err


def test_resolve_tenant_undeclared_root_refuses(tmp_path, monkeypatch, capsys):
    # No automatic unattributed/<reponame> fallback (dropped 2026-07-28) -
    # dax run's classification step should always resolve this before a
    # container ever launches; a wrapper invocation reaching an undeclared
    # root refuses instead of silently pooling into a default tenant.
    home = tmp_path / 'home'
    repo = home / 'iandidit'
    repo.mkdir(parents=True)  # no .dax-tenant at all -> undeclared
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('DAX_PROJECT_NAME', 'iandidit')
    monkeypatch.delenv('DAX_MULTI_TENANT', raising=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0
    out = capsys.readouterr()
    assert out.out == ''
    assert 'dax tenant set' in out.err


def test_resolve_tenant_respects_dax_multi_tenant_env_var(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    repo = home / 'discernment'
    sub_a = repo / 'project_ysecurity_comp_model'
    _declare(sub_a, 'ysecurity')
    _declare(repo / 'project_augment', 'augment')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('DAX_PROJECT_NAME', 'discernment')
    monkeypatch.setenv('DAX_MULTI_TENANT', '1')
    monkeypatch.chdir(sub_a)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    main()

    out = capsys.readouterr()
    assert out.out == 'TENANT=ysecurity\nPROJECT=project_ysecurity_comp_model\n'


def test_resolve_tenant_respects_dax_tenant_subdir_env_var(
        tmp_path, monkeypatch, capsys):
    # discernment's actual tenant subdirectories sit under processes/, not
    # directly under the repo root - DAX_TENANT_SUBDIR is how that reaches
    # the wrapper/CLI without generalizing the resolution table itself.
    home = tmp_path / 'home'
    repo = home / 'discernment'
    sub_a = repo / 'processes' / 'project_ysecurity_comp_model'
    _declare(sub_a, 'ysecurity')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('DAX_PROJECT_NAME', 'discernment')
    monkeypatch.setenv('DAX_MULTI_TENANT', '1')
    monkeypatch.setenv('DAX_TENANT_SUBDIR', 'processes')
    monkeypatch.chdir(sub_a)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    main()

    out = capsys.readouterr()
    assert out.out == 'TENANT=ysecurity\nPROJECT=project_ysecurity_comp_model\n'


def test_resolve_tenant_refuses_in_an_unrelated_home_level_mount(
        tmp_path, monkeypatch, capsys):
    # ~/.claude, ~/.augment, ~/.aws etc. sit at the same level as a project's
    # own mount. DAX_PROJECT_NAME is what tells resolve-tenant "fabric" is the
    # real project here, not whatever directory the user happened to cd into.
    home = tmp_path / 'home'
    _declare(home / 'fabric', 'personal')
    other_mount = home / '.claude'
    other_mount.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('DAX_PROJECT_NAME', 'fabric')
    monkeypatch.delenv('DAX_MULTI_TENANT', raising=False)
    monkeypatch.chdir(other_mount)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0
    out = capsys.readouterr()
    assert 'unattributed' not in out.out


def test_resolve_tenant_fails_clearly_when_project_name_env_var_missing(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    _declare(repo, 'personal')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('DAX_PROJECT_NAME', raising=False)
    monkeypatch.chdir(repo)
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant'])

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code != 0
    out = capsys.readouterr()
    assert 'DAX_PROJECT_NAME' in out.err


def test_resolve_tenant_rejects_extra_arguments(monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', ['dax-creds', 'resolve-tenant', 'extra'])

    with pytest.raises(SystemExit):
        main()

    out = capsys.readouterr()
    assert 'Usage: dax-creds resolve-tenant' in out.err
