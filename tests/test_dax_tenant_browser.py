"""Per-tenant browser profiles for Claude logins (decision 43).

A top-level `tenants:` block, keyed by tenant then provider, is merged over
`credential_defaults.<provider>` when the implied Claude credential is
synthesized, so `dax creds login`, `creds list` and `env show` all open or
show the tenant's profile. The bootstrap wizard asks only for a brand-new
tenant, and `dax run`/`dax config` warn about entries nothing uses.
"""
import argparse

import pytest
import yaml

import dax
from dax import _bootstrap_env
from dax_creds.config import (
    credential_settings_source, derived_credentials, get_project_credentials,
    load_dax_config, synthesized_credential, unknown_tenant_entries,
)
from dax_creds.init import run_creds_list, run_env_show


DEFAULTS = {'claude': {'browser': 'chrome', 'chrome_profile': 'Profile 2'}}


def _config(tenants=None, **extra):
    config = {
        'credential_defaults': DEFAULTS,
        'credentials': {},
        'projects': {
            'fabric': {'dir': '/src/fabric', 'tenant': 'personal'},
            'atticus': {'dir': '/src/atticus', 'tenant': 'ysecurity'},
        },
    }
    if tenants is not None:
        config['tenants'] = tenants
    config.update(extra)
    return config


YSEC = {'ysecurity': {'claude': {'chrome_profile': 'Profile 5'}}}


# --- resolution ----------------------------------------------------------------

def test_a_tenant_setting_is_merged_over_the_default():
    cred = synthesized_credential(_config(YSEC), 'claude', 'ysecurity')
    assert cred == {'provider': 'claude', 'browser': 'chrome', 'chrome_profile': 'Profile 5'}


def test_another_tenant_keeps_the_default():
    cred = synthesized_credential(_config(YSEC), 'claude', 'personal')
    assert cred['chrome_profile'] == 'Profile 2'


def test_a_tenant_block_cannot_change_the_provider():
    config = _config({'ysecurity': {'claude': {'provider': 'github', 'browser': 'arc'}}})
    cred = synthesized_credential(config, 'claude', 'ysecurity')
    assert cred['provider'] == 'claude' and cred['browser'] == 'arc'


def test_an_empty_tenant_block_is_harmless():
    for tenants in ({'ysecurity': None}, {'ysecurity': {'claude': None}}, None):
        assert synthesized_credential(_config(tenants), 'claude', 'ysecurity')[
            'chrome_profile'] == 'Profile 2'


def test_the_login_definition_uses_the_tenant_profile():
    """`dax creds login` resolves derived names through derived_credentials."""
    creds = derived_credentials(_config(YSEC))
    assert creds['claude-ysecurity-atticus']['chrome_profile'] == 'Profile 5'
    assert creds['claude-personal-fabric']['chrome_profile'] == 'Profile 2'


def test_dax_creds_login_opens_the_tenant_profile():
    assert dax._login_credential_def(_config(YSEC), 'claude-ysecurity-atticus')[
        'chrome_profile'] == 'Profile 5'


def test_dax_run_gets_the_tenant_profile():
    config = _config(YSEC)
    creds = get_project_credentials(config, config['projects']['atticus'], 'atticus')
    assert creds['claude-ysecurity-atticus']['chrome_profile'] == 'Profile 5'


def test_an_explicitly_registered_credential_keeps_its_own_settings():
    config = _config(YSEC, credentials={
        'claude-ysecurity-atticus': {'provider': 'claude', 'browser': 'default'}})
    creds = get_project_credentials(config, config['projects']['atticus'], 'atticus')
    assert creds['claude-ysecurity-atticus'] == {'provider': 'claude', 'browser': 'default'}


def test_the_source_is_reported():
    config = _config(YSEC)
    assert credential_settings_source(config, 'ysecurity', 'claude') == 'tenant'
    assert credential_settings_source(config, 'personal', 'claude') == 'default'
    assert credential_settings_source({}, 'personal', 'claude') is None


def test_a_tenant_browser_drops_the_defaults_profile():
    """A browser and its profile are overridden together: plain Chrome (or
    Firefox) for a tenant must not inherit the default's Chrome profile."""
    for browser in ('chrome', 'firefox'):
        config = _config({'ysecurity': {'claude': {'browser': browser}}})
        cred = synthesized_credential(config, 'claude', 'ysecurity')
        assert cred == {'provider': 'claude', 'browser': browser}


