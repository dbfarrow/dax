"""Gate 0: cmd_run's project-resolution step, ahead of Gate A/B tenant

Covers dax.py's `find_project_by_dir` -> `find_enclosing_project` fallback:
`dax run` invoked from *inside* an already-registered project (rather than
from its root) must refuse with a clear message, not silently auto-register
the subdirectory as a brand-new, unrelated project via `run_init`. That
auto-register path previously mounted only the subdirectory, silently
dropping repo-root content (`.git`, `.claude/skills/`, etc.) with no error.
"""
import argparse
import yaml
import pytest

from dax import cmd_run


def _write_yaml(path, data):
    with open(path, 'w') as f:
        yaml.dump(data, f)


def _args():
    return argparse.Namespace(features=None, ports=None, test_only=True)


def test_cmd_run_refuses_when_invoked_from_inside_registered_multi_tenant_project(
        tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    repo = home / 'discernment'
    subdir = repo / 'processes' / 'ysecurity-onboard'
    subdir.mkdir(parents=True)
    (subdir / '.dax-tenant').write_text('personal')

    _write_yaml(home / '.dax.yaml', {
        'image': 'test/dax:latest',
        'features': [],
        'projects': {
            'discernment': {
                'dir': str(repo),
                'multi_tenant': True,
                'tenant_subdir': 'processes',
                'creds': [],
            },
        },
    })
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(subdir)

    with pytest.raises(SystemExit) as exc_info:
        cmd_run(_args())

    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert str(subdir) in out
    assert "registered project 'discernment'" in out
    assert str(repo) in out
    assert 'dax run' in out


def test_cmd_run_still_auto_registers_a_genuinely_new_project(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    home.mkdir()
    other_repo = home / 'other-project'
    other_repo.mkdir()
    new_repo = home / 'brand-new-project'
    new_repo.mkdir()

    _write_yaml(home / '.dax.yaml', {
        'image': 'test/dax:latest',
        'features': [],
        'projects': {
            'other-project': {'dir': str(other_repo), 'creds': []},
        },
    })
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(new_repo)

    def fake_run_init(dax_config, cwd):
        dax_config.setdefault('projects', {})['brand-new-project'] = {
            'dir': str(cwd), 'creds': [],
        }
        _write_yaml(home / '.dax.yaml', dax_config)
        return dax_config

    monkeypatch.setattr('dax_creds.init.run_init', fake_run_init)

    # cmd_run proceeds past project resolution into real docker/daemon
    # machinery next, which this test has no interest in exercising - a
    # missing `docker` binary (or any other failure past this point) is fine;
    # only that it did NOT take the refusal branch matters here.
    try:
        cmd_run(_args())
    except SystemExit as e:
        assert e.code != 1
    except Exception:
        pass

    out = capsys.readouterr().out
    assert 'project not registered' in out
    assert 'registered project' not in out
