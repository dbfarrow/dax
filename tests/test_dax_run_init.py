"""`dax init` (`_run_init`, `dax_creds/init.py`): prompts for `tenant` at
registration time, and refuses to register a duplicate when run from inside
an already-registered project's subdirectory.

Both were backlog items: `tenant` previously had no path into a project's
registry entry except the separate `dax env set` command, and `_run_init`
matched projects by exact `dir == cwd` — the same blind spot
`find_enclosing_project`/Gate 0 closed for `dax run`, still open here.

Follows tests/test_dax_creds_add_flow.py's `Answers` pattern: scripted
select/text/confirm/checkbox replies monkeypatched onto the prompt functions,
so an unexpected prompt raises instead of hanging on a real questionary call.
"""
from pathlib import Path

import pytest

import dax_creds.init as init_module
from dax_creds.config import load_dax_config
from dax_creds.init import run_init


class Answers:
    def __init__(self, texts=(), confirms=(), checkboxes=()):
        self.texts = list(texts)
        self.confirms = list(confirms)
        self.checkboxes = list(checkboxes)

    def text(self, prompt, default=None):
        assert self.texts, f'unexpected text prompt: {prompt}'
        return self.texts.pop(0)

    def confirm(self, prompt, default=False):
        assert self.confirms, f'unexpected confirm: {prompt}'
        return self.confirms.pop(0)

    def checkbox(self, prompt, choices):
        assert self.checkboxes, f'unexpected checkbox: {prompt}'
        return self.checkboxes.pop(0)

    def select(self, prompt, choices):
        raise AssertionError(f'unexpected select: {prompt}')


@pytest.fixture
def answers(monkeypatch):
    a = Answers()
    monkeypatch.setattr(init_module, '_prompt', a.text)
    monkeypatch.setattr(init_module, '_q_confirm', a.confirm)
    monkeypatch.setattr(init_module, '_q_checkbox', a.checkbox)
    monkeypatch.setattr(init_module, '_q_select', a.select)
    return a


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text('projects: {}\n')
    monkeypatch.setenv('HOME', str(home))
    return home


def test_registration_prompts_for_and_saves_tenant(home, answers):
    repo = home / 'myproject'
    repo.mkdir()
    # image, tenant, then no credentials to select and none to define.
    answers.texts.extend(['dax-base', 'personal'])
    answers.checkboxes.append([])
    answers.confirms.append(False)

    run_init({'projects': {}}, repo)

    config = load_dax_config()
    assert config['projects']['myproject']['tenant'] == 'personal'


def test_leaving_tenant_blank_registers_with_no_tenant(home, answers):
    repo = home / 'myproject'
    repo.mkdir()
    answers.texts.extend(['dax-base', ''])
    answers.checkboxes.append([])
    answers.confirms.append(False)

    run_init({'projects': {}}, repo)

    config = load_dax_config()
    assert 'tenant' not in config['projects']['myproject']


def test_refuses_to_register_a_subdirectory_of_an_existing_project(home, answers, capsys):
    repo = home / 'myproject'
    repo.mkdir()
    config = {'projects': {'myproject': {'dir': str(repo), 'creds': []}}}

    subdir = repo / 'nested'
    subdir.mkdir()

    result = run_init(config, subdir)

    out = capsys.readouterr().out
    assert 'already-registered project "myproject"' in out
    assert 'nested' not in result.get('projects', {})
    assert len(result['projects']) == 1


def test_updating_an_existing_project_keeps_its_tenant_as_the_default(home, answers):
    repo = home / 'myproject'
    repo.mkdir()
    config = {'projects': {'myproject': {'dir': str(repo), 'tenant': 'personal', 'creds': []}}}
    (home / '.dax.yaml').write_text('')
    from dax_creds.init import save_config
    save_config(config)

    answers.confirms.append(True)  # "Update it?"
    answers.texts.extend(['dax-base', 'personal'])  # image, tenant (unchanged)
    answers.checkboxes.append([])
    answers.confirms.append(False)  # "Define a new credential?"

    run_init(load_dax_config(), repo)

    assert load_dax_config()['projects']['myproject']['tenant'] == 'personal'
