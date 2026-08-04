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