@pytest.mark.parametrize('tenants', [
    ['ysecurity'], 'ysecurity', {'ysecurity': 'foo'}, {'ysecurity': ['claude']},
    {'ysecurity': {'claude': 'chrome'}}, {'ysecurity': {'claude': ['chrome']}},
])
def test_a_malformed_tenants_block_is_ignored_not_a_crash(tenants):
    config = _config(tenants)
    assert synthesized_credential(config, 'claude', 'ysecurity')['chrome_profile'] == 'Profile 2'
    derived_credentials(config)
    assert unknown_tenant_entries(config), 'a malformed block should be reported'
    assert not any('`tenants.ysecurity.f`' in p for p in unknown_tenant_entries(config))


def test_a_malformed_credential_defaults_is_ignored_not_a_crash():
    config = _config(credential_defaults={'claude': 'chrome'})
    assert synthesized_credential(config, 'claude', 'personal') == {'provider': 'claude'}


def test_a_profile_without_chrome_is_flagged():
    config = _config({'ysecurity': {'claude': {'chrome_profile': 'Profile 5'}}},
                     credential_defaults={'claude': {'browser': 'firefox'}})
    assert unknown_tenant_entries(config) == [
        "`tenants.ysecurity.claude` sets chrome_profile 'Profile 5', but its browser is "
        "'firefox', so the profile is ignored — add `browser: chrome`"]


def test_dax_config_rejects_a_non_map_tenants():
    assert dax._config_shape_problem({'projects': {}, 'tenants': ['x']}) == (
        '`tenants:` must be a mapping of tenant names to settings')
    assert dax._config_shape_problem({'projects': {}, 'tenants': None}) is None


# --- checks ------------------------------------------------------------------------

def test_a_tenant_no_env_uses_is_flagged():
    problems = unknown_tenant_entries(_config({'ysecurty': {'claude': {}}}))
    assert problems == ["tenant 'ysecurty' in `tenants:` is used by no env yet (typo?)"]


def test_a_provider_other_than_claude_is_flagged():
    problems = unknown_tenant_entries(_config({'ysecurity': {'github': {}}}))
    assert problems == ['`tenants.ysecurity.github` — ignored (a tenant takes claude, hidden)']


def test_a_valid_block_is_silent():
    assert unknown_tenant_entries(_config(YSEC)) == []
    assert unknown_tenant_entries(_config()) == []


def test_a_non_map_is_flagged():
    assert unknown_tenant_entries(_config(['ysecurity'])) == [
        '`tenants:` should be a map of tenant names — ignored']


def test_dax_run_warns_about_a_stray_tenant(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    repo = home / 'fabric'
    repo.mkdir(parents=True)
    (home / '.dax.yaml').write_text(yaml.dump({
        'image': 'dax:latest', 'credentials': {},
        'tenants': {'ysecurty': {'claude': {'chrome_profile': 'Profile 5'}}},
        'projects': {'fabric': {'dir': str(repo), 'tenant': 'personal'}}}))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)

    dax.cmd_run(argparse.Namespace(features=None, ports=None, test_only=True))

    assert "tenant 'ysecurty' in `tenants:` is used by no env yet" in capsys.readouterr().out


# --- what the user sees ----------------------------------------------------------

def test_env_show_says_which_browser_the_login_opens(capsys):
    run_env_show(_config(YSEC), 'atticus')
    assert 'login opens Chrome: Profile 5   (tenant ysecurity)' in capsys.readouterr().out

    run_env_show(_config(YSEC), 'fabric')
    assert 'login opens Chrome: Profile 2   (credential_defaults)' in capsys.readouterr().out

    run_env_show({'projects': _config()['projects']}, 'fabric')
    assert 'login opens default browser   (nothing set)' in capsys.readouterr().out


def test_creds_list_shows_the_tenant_profile(capsys):
    run_creds_list(_config(YSEC))
    rows = [l for l in capsys.readouterr().out.splitlines() if 'claude-ysecurity-atticus' in l]
    assert rows and 'Profile 5' in rows[0]


# --- the wizard ----------------------------------------------------------------------

