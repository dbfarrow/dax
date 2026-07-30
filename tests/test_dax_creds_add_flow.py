"""`dax creds add` asks the provider first, and never asks for a per-env name.

Naming is dax's job (decision C2): a hand-typed name is where `claud-fre` came
from — a misspelling that sat unused in ~/.dax.yaml for weeks, silently, because
a credential name nothing resolves to is simply never matched. Asking the
provider first is what makes skipping the name prompt possible.

The claude path also never imports a token from disk. That copy is what puts two
names on one OAuth grant, so `add` sets up the definition and hands off to the
login that mints an independent grant.
"""
import pytest

import dax_creds.init as init_module
from dax_creds.init import _add_per_env_credential, _env_choices, _run_creds_add


@pytest.fixture
def config():
    return {
        'credential_defaults': {'claude': {'browser': 'chrome',
                                           'chrome_profile': 'Profile 2'}},
        'credentials': {'github-dbfarrow': {'provider': 'github'}},
        'projects': {
            'dax': {'tenant': 'personal', 'dir': '/repos/dax',
                    'creds': ['claude', 'github-dbfarrow']},
            'fabric': {'tenant': 'personal', 'dir': '/repos/fabric',
                       'creds': ['github-dbfarrow']},
            'untenanted': {'dir': '/repos/untenanted', 'creds': []},
        },
    }


class Answers:
    """Scripted replies, so a prompt the flow should not reach raises."""

    def __init__(self, selects=(), texts=(), confirms=()):
        self.selects = list(selects)
        self.texts = list(texts)
        self.confirms = list(confirms)
        self.prompts_seen = []

    def select(self, prompt, choices):
        self.prompts_seen.append(prompt)
        assert self.selects, f'unexpected select: {prompt}'
        return self.selects.pop(0)

    def text(self, prompt, default=None):
        self.prompts_seen.append(prompt)
        assert self.texts, f'unexpected text prompt: {prompt}'
        return self.texts.pop(0)

    def confirm(self, prompt, default=False):
        self.prompts_seen.append(prompt)
        assert self.confirms, f'unexpected confirm: {prompt}'
        return self.confirms.pop(0)


@pytest.fixture
def answers(monkeypatch):
    a = Answers()
    monkeypatch.setattr(init_module, '_q_select', a.select)
    monkeypatch.setattr(init_module, '_prompt', a.text)
    monkeypatch.setattr(init_module, '_q_confirm', a.confirm)
    monkeypatch.setattr(init_module, 'save_config', lambda cfg: None)
    monkeypatch.setattr(init_module, '_keyring_module', None)
    return a


# --- the claude path: no name prompt ----------------------------------------

def test_claude_never_asks_for_a_credential_name(config, answers):
    answers.selects = ['claude', 'dax']
    answers.confirms = [True]          # run the login now?

    _, pending = _run_creds_add(config, cwd='/repos/dax')

    assert pending == 'claude-personal-dax'
    assert not any('name' in p.lower() for p in answers.prompts_seen), \
        answers.prompts_seen


def test_claude_offers_the_login_that_mints_the_grant(config, answers):
    answers.selects = ['claude', 'fabric']
    answers.confirms = [True, True]    # add to creds list, then run login

    _, pending = _run_creds_add(config, cwd='/repos/fabric')

    assert pending == 'claude-personal-fabric'


def test_declining_the_login_still_saves_and_says_how(config, answers, capsys):
    answers.selects = ['claude', 'dax']
    answers.confirms = [False]

    _, pending = _run_creds_add(config, cwd='/repos/dax')

    assert pending is None
    assert 'dax creds login claude-personal-dax' in capsys.readouterr().out


def test_claude_adds_the_bare_token_to_an_env_that_lacks_it(config, answers):
    answers.selects = ['claude', 'fabric']
    answers.confirms = [True, False]

    _run_creds_add(config, cwd='/repos/fabric')

    assert config['projects']['fabric']['creds'] == ['github-dbfarrow', 'claude']


def test_declining_that_leaves_the_creds_list_alone(config, answers, capsys):
    answers.selects = ['claude', 'fabric']
    answers.confirms = [False, False]

    _run_creds_add(config, cwd='/repos/fabric')

    assert config['projects']['fabric']['creds'] == ['github-dbfarrow']
    assert 'go unused' in capsys.readouterr().out


def test_an_env_that_already_uses_it_is_not_asked_again(config, answers):
    """`dax` already lists a bare `claude`, so only the login confirm is due."""
    answers.selects = ['claude', 'dax']
    answers.confirms = [True]

    _run_creds_add(config, cwd='/repos/dax')

    assert config['projects']['dax']['creds'].count('claude') == 1


# --- tenant, asked only where it is genuinely unknown ------------------------

