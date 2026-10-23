"""Stored webpreview ports and the env index page
(docs/design/2026-10-23-webpreview-ports.md)."""
import pytest

from dax_creds import webpreview as wp


def _cfg(projects, base=None, tenants=None):
    cfg = {'projects': projects}
    if base is not None:
        cfg['defaults'] = {'webpreview_base_port': base}
    if tenants is not None:
        cfg['tenants'] = tenants
    return cfg


# --- hashing and the base ---------------------------------------------------------

def test_hashed_port_is_deterministic_and_inside_the_range():
    a = wp.hashed_port('/Users/me/proj')
    assert a == wp.hashed_port('/Users/me/proj')
    assert 8000 <= a < 9000


def test_hashed_port_follows_the_base():
    assert wp.hashed_port('/Users/me/proj', 20000) - 20000 == wp.hashed_port('/Users/me/proj') - 8000


def test_base_port_defaults_to_8000_and_reads_defaults():
    assert wp.base_port({}) == 8000
    assert wp.base_port({'defaults': {'webpreview_base_port': 12000}}) == 12000


# --- a new env's port -------------------------------------------------------------

def test_new_env_gets_its_hashed_port_when_free():
    assert wp.port_for_new_env(_cfg({}), '/Users/me/proj') == wp.hashed_port('/Users/me/proj')


def test_new_env_moves_up_past_ports_other_envs_hold():
    first = wp.hashed_port('/Users/me/new')
    taken = {'a': {'dir': '/x', 'webpreview': {'port': first}},
             'b': {'dir': '/y', 'webpreview': {'port': first + 1 if first < 8999 else 8000}}}
    got = wp.port_for_new_env(_cfg(taken), '/Users/me/new')
    assert got not in (first, taken['b']['webpreview']['port'])
    assert 8000 <= got < 9000


def test_new_env_wraps_around_the_top_of_the_range():
    # Find a folder that hashes to the very top of the range is impractical;
    # instead fill everything from its guess to the top and check the wrap.
    first = wp.hashed_port('/Users/me/wrap')
    taken = {'e{}'.format(p): {'dir': '/d{}'.format(p), 'webpreview': {'port': p}}
             for p in range(first, 9000)}
    assert wp.port_for_new_env(_cfg(taken), '/Users/me/wrap') == (8000 if first > 8000 else first)


# --- the assignment pass ------------------------------------------------------------

def test_pass_fills_every_env_without_a_port_and_keeps_them_unique():
    projects = {'a': {'dir': '/Users/me/a'}, 'b': {'dir': '/Users/me/b'},
                'c': {'dir': '/Users/me/c'}}
    changes = wp.plan_port_assignment(_cfg(projects))
    assert [c[0] for c in changes] == ['a', 'b', 'c']
    assert all(old is None for _, old, _ in changes)
    new = [n for _, _, n in changes]
    assert len(set(new)) == 3 and all(8000 <= p < 9000 for p in new)


def test_pass_is_a_no_op_when_nothing_changed():
    projects = {'a': {'dir': '/Users/me/a'}, 'b': {'dir': '/Users/me/b'}}
    cfg = _cfg(projects)
    for name, _, port in wp.plan_port_assignment(cfg):
        wp.set_port(projects[name], port)
    assert wp.plan_port_assignment(cfg) == []


def test_pass_leaves_in_range_ports_alone_even_if_not_the_hashed_guess():
    projects = {'a': {'dir': '/Users/me/a', 'webpreview': {'port': 8123}}}
    assert wp.plan_port_assignment(_cfg(projects)) == []


def test_pass_renumbers_envs_outside_a_changed_base():
    projects = {'a': {'dir': '/Users/me/a', 'webpreview': {'port': 8123}},
                'b': {'dir': '/Users/me/b'}}
    changes = wp.plan_port_assignment(_cfg(projects, base=20000))
    assert [(n, old) for n, old, _ in changes] == [('a', 8123), ('b', None)]
    assert all(20000 <= new < 21000 for _, _, new in changes)


def test_pass_resolves_a_duplicate_by_moving_the_later_env():
    projects = {'a': {'dir': '/Users/me/a', 'webpreview': {'port': 8500}},
                'b': {'dir': '/Users/me/b', 'webpreview': {'port': 8500}}}
    changes = wp.plan_port_assignment(_cfg(projects))
    assert len(changes) == 1 and changes[0][0] == 'b' and changes[0][2] != 8500


def test_set_port_keeps_other_webpreview_keys():
    entry = {'webpreview': {'port': 1, 'other': 'kept'}}
    wp.set_port(entry, 8200)
    assert entry['webpreview'] == {'port': 8200, 'other': 'kept'}


# --- the index page -----------------------------------------------------------------

def test_index_lists_every_env_grouped_by_tenant_in_tenants_order():
    cfg = _cfg({'zed': {'dir': '/z', 'tenant': 'personal', 'webpreview': {'port': 8001}},
                'alpha': {'dir': '/a', 'tenant': 'work', 'webpreview': {'port': 8002}},
                'beta': {'dir': '/b', 'tenant': 'personal', 'webpreview': {'port': 8003}}},
               tenants={'work': {}, 'personal': {}})
    page = wp.render_index(cfg)
    assert page.index('<h2>work</h2>') < page.index('<h2>personal</h2>')
    assert page.index('>beta<') < page.index('>zed<')
    assert 'href="http://localhost:8002/"' in page


def test_index_leaves_out_hidden_tenants():
    cfg = _cfg({'seen': {'dir': '/s', 'tenant': 'personal', 'webpreview': {'port': 8001}},
                'gone': {'dir': '/g', 'tenant': 'old', 'webpreview': {'port': 8002}}},
               tenants={'personal': {}, 'old': {'hidden': True}})
    page = wp.render_index(cfg)
    assert '>seen<' in page and '>gone<' not in page


def test_index_shows_an_env_without_a_port_and_says_what_to_run():
    page = wp.render_index(_cfg({'new': {'dir': '/n', 'tenant': 'personal'}}))
    assert '>new<' in page and 'dax env ports --assign' in page
    assert '<tr data-url' not in page


def test_index_checks_running_live_rather_than_baking_it_in():
    page = wp.render_index(_cfg({'a': {'dir': '/a', 'tenant': 't', 'webpreview': {'port': 8004}}}))
    assert 'data-url="http://localhost:8004/"' in page
    assert "mode: 'no-cors'" in page


def test_index_escapes_names_and_paths():
    page = wp.render_index(_cfg({'<b>': {'dir': '/a&b', 'tenant': 't', 'webpreview': {'port': 8005}}}))
    assert '&lt;b&gt;' in page and '/a&amp;b' in page


def test_write_index_writes_under_local_state(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = wp.write_index(_cfg({}))
    assert path == tmp_path / '.local' / 'state' / 'dax' / 'envs.html'
    assert path.read_text().startswith('<!doctype html>')
