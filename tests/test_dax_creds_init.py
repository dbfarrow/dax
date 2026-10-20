import sys

import pytest
import yaml
from pathlib import Path
from dax_creds.init import save_config, run_creds_remove, ensure_project_credentials, run_creds_update


class FakeKeyring:
    def __init__(self, stored=None):
        self._stored = dict(stored or {})

    def get_password(self, service, name):
        return self._stored.get((service, name))

    def set_password(self, service, name, value):
        self._stored[(service, name)] = value

    def delete_password(self, service, name):
        key = (service, name)
        if key not in self._stored:
            raise KeyError(name)
        del self._stored[key]


def _load_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)


def test_creds_remove_deletes_token_from_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_abc'})
    config = {'credentials': {'github-dfarrow': {'provider': 'github'}}}
    run_creds_remove(config, 'github-dfarrow', keyring=kr)
    assert kr.get_password('dax-creds', 'github-dfarrow') is None


def test_creds_remove_is_silent_when_not_in_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring()
    config = {'credentials': {'github-dfarrow': {'provider': 'github'}}}
    run_creds_remove(config, 'github-dfarrow', keyring=kr)  # must not raise


def test_creds_remove_rejects_unknown_credential():
    config = {'credentials': {}}
    with pytest.raises(KeyError, match='no-such-cred'):
        run_creds_remove(config, 'no-such-cred')


def test_creds_remove_also_drops_the_config_entry(tmp_path, monkeypatch):
    """Clearing only Keychain left the definition behind, so a fat-fingered
    name could never actually be removed."""
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'claud-fre'): 'blob'})
    config = {'credentials': {'claud-fre': {'provider': 'claude'},
                              'claude-fre': {'provider': 'claude'}},
              'projects': {}}

    run_creds_remove(config, 'claud-fre', keyring=kr)

    assert 'claud-fre' not in config['credentials']
    assert 'claude-fre' in config['credentials']
    assert 'claud-fre' not in (tmp_path / '.dax.yaml').read_text()


def test_creds_remove_refuses_when_a_project_still_references_it(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'gh-shared'): 'blob'})
    config = {'credentials': {'gh-shared': {'provider': 'github'}},
              'projects': {'fabric': {'tenant': 't', 'creds': ['gh-shared']},
                           'dax': {'tenant': 't', 'creds': ['gh-shared']}}}

    with pytest.raises(ValueError, match='dax, fabric'):
        run_creds_remove(config, 'gh-shared', keyring=kr)

    # Refusal must leave both stores untouched, not half-remove it.
    assert 'gh-shared' in config['credentials']
    assert kr.get_password('dax-creds', 'gh-shared') == 'blob'
    assert not (tmp_path / '.dax.yaml').exists()


def test_creds_update_rejects_unknown_credential():
    config = {'credentials': {}}
    with pytest.raises(KeyError, match='no-such-cred'):
        run_creds_update(config, 'no-such-cred')


def test_creds_update_sets_browser_to_chrome_profile(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {'credentials': {'github-dfarrow': {'provider': 'github'}}}

    fake_browsers = [
        {'label': 'Default browser', 'browser': 'default', 'chrome_profile': None},
        {'label': 'Chrome: Work (dave@work.com) [Profile 1]', 'browser': 'chrome', 'chrome_profile': 'Profile 1'},
    ]
    inputs = iter(['2'])  # select Chrome: Work

    run_creds_update(
        config, 'github-dfarrow',
        _prompter=lambda prompt, default=None: next(inputs) or default or '',
        _browser_enumerator=lambda: fake_browsers,
    )

    assert config['credentials']['github-dfarrow']['browser'] == 'chrome'
    assert config['credentials']['github-dfarrow']['chrome_profile'] == 'Profile 1'


def test_creds_update_sets_browser_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {'credentials': {'github-dfarrow': {'provider': 'github', 'browser': 'chrome', 'chrome_profile': 'Profile 1'}}}

    fake_browsers = [
        {'label': 'Default browser', 'browser': 'default', 'chrome_profile': None},
        {'label': 'Firefox', 'browser': 'firefox', 'chrome_profile': None},
    ]
    inputs = iter(['1'])  # select Default browser

    run_creds_update(
        config, 'github-dfarrow',
        _prompter=lambda prompt, default=None: next(inputs) or default or '',
        _browser_enumerator=lambda: fake_browsers,
    )

    assert config['credentials']['github-dfarrow']['browser'] == 'default'
    assert 'chrome_profile' not in config['credentials']['github-dfarrow']


def test_creds_update_preserves_keychain_entry(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_existing'})
    config = {'credentials': {'github-dfarrow': {'provider': 'github'}}}

    run_creds_update(
        config, 'github-dfarrow',
        _prompter=lambda prompt, default=None: default or '',
        _browser_enumerator=lambda: [{'label': 'Default browser', 'browser': 'default', 'chrome_profile': None}],
    )

    assert kr.get_password('dax-creds', 'github-dfarrow') == 'ghp_existing'


def test_creds_update_picks_credential_when_name_omitted(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {
        'credentials': {
            'github-dfarrow': {'provider': 'github', 'browser': 'firefox'},
            'github-work':    {'provider': 'github', 'browser': 'default'},
        }
    }
    fake_browsers = [
        {'label': 'Default browser', 'browser': 'default', 'chrome_profile': None},
        {'label': 'Firefox',         'browser': 'firefox', 'chrome_profile': None},
    ]

    run_creds_update(
        config, name=None,
        _prompter=lambda prompt, default=None: '',  # blank = keep current
        _browser_enumerator=lambda: fake_browsers,
        _picker=lambda prompt, choices: choices[1],  # pick second item = 'github-work...'
    )

    # github-work should have been updated (browser kept as 'default')
    assert config['credentials']['github-work']['browser'] == 'default'


def test_ensure_project_credentials_returns_missing_cred_names(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring()  # empty — token missing

    project_creds = {'github-dfarrow': {'provider': 'github'}}
    config = {'credentials': project_creds}

    missing = ensure_project_credentials(config, project_creds, keyring=kr)

    assert missing == ['github-dfarrow']


def test_ensure_project_credentials_returns_empty_when_cred_in_keychain(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'github-dfarrow'): 'ghp_existing'})

    project_creds = {'github-dfarrow': {'provider': 'github'}}
    config = {'credentials': project_creds}

    missing = ensure_project_credentials(config, project_creds, keyring=kr)

    assert missing == []


def test_creds_remove_can_clear_a_derived_credential(tmp_path, monkeypatch, capsys):
    """A derived credential has no config entry, so `remove` reported it as
    unknown and there was no way to clear a bad one."""
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'claude-personal-fabric'): 'shared-grant'})
    config = {'credentials': {},
              'projects': {'fabric': {'tenant': 'personal', 'creds': ['claude']}}}

    run_creds_remove(config, 'claude-personal-fabric', keyring=kr)

    assert kr.get_password('dax-creds', 'claude-personal-fabric') is None
    out = capsys.readouterr().out
    assert 'derived credential' in out
    assert 'fabric will be offered a fresh login' in out


