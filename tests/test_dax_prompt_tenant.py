"""`_prompt_tenant` and `_known_tenant_names`: what makes tenant a deliberate,
required choice in the bootstrap wizard and in restore (env-lifecycle
consolidation, decisions 8 and 35).

Moved here from test_dax_tenant_cmds.py, deleted with `dax tenant`/`dax
tenants` and the rest of the multi-tenant model in step 8. `_prompt_tenant`
survives and `_known_tenant_names` was rewritten, so their tests do too.
"""
import pytest

from dax import _NEW_TENANT_CHOICE, _known_tenant_names, _prompt_tenant


def _write_config(home, text):
    (home / '.dax.yaml').write_text(text)


def test_known_tenant_names_empty_when_no_dax_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    assert _known_tenant_names() == set()


def test_known_tenant_names_comes_from_every_envs_tenant_field(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    _write_config(tmp_path, (
        'projects:\n'
        '  a: {dir: /nowhere/a, tenant: personal}\n'
        '  b: {dir: /nowhere/b, tenant: ysecurity}\n'
        '  c: {dir: /nowhere/c, tenant: personal}\n'
        '  d: {dir: /nowhere/d}\n'))

    # Folders needn't exist: the registry alone is the source now.
    assert _known_tenant_names() == {'personal', 'ysecurity'}


def test_legacy_dax_tenant_files_are_no_longer_read(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    repo = tmp_path / 'repo'
    repo.mkdir()
    (repo / '.dax-tenant').write_text('fromfile')
    _write_config(tmp_path, 'projects:\n  repo: {dir: %s, tenant: personal}\n' % repo)

    assert _known_tenant_names() == {'personal'}


def test_prompt_tenant_picks_from_known_list(tmp_path):
    picked = []

    def fake_select(prompt, choices, default):
        picked.append((prompt, choices, default))
        return 'ysecurity'

    result = _prompt_tenant(tmp_path / 'sub', {'ysecurity', 'personal'}, _select=fake_select)

    assert result == 'ysecurity'
    prompt, choices, default = picked[0]
    assert set(choices) == {'ysecurity', 'personal', _NEW_TENANT_CHOICE}
    assert default is None


def test_prompt_tenant_new_tenant_choice_falls_through_to_text_prompt(tmp_path):
    text_calls = []

    def fake_text(prompt, default):
        text_calls.append((prompt, default))
        return 'brand-new-tenant'

    result = _prompt_tenant(
        tmp_path / 'sub', {'ysecurity'},
        _select=lambda p, c, d: _NEW_TENANT_CHOICE, _text=fake_text)

    assert result == 'brand-new-tenant'
    assert len(text_calls) == 1


def test_prompt_tenant_uses_text_prompt_when_nothing_known_yet(tmp_path):
    result = _prompt_tenant(tmp_path / 'sub', set(), _text=lambda p, d: 'first-tenant')

    assert result == 'first-tenant'


def test_prompt_tenant_passes_current_value_as_select_default(tmp_path):
    captured = {}

    def fake_select(prompt, choices, default):
        captured['default'] = default
        return default

    _prompt_tenant(tmp_path / 'sub', {'ysecurity', 'personal'}, default='personal',
                    _select=fake_select)

    assert captured['default'] == 'personal'


def test_prompt_tenant_raises_on_cancelled_select(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        _prompt_tenant(tmp_path / 'sub', {'ysecurity'}, _select=lambda p, c, d: None)


def test_prompt_tenant_raises_on_cancelled_text(tmp_path):
    """A blank answer re-asks now (decision 8; see test_dax_bootstrap_env.py);
    only an explicit cancel stops."""
    with pytest.raises(KeyboardInterrupt):
        _prompt_tenant(tmp_path / 'sub', set(), _text=lambda p, d: None)


# --- _ensure_tenants_classified ----------------------------------------------
