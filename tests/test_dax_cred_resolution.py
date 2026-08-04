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


def test_explicit_name_wins_over_the_convention():
    """Backward compatibility, and a deliberate escape hatch for sharing one
    credential across two envs."""
    project = dict(FABRIC, creds=['claude-fre'])

    assert resolve_credential_names(project, 'fabric') == [('claude-fre', None)]


def test_bare_token_without_a_tenant_is_refused():
    project = {'dir': '/src/fabric', 'creds': ['claude']}

    with pytest.raises(ValueError) as excinfo:
        resolve_credential_names(project, 'fabric')

    message = excinfo.value.args[0]
    assert 'no tenant' in message
    assert 'dax env set fabric tenant' in message


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