def test_tenant_is_not_asked_when_the_env_already_has_one(config, answers):
    answers.selects = ['claude', 'dax']
    answers.confirms = [True]

    _run_creds_add(config, cwd='/repos/dax')

    assert not any('tenant' in p.lower() for p in answers.prompts_seen), \
        answers.prompts_seen


def test_tenant_is_asked_when_the_env_has_none(config, answers):
    """The derived name cannot be built without it, and C2 refuses rather than
    falling back to a shared credential."""
    answers.selects = ['claude', 'untenanted']
    answers.texts = ['acme']
    answers.confirms = [True, True]

    _, pending = _run_creds_add(config, cwd='/repos/untenanted')

    assert config['projects']['untenanted']['tenant'] == 'acme'
    assert pending == 'claude-acme-untenanted'


def test_no_tenant_given_saves_nothing(config, answers, capsys):
    answers.selects = ['claude', 'untenanted']
    answers.texts = ['']

    _, pending = _run_creds_add(config, cwd='/repos/untenanted')

    assert pending is None
    assert 'tenant' not in config['projects']['untenanted']
    assert 'Nothing saved' in capsys.readouterr().out


# --- credential_defaults ----------------------------------------------------

def test_browser_is_not_asked_when_defaults_exist(config, answers):
    """Browser belongs to the human authenticating, so one defaults block covers
    every derived credential (C4) instead of being re-answered per env."""
    answers.selects = ['claude', 'dax']
    answers.confirms = [True]

    _run_creds_add(config, cwd='/repos/dax')

    assert not any('rowser' in p for p in answers.prompts_seen), answers.prompts_seen


def test_browser_is_asked_once_and_written_to_defaults(config, answers, monkeypatch):
    del config['credential_defaults']
    monkeypatch.setattr(init_module, '_pick_browser',
                        lambda *a, **k: {'browser': 'chrome',
                                         'chrome_profile': 'Profile 7'})
    answers.selects = ['claude', 'dax']
    answers.confirms = [True]

    _run_creds_add(config, cwd='/repos/dax')

    assert config['credential_defaults']['claude'] == {'browser': 'chrome',
                                                       'chrome_profile': 'Profile 7'}


# --- env selection ----------------------------------------------------------

def test_the_env_containing_cwd_is_offered_first(config):
    assert _env_choices(config, cwd='/repos/fabric')[0] == 'fabric'


def test_a_subdirectory_still_resolves_to_its_env(config):
    """`find_enclosing_project`'s case: cwd nested inside a registered env."""
    assert _env_choices(config, cwd='/repos/dax/dax_creds')[0] == 'dax'


def test_an_unrelated_cwd_just_leaves_the_order_alone(config):
    assert set(_env_choices(config, cwd='/tmp/elsewhere')) == set(config['projects'])


def test_no_envs_registered_is_refused_with_a_pointer(answers, capsys):
    _, pending = _add_per_env_credential({'projects': {}}, 'claude', cwd='/tmp')

    assert pending is None
    assert 'dax init' in capsys.readouterr().out


# --- the honest report on the non-per-env path -------------------------------

def test_a_credential_with_no_secret_does_not_claim_to_be_saved(
        config, answers, monkeypatch, capsys):
    """"saved" is true of the YAML write but answers the wrong question — the
    user is asking whether the credential works now."""
    monkeypatch.setattr(init_module, '_define_credential',
                        lambda *a, **k: {'provider': 'gmail'})
    monkeypatch.setattr(init_module, '_setup_google_credential',
                        lambda *a, **k: False)
    answers.selects = ['gmail']
    answers.texts = ['gmail-new']

    _run_creds_add(config, cwd='/repos/dax')

    out = capsys.readouterr().out
    assert 'no secret is stored for it yet' in out
    assert 'dax creds login gmail-new' in out


def test_a_credential_that_did_store_reports_plainly(
        config, answers, monkeypatch, capsys):
    monkeypatch.setattr(init_module, '_define_credential',
                        lambda *a, **k: {'provider': 'ssh', 'key': '~/.ssh/id_rsa'})
    monkeypatch.setattr(init_module, '_setup_ssh_credential', lambda *a, **k: True)
    answers.selects = ['ssh']
    answers.texts = ['ssh-new']

    _run_creds_add(config, cwd='/repos/dax')

    out = capsys.readouterr().out
    assert 'saved' in out
    assert 'no secret' not in out


def test_provider_is_asked_before_the_name(config, answers, monkeypatch):
    """Reversed from the original flow, and it is what lets the claude path skip
    the name prompt entirely."""
    monkeypatch.setattr(init_module, '_define_credential',
                        lambda *a, **k: {'provider': 'ssh'})
    monkeypatch.setattr(init_module, '_setup_ssh_credential', lambda *a, **k: True)
    answers.selects = ['ssh']
    answers.texts = ['ssh-new']

    _run_creds_add(config, cwd='/repos/dax')

    assert answers.prompts_seen[0] == 'Provider:'
