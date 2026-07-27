import pytest
import yaml
from pathlib import Path
from dax_creds.init import register_project, save_config, run_creds_remove, ensure_project_credentials, run_creds_update, _setup_claude_credential


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


def test_register_project_adds_project_entry(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config_path = tmp_path / '.dax.yaml'
    config_path.write_text(yaml.dump({
        'defaults': {'image': 'dax-base'},
        'credentials': {},
        'projects': {},
    }))

    project_dir = tmp_path / 'my-project'
    config = _load_yaml(config_path)
    register_project(config, name='my-project', project_dir=project_dir,
                     image='dax-base', creds=['github-dfarrow'])

    assert config['projects']['my-project']['dir'] == str(project_dir)
    assert config['projects']['my-project']['creds'] == ['github-dfarrow']
    assert config['projects']['my-project']['image'] == 'dax-base'


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


def test_setup_claude_imports_credentials_json_as_blob(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('HOME', str(tmp_path))
    creds_dir = tmp_path / '.claude'
    creds_dir.mkdir()
    payload = {'claudeAiOauth': {'accessToken': 'sk-ant-oat01-abc', 'refreshToken': 'sk-ant-ort01-xyz'}}
    (creds_dir / 'credentials.json').write_text(json.dumps(payload))

    class FakeKeyring:
        def __init__(self):
            self._stored = {}
        def get_password(self, s, n):
            return self._stored.get((s, n))
        def set_password(self, s, n, v):
            self._stored[(s, n)] = v

    kr = FakeKeyring()
    import dax_creds.providers.claude as _cm
    monkeypatch.setattr(_cm, '_keyring', kr)

    _setup_claude_credential('claude-work', {'provider': 'claude'})

    stored = json.loads(kr.get_password('dax-creds', 'claude-work'))
    assert stored == payload


def test_setup_claude_skips_when_already_in_keychain(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))

    class FakeKeyring:
        def get_password(self, s, n):
            return 'existing-token'
        def set_password(self, s, n, v):
            raise AssertionError('should not be called')

    import dax_creds.providers.claude as _cm
    monkeypatch.setattr(_cm, '_keyring', FakeKeyring())

    _setup_claude_credential('claude-work', {'provider': 'claude'})

    out = capsys.readouterr().out
    assert 'already in Keychain' in out


def test_save_config_writes_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config = {
        'projects': {'my-project': {'dir': str(tmp_path), 'creds': []}},
    }

    save_config(config)

    saved = _load_yaml(tmp_path / '.dax.yaml')
    assert saved['projects']['my-project']['dir'] == str(tmp_path)
