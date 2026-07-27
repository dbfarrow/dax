import json
import pytest
from dax_creds.chrome import enumerate_profiles, open_url_in_profile


def test_enumerate_profiles_returns_profiles(tmp_path):
    local_state = {
        'profile': {
            'info_cache': {
                'Default': {'name': 'Personal', 'user_name': 'dave@gmail.com'},
                'Profile 1': {'name': 'Customer A', 'user_name': 'dave@customera.com'},
                'Profile 2': {'name': 'Customer B', 'user_name': 'dave@customerb.com'},
            }
        }
    }
    state_file = tmp_path / 'Local State'
    state_file.write_text(json.dumps(local_state))

    profiles = enumerate_profiles(state_file)

    assert len(profiles) == 3
    assert {'name': 'Personal', 'email': 'dave@gmail.com', 'directory': 'Default'} in profiles
    assert {'name': 'Customer A', 'email': 'dave@customera.com', 'directory': 'Profile 1'} in profiles


def test_enumerate_profiles_returns_empty_when_file_missing(tmp_path):
    profiles = enumerate_profiles(tmp_path / 'nonexistent')
    assert profiles == []


def test_enumerate_profiles_sorts_default_first(tmp_path):
    local_state = {
        'profile': {
            'info_cache': {
                'Profile 1': {'name': 'Customer A', 'user_name': 'dave@customera.com'},
                'Default': {'name': 'Personal', 'user_name': 'dave@gmail.com'},
            }
        }
    }
    state_file = tmp_path / 'Local State'
    state_file.write_text(json.dumps(local_state))

    profiles = enumerate_profiles(state_file)

    assert profiles[0]['directory'] == 'Default'
