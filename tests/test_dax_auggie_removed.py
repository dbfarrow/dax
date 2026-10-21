"""Auggie support is removed (env-lifecycle consolidation, decision 39).

What's left to test is how dax treats an env that still lists it, as the
live `ys-augmentcode` does: `auggie` in `features:` and an auggie-provider
credential in `creds:` are ignored (never "unknown feature", never a
credential to serve), reported on every `dax run` until removed, and dropped
from archives on restore.
"""
import argparse

import pytest
import yaml

import dax
from dax import _feature_names, cmd_run
from dax_creds.config import is_retired_credential, resolve_credential_names, retired_entries


CONFIG = {
    'credentials': {'auggie': {'provider': 'auggie'}, 'gh': {'provider': 'github'}},
    'projects': {'ys': {'dir': '/x', 'tenant': 'augment',
                        'features': ['auggie', 'auto_claude'], 'creds': ['auggie', 'gh']}},
}


def test_auggie_is_no_longer_a_feature():
    assert 'auggie' not in _feature_names()


def test_the_credential_providers_no_longer_offer_auggie():
    from dax_creds.init import _CRED_PROVIDERS, _PROVIDER_FIELDS
    assert 'auggie' not in _CRED_PROVIDERS
    assert 'auggie' not in _PROVIDER_FIELDS


@pytest.mark.parametrize('name,config,expected', [
    ('auggie', {}, True),                                              # bare, unregistered
    ('aug-work', {'credentials': {'aug-work': {'provider': 'auggie'}}}, True),
    ('gh', {'credentials': {'gh': {'provider': 'github'}}}, False),
])
def test_is_retired_credential_covers_auggie(name, config, expected):
    assert is_retired_credential(config, name) is expected


def test_an_auggie_credential_is_never_resolved_for_an_env():
    names = [n for n, _ in resolve_credential_names(CONFIG['projects']['ys'], 'ys', CONFIG)]
    assert names == ['claude-augment-ys', 'gh']


def test_auggie_entries_are_reported_as_retired():
    assert retired_entries(CONFIG) == {'ys': ['auggie (features)', 'auggie (creds)']}


def test_an_env_listing_auggie_still_launches(tmp_path, monkeypatch, capsys):
    home = tmp_path / 'home'
    repo = home / 'ys'
    repo.mkdir(parents=True)
    config = dict(CONFIG)
    config['image'] = 'test/dax:latest'
    config['projects'] = {'ys': dict(CONFIG['projects']['ys'], dir=str(repo))}
    with open(home / '.dax.yaml', 'w') as f:
        yaml.dump(config, f)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.chdir(repo)

    cmd_run(argparse.Namespace(features=None, ports=None, test_only=True))

    out = capsys.readouterr().out
    assert 'unknown feature' not in out
    assert '.augment' not in out
    assert "env 'ys' still lists auggie (features), auggie (creds)" in out


def test_restore_drops_auggie_from_an_archive(tmp_path):
    from dax import _restored_entry
    manifest = {'name': 'ys', 'features': ['auggie', 'auto_claude'], 'creds': ['auggie', 'gh']}

    entry, notes = _restored_entry(CONFIG, manifest, tmp_path / 'ys', 'augment')

    assert list(entry['features']) == ['auto_claude']
    assert list(entry['creds']) == ['gh']
    assert notes['dropped_features'] == []  # dropped silently, not kept as #- auggie


def test_the_image_no_longer_installs_auggie():
    from pathlib import Path
    template = (Path(dax.__file__).parent / 'Dockerfile.tmpl').read_text()
    assert 'auggie' not in template.lower()