BROWSERS = [
    {'label': 'Default browser', 'browser': 'default', 'chrome_profile': None},
    {'label': 'Chrome: Work [Profile 2]', 'browser': 'chrome', 'chrome_profile': 'Profile 2'},
    {'label': 'Chrome: Ysec [Profile 5]', 'browser': 'chrome', 'chrome_profile': 'Profile 5'},
]


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text(yaml.dump({
        'defaults': {'image': 'dax-base'}, 'credentials': {},
        'credential_defaults': DEFAULTS,
        'projects': {'fabric': {'dir': str(home / 'fabric'), 'tenant': 'personal'}}}))
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr('dax._known_tenant_names', lambda: {'personal'})
    monkeypatch.setattr('dax_creds.chrome.enumerate_browsers', lambda *a, **k: BROWSERS)
    monkeypatch.setattr('dax_creds.init._q_text', lambda prompt, default=None: default)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda prompt, default=False: True)
    monkeypatch.setattr('dax_creds.init._q_checkbox', lambda prompt, choices: [])
    state = {'asked': [], 'browser': None, 'rows': None}

    def select(prompt, choices, default=None):
        if prompt.startswith('Browser for'):
            state['asked'].append(prompt)
            state['offered'] = [(c.title, c.value) for c in choices]
            return state['browser']
        return 'coding'
    monkeypatch.setattr('dax_creds.init._q_select', select)
    real_table = dax._print_table
    monkeypatch.setattr(dax, '_print_table', lambda h, rows: (
        state.__setitem__('rows', dict(rows)), real_table(h, rows)))

    def run(tenant, browser='same'):
        state['browser'] = browser
        monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known, **k: tenant)
        env_dir = home / 'demo'
        env_dir.mkdir()
        _bootstrap_env(load_dax_config(), env_dir.resolve())
        return load_dax_config()
    return run, state


def test_a_new_tenant_is_asked_and_a_profile_saved(wizard):
    run, state = wizard
    config = run('ysecurity', BROWSERS[2])

    assert state['asked'] == ['Browser for ysecurity Claude logins:']
    assert config['tenants'] == {'ysecurity': {'claude': {'browser': 'chrome',
                                                          'chrome_profile': 'Profile 5'}}}
    assert state['rows']['claude login'] == 'Chrome: Profile 5   (will be saved for tenant ysecurity)'


def test_the_default_is_offered_first_and_writes_nothing(wizard):
    run, state = wizard
    config = run('ysecurity', 'same')

    assert state['offered'][0] == ('Same as default (Chrome: Profile 2)', 'same')
    assert 'Default browser' not in [t for t, _ in state['offered']]
    assert 'tenants' not in config
    assert state['rows']['claude login'] == 'Chrome: Profile 2'


def test_picking_the_default_profile_by_name_writes_nothing(wizard):
    run, state = wizard
    config = run('ysecurity', BROWSERS[1])
    assert 'tenants' not in config


def test_a_tenant_already_in_use_is_not_asked(wizard):
    run, state = wizard
    run('personal', BROWSERS[2])
    assert state['asked'] == []


def test_a_tenant_already_in_tenants_is_not_asked(wizard, tmp_path):
    run, state = wizard
    path = tmp_path / 'home' / '.dax.yaml'
    data = yaml.safe_load(path.read_text())
    data['tenants'] = {'ysecurity': {'claude': {'chrome_profile': 'Profile 5'}}}
    path.write_text(yaml.dump(data))

    run('ysecurity', BROWSERS[1])

    assert state['asked'] == []
    assert state['rows']['claude login'] == 'Chrome: Profile 5   (tenant ysecurity)'


def test_an_emptied_tenants_key_still_saves(wizard, tmp_path):
    """ruamel loads `tenants:` with nothing under it as None."""
    run, state = wizard
    path = tmp_path / 'home' / '.dax.yaml'
    path.write_text(path.read_text() + 'tenants:\n')

    config = run('ysecurity', BROWSERS[2])

    assert state['asked'] == ['Browser for ysecurity Claude logins:']
    assert config['tenants']['ysecurity']['claude']['chrome_profile'] == 'Profile 5'


def test_plain_chrome_is_not_mistaken_for_the_default(wizard):
    """Default is Chrome: Profile 2; plain Chrome is a different choice."""
    run, state = wizard
    config = run('ysecurity', {'label': 'Chrome', 'browser': 'chrome', 'chrome_profile': None})

    assert config['tenants']['ysecurity']['claude'] == {'browser': 'chrome'}
    assert synthesized_credential(config, 'claude', 'ysecurity').get('chrome_profile') is None


def test_chrome_without_a_profile_opens_chrome_not_a_profile_named_none(monkeypatch):
    from dax_creds import daemon
    import subprocess as sp
    opened = []
    monkeypatch.setattr(sp, 'Popen', lambda cmd, *a, **k: opened.append(cmd))
    daemon._default_url_opener('https://x', 'chrome', None)
    assert opened == [['open', '-a', 'Google Chrome', 'https://x']]
