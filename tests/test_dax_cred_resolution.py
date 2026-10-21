"""Tests for per-env Claude credential naming.

Listing a bare `claude` in an env's `creds:` asks dax to derive
`claude-<tenant>-<project>` rather than the user hand-maintaining the
convention. That matters because a hand-maintained convention is one users
typo — a real `claud-fre` entry sat unused in ~/.dax.yaml for weeks — and
because per-env credentials are what make rotation in one project unable to
poison another (redesign decision C).
"""
import pytest

from dax_creds.config import (
    credential_users, derived_credential_name, derived_credentials,
    find_named_project_by_dir, get_project_credentials, resolve_credential_names,
    synthesized_credential,
)


FABRIC = {'dir': '/src/fabric', 'tenant': 'personal',
          'creds': ['claude', 'github-dfarrow']}
REGISTRY = {'credentials': {'github-dfarrow': {'provider': 'github'}}}


def test_bare_token_resolves_to_the_convention():
    """The second element is the provider when dax derived the name, None when
    the user named it — that is what tells the caller to synthesize."""
    resolved = resolve_credential_names(FABRIC, 'fabric')

    assert resolved == [('claude-personal-fabric', 'claude'), ('github-dfarrow', None)]


def test_derived_name_shape():
    assert derived_credential_name('claude', 'ysecurity', 'onboard') == 'claude-ysecurity-onboard'


def test_an_explicit_claude_credential_is_redundant_and_skipped():
    """Decision 36: the implied name is the env's Claude credential; an
    explicitly named one in `creds:` is ignored (`config` identifies it)."""
    project = dict(FABRIC, creds=['claude-fre'])
    config = {'credentials': {'claude-fre': {'provider': 'claude'}}}

    assert resolve_credential_names(project, 'fabric', config) == [
        ('claude-personal-fabric', 'claude')]


def test_an_explicit_non_claude_name_is_kept_as_named():
    project = dict(FABRIC, creds=['github-dfarrow'])

    assert resolve_credential_names(project, 'fabric', REGISTRY) == [
        ('claude-personal-fabric', 'claude'), ('github-dfarrow', None)]


def test_bare_token_without_a_tenant_is_refused():
    project = {'dir': '/src/fabric', 'creds': ['claude']}

    with pytest.raises(ValueError) as excinfo:
        resolve_credential_names(project, 'fabric')

    message = excinfo.value.args[0]
    assert 'no tenant' in message
    assert '`tenant: <tenant>`' in message and '~/.dax.yaml' in message


def test_unregistered_derived_credential_is_synthesized():
    """First run for a new env: the name resolves before anything registers it,
    so the caller can report it missing from Keychain and offer a login."""
    creds = get_project_credentials(REGISTRY, FABRIC, 'fabric')

    assert creds['claude-personal-fabric'] == {'provider': 'claude'}
    assert creds['github-dfarrow']['provider'] == 'github'


def test_registered_derived_credential_uses_its_real_definition():
    config = {'credentials': dict(
        REGISTRY['credentials'],
        **{'claude-personal-fabric': {'provider': 'claude', 'browser': 'chrome'}})}

    creds = get_project_credentials(config, FABRIC, 'fabric')

    assert creds['claude-personal-fabric']['browser'] == 'chrome'


def test_unknown_explicit_credential_still_raises():
    project = dict(FABRIC, creds=['nope'])

    with pytest.raises(KeyError):
        get_project_credentials(REGISTRY, project, 'fabric')


def test_without_a_project_name_the_list_is_taken_literally():
    """The older call signature, kept so existing callers don't change meaning."""
    project = dict(FABRIC, creds=['github-dfarrow'])

    assert list(get_project_credentials(REGISTRY, project)) == ['github-dfarrow']


# --- per-provider defaults -------------------------------------------------

DEFAULTS = {'credential_defaults': {'claude': {'browser': 'chrome',
                                               'chrome_profile': 'Profile 2'}}}


def test_derived_credential_inherits_browser_defaults():
    """A derived credential has no entry to hold browser/chrome_profile, so
    without defaults every per-env login opens the wrong browser."""
    config = dict(REGISTRY, **DEFAULTS)

    creds = get_project_credentials(config, FABRIC, 'fabric')

    assert creds['claude-personal-fabric'] == {
        'provider': 'claude', 'browser': 'chrome', 'chrome_profile': 'Profile 2'}


def test_synthesized_credential_without_defaults_is_bare():
    assert synthesized_credential({}, 'claude') == {'provider': 'claude'}


def test_defaults_never_override_the_provider():
    config = {'credential_defaults': {'claude': {'provider': 'nonsense'}}}

    assert synthesized_credential(config, 'claude')['provider'] == 'claude'


def test_a_registered_credential_ignores_the_defaults():
    """An explicit entry owns its own settings."""
    config = dict(DEFAULTS, credentials=dict(
        REGISTRY['credentials'],
        **{'claude-personal-fabric': {'provider': 'claude', 'browser': 'firefox'}}))

    creds = get_project_credentials(config, FABRIC, 'fabric')

    assert creds['claude-personal-fabric']['browser'] == 'firefox'


