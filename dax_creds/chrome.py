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


_BROWSER_APPS = [
    ('firefox', 'Firefox',       '/Applications/Firefox.app'),
    ('safari',  'Safari',        '/Applications/Safari.app'),
    ('arc',     'Arc',           '/Applications/Arc.app'),
    ('brave',   'Brave Browser', '/Applications/Brave Browser.app'),
]

_CHROME_APP_PATH = '/Applications/Google Chrome.app'


def enumerate_browsers(state_file=None):
    """Return a flat list of browser options for URL opening.

    Each item: {'label': str, 'browser': str, 'chrome_profile': str|None}
    """
    options = [{'label': 'Default browser', 'browser': 'default', 'chrome_profile': None}]

    for key, label, path in _BROWSER_APPS:
        if Path(path).exists():
            options.append({'label': label, 'browser': key, 'chrome_profile': None})

    if Path(_CHROME_APP_PATH).exists():
        profiles = enumerate_profiles(state_file)
        if profiles:
            for p in profiles:
                label = f'Chrome: {p["name"]} ({p["email"]}) [{p["directory"]}]'
                options.append({'label': label, 'browser': 'chrome', 'chrome_profile': p['directory']})
        else:
            options.append({'label': 'Chrome', 'browser': 'chrome', 'chrome_profile': None})

    return options


def browser_label(browser, chrome_profile=None):
    """Human-readable label for a (browser, chrome_profile) pair."""
    if browser == 'chrome':
        return f'Chrome: {chrome_profile}' if chrome_profile else 'Chrome'
    if browser and browser != 'default':
        return browser.title()
    return 'default browser'
