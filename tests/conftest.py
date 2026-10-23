"""Test-wide guards.

`HOME` is redirected for every test, because a dax container bind-mounts the real
`~/.dax.yaml` **rw** — so a test that writes it does not corrupt a copy, it
corrupts the developer's actual configuration on the host, including credential
`client_id`/`client_secret` values that exist nowhere else.

That happened on 2026-07-31: four tests called `run_env_set` with a synthetic
config dict and no `HOME` isolation. `run_env_set` calls `save_config`
unconditionally, so the suite overwrote the real config with fixture data
(`dir: /repos/fabric`, `features: [anything]`), destroying four env definitions
and eight credentials. Recovery depended on a backup that happened to exist.

Redirecting by default rather than asking each test to remember is the fix: a
test that genuinely wants a populated home writes one under the redirected path,
which is what the existing per-module `home` fixtures already do (their own
`monkeypatch.setenv` simply wins over this one).
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp('home')
    monkeypatch.setenv('HOME', str(home))
    return home


def pytest_configure(config):
    config.addinivalue_line(
        'markers',
        'real_webpreview_port: let feature_webpreview read the stored port and check '
        'the host for real, instead of the default stand-in (see '
        '_webpreview_port_stand_in)')
    config.addinivalue_line(
        'markers',
        'real_claude_login: let `dax run` look up and refresh the implied Claude '
        'credential for real, instead of the default stand-in (see '
        '_implied_claude_credential_is_logged_in)')


@pytest.fixture(autouse=True)
def _implied_claude_credential_is_logged_in(request, monkeypatch):
    """Every env has an implied Claude credential since Claude became a
    baseline feature (decision 35). In a test, Keychain is empty, so
    `dax run` would stop at "not in Keychain, log in now?" and, on a fresh
    name, start the real login flow, in every test that runs `cmd_run` for
    reasons unrelated to credentials.

    So by default the Claude credential counts as stored and recently logged
    in. Tests about that path itself (missing credentials, relogin) opt out
    with `@pytest.mark.real_claude_login`.
    """
    if request.node.get_closest_marker('real_claude_login'):
        return
    import dax_creds.config as config_mod
    import dax_creds.init as init_mod

    real_ensure = init_mod.ensure_project_credentials

    def _ensure(config, project_creds, keyring=None):
        missing = real_ensure(config, project_creds, keyring=keyring)
        return [n for n in missing if (project_creds.get(n) or {}).get('provider') != 'claude']

    monkeypatch.setattr(init_mod, 'ensure_project_credentials', _ensure)
    monkeypatch.setattr(config_mod, 'relogin_due', lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _no_image_version_lookup(monkeypatch):
    """`dax run` asks docker for the image's `dax.version` label (decision
    42). Tests never have a real image, and some stub `subprocess.run` for
    their own purposes, so the lookup reports "can't tell" and stays silent.
    test_dax_version.py tests the real lookup directly."""
    import dax
    monkeypatch.setattr(dax, '_image_version', lambda image: None)


@pytest.fixture(autouse=True)
def _webpreview_port_stand_in(request, monkeypatch):
    """Every env now needs a stored webpreview port to launch, and dax refuses
    a busy one. Most tests build env entries without a port and must not
    depend on which host ports happen to be free, so by default an env with no
    stored port launches on 8000 and every port counts as free. Tests of the
    real behaviour opt out with `@pytest.mark.real_webpreview_port`."""
    if request.node.get_closest_marker('real_webpreview_port'):
        return
    import dax
    real = dax._stored_preview_port
    monkeypatch.setattr(dax, '_stored_preview_port', lambda config: real(config) or 8000)
    monkeypatch.setattr(dax, '_is_port_free', lambda port: True)


@pytest.fixture
def launch_without_docker(monkeypatch):
    """Lets a test run `cmd_run` for real (not -t) without docker: the
    `docker run` is recorded instead of executed, and nothing reaches
    Keychain. Needed where -t is the wrong tool: under -t, `dax run` never
    starts the bootstrap wizard and never touches a state tree. Returns the
    list of commands `subprocess.run` was given."""
    import dax
    ran = []
    monkeypatch.setattr(dax.subprocess, 'run', lambda cmd, *a, **k: ran.append(cmd))
    monkeypatch.setattr(dax, '_keyring_importable', lambda: False)
    monkeypatch.setattr(dax, '_sync_and_clear_claude_credential', lambda *a, **k: None)
    return ran