# --- enumeration -----------------------------------------------------------

def test_derived_credentials_are_enumerable():
    """`dax creds list` on the host must not disagree with `dax-creds list`
    inside the container about which credentials exist."""
    config = dict(REGISTRY, projects={'fabric': FABRIC}, **DEFAULTS)

    derived = derived_credentials(config)

    assert list(derived) == ['claude-personal-fabric']
    assert derived['claude-personal-fabric']['chrome_profile'] == 'Profile 2'


def test_enumeration_skips_envs_that_cannot_resolve():
    config = {'credentials': {}, 'projects': {'broken': {'creds': ['claude']}}}

    assert derived_credentials(config) == {}


def test_credential_users_resolves_bare_tokens():
    config = {'projects': {'fabric': FABRIC}}

    assert credential_users(config, 'claude-personal-fabric') == ['fabric']
    assert credential_users(config, 'github-dfarrow') == ['fabric']
    assert credential_users(config, 'claude') == []


def test_find_named_project_by_dir_returns_the_name(tmp_path):
    config = {'projects': {'fabric': {'dir': str(tmp_path)}}}

    name, project = find_named_project_by_dir(config, tmp_path)

    assert name == 'fabric'
    assert project['dir'] == str(tmp_path)


def test_find_named_project_by_dir_raises_when_absent(tmp_path):
    with pytest.raises(KeyError):
        find_named_project_by_dir({'projects': {}}, tmp_path)


# --- the implied credential reaches every caller (decision 35) -------------
#
# Found by the independent review: every credential lookup derives from an
# env's `creds:` list, so resolving the implied name only in `cmd_run` would
# have left login, destroy, `creds list` and the grant-collision check blind
# to it. It lives in resolve_credential_names, so each caller sees it.

EMPTY_ENV = {'credentials': {}, 'projects': {
    'fabric': {'dir': '/src/fabric', 'tenant': 'personal', 'creds': []}}}


def test_every_env_has_its_implied_claude_credential():
    assert resolve_credential_names(EMPTY_ENV['projects']['fabric'], 'fabric', EMPTY_ENV) == [
        ('claude-personal-fabric', 'claude')]


def test_derived_credentials_lists_the_implied_name():
    assert 'claude-personal-fabric' in derived_credentials(EMPTY_ENV)


def test_credential_users_finds_the_env_behind_an_implied_name():
    assert credential_users(EMPTY_ENV, 'claude-personal-fabric') == ['fabric']


def test_the_grant_collision_name_set_includes_implied_names():
    from dax_creds.config import credential_names_for_provider
    assert 'claude-personal-fabric' in credential_names_for_provider(EMPTY_ENV, 'claude')


def test_creds_list_shows_the_implied_credential(capsys):
    from dax_creds.init import run_creds_list
    run_creds_list(EMPTY_ENV)
    assert 'claude-personal-fabric' in capsys.readouterr().out


def test_an_env_without_a_tenant_has_no_resolvable_credentials():
    with pytest.raises(ValueError, match="env 'fabric' has no tenant"):
        resolve_credential_names({'creds': []}, 'fabric', {})


# --- old Claude entries (decision 36) ----------------------------------------

def test_retired_entries_finds_features_and_creds():
    from dax_creds.config import retired_entries
    config = {
        'credentials': {'claude-fre': {'provider': 'claude'}, 'gh': {'provider': 'github'}},
        'projects': {
            'a': {'features': ['claude_tenant_state', 'auto_claude'], 'creds': ['claude', 'gh']},
            'b': {'features': ['claude'], 'creds': ['claude-fre']},
            'clean': {'features': ['auto_claude'], 'creds': ['gh']},
        },
    }

    assert retired_entries(config) == {
        'a': ['claude_tenant_state (features)', 'claude (creds)'],
        'b': ['claude (features)', 'claude-fre (creds)'],
    }


# --- fixes from step 7's /code-review ------------------------------------------

def test_a_tenantless_envs_explicit_credentials_still_count_as_used():
    """Otherwise `dax creds remove` would delete a credential it still lists."""
    config = {'credentials': {'gh': {'provider': 'github'}},
              'projects': {'old': {'creds': ['gh']}}}
    assert credential_users(config, 'gh') == ['old']


@pytest.mark.parametrize('name,registered,expected', [
    ('claude', None, True),
    ('claude-personal-foo', None, True),        # unregistered: e.g. from another machine's archive
    ('claude-personal-foo', 'github', False),   # registered: the provider decides, not the spelling
    ('claude-fre', 'claude', True),
    ('github-me', None, False),
])
def test_is_claude_credential(name, registered, expected):
    from dax_creds.config import is_claude_credential
    config = {'credentials': {name: {'provider': registered}}} if registered else {}
    assert is_claude_credential(config, name) is expected
