import json
import subprocess
from pathlib import Path

_CHROME_LOCAL_STATE = (
    Path.home() / 'Library' / 'Application Support' / 'Google' / 'Chrome' / 'Local State'
)

_CHROME_APP = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def enumerate_profiles(state_file=None):
    path = Path(state_file) if state_file else _CHROME_LOCAL_STATE
    try:
        with open(path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    cache = data.get('profile', {}).get('info_cache', {})
    profiles = [
        {
            'name': info.get('name', directory),
            'email': info.get('user_name', ''),
            'directory': directory,
        }
        for directory, info in cache.items()
    ]
    profiles.sort(key=lambda p: (0 if p['directory'] == 'Default' else 1, p['directory']))
    return profiles


def open_url_in_profile(url, chrome_profile):
    subprocess.Popen([_CHROME_APP, f'--profile-directory={chrome_profile}', url])