def test_creds_remove_of_a_derived_credential_writes_no_config(tmp_path, monkeypatch):
    """Nothing to delete from ~/.dax.yaml, so it must not be rewritten."""
    monkeypatch.setenv('HOME', str(tmp_path))
    kr = FakeKeyring({('dax-creds', 'claude-personal-fabric'): 'shared-grant'})
    config = {'credentials': {},
              'projects': {'fabric': {'tenant': 'personal', 'creds': ['claude']}}}

    run_creds_remove(config, 'claude-personal-fabric', keyring=kr)

    assert not (tmp_path / '.dax.yaml').exists()


def test_save_config_writes_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {
        'projects': {'my-project': {'dir': str(tmp_path), 'creds': []}},
    }

    save_config(config)

    saved = _load_yaml(tmp_path / '.dax.yaml')
    assert saved['projects']['my-project']['dir'] == str(tmp_path)


class _FakeAsk:
    def __init__(self, value):
        self._value = value

    def ask(self):
        return self._value


def test_flush_stdin_is_a_silent_noop_without_a_real_terminal():
    """Regression coverage for a real bug found live, 2026-08-30: in
    `dax process restore`'s interactive picker (several questionary prompts
    in a row, looping back after each pick), a leftover buffered keystroke
    (most likely a habitual double Enter on the previous prompt) got
    silently consumed by the *next* prompt the instant it started reading,
    resolving it to its default/first choice before the user ever saw it.
    `_flush_stdin` exists to drain that buffer right before every prompt;
    this confirms it never raises outside a real TTY (pytest's own stdin),
    which is also every non-interactive/scripted/piped-input case."""
    from dax_creds.init import _flush_stdin
    _flush_stdin()  # must not raise


def test_flush_stdin_calls_termios_tcflush_when_available(monkeypatch):
    """Fakes sys.stdin rather than relying on the ambient real one: under
    pytest's default output capture, sys.stdin is replaced with a
    DontReadFromInput-style object whose .fileno() raises — which
    _flush_stdin's own broad except is right to swallow in production, but
    made an earlier version of this test pass only under `-s` (capture
    disabled) and silently do nothing (0 calls recorded, assertion never
    exercising the real code path) under the suite's normal captured run.
    A substitute stdin with a real, working fileno() makes the test
    deterministic regardless of capture mode."""
    import termios

    class _FakeStdin:
        def fileno(self):
            return 99

    monkeypatch.setattr(sys, 'stdin', _FakeStdin())
    calls = []
    monkeypatch.setattr(termios, 'tcflush', lambda fd, mode: calls.append((fd, mode)))

    from dax_creds.init import _flush_stdin
    _flush_stdin()

    assert calls == [(99, termios.TCIFLUSH)]


@pytest.mark.parametrize('wrapper, method, value', [
    ('_q_text', 'text', 'typed'),
    ('_q_select', 'select', 'picked'),
    ('_q_confirm', 'confirm', True),
    ('_q_checkbox', 'checkbox', ['a']),
    ('_q_path', 'path', '/some/where'),
])
def test_prompt_wrappers_flush_stdin_before_asking(monkeypatch, wrapper, method, value):
    """Each interactive prompt wrapper must flush stdin before it asks —
    this is what actually closes the bug: it's not enough for
    `_flush_stdin` to exist, every prompt in a chained/looped sequence has
    to call it first."""
    import questionary
    import dax_creds.init as init_mod

    flushed = []
    monkeypatch.setattr(init_mod, '_flush_stdin', lambda: flushed.append(True))
    monkeypatch.setattr(questionary, method, lambda *a, **k: _FakeAsk(value))

    fn = getattr(init_mod, wrapper)
    if wrapper in ('_q_select', '_q_checkbox'):
        result = fn('prompt', ['a', 'b'])
    else:
        result = fn('prompt')

    assert flushed == [True]
    assert result == value
