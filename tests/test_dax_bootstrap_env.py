"""`_bootstrap_env`: the wizard `dax run` starts in an unregistered folder,
replacing `dax init` and `dax process new` (env-lifecycle consolidation,
decisions 4-8, 25, 35, 37).

What's under test:
- the env is named after the folder; nothing comes from flags;
- fixed inputs are fatal when wrong (cwd outside $HOME, nested in or
  enclosing a registered env, a name already registered, leftover state),
  while prompted answers re-ask;
- tenant is always asked; only existing non-Claude credentials are offered,
  with no way to define one here;
- a substrate-backed process runs new-process.sh only after the confirm;
- declining or cancelling registers nothing.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

import dax
from dax import _bootstrap_env, _prompt_tenant, _read_process_types
from dax_creds.config import load_dax_config, state_tree_path


TYPES_TSV = """\
# comment line, ignored
commitment\tdiscernment\tCareer, role, or partnership decisions
ops\tops\tStructural or operational setup for a new way of working
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.dax.yaml').write_text(
        'defaults:\n  image: dax-base\n'
        'credentials:\n'
        '  github-me: {provider: github}\n'
        '  ssh-me: {provider: ssh}\n'
        '  claude-old: {provider: claude}\n'
        'projects: {}\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr('dax._known_tenant_names', lambda: {'acme', 'personal'})
    return home


@pytest.fixture
def substrate(tmp_path):
    root = tmp_path / 'virgil'
    (root / 'shared').mkdir(parents=True)
    (root / 'shared' / 'process-types.tsv').write_text(TYPES_TSV)
    # Stands in for the real new-process.sh: records its arguments.
    script = root / 'shared' / 'new-process.sh'
    script.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$(dirname "$0")/../../last-call.txt"\n')
    script.chmod(0o755)
    return root


class Script:
    """Scripted answers for each kind of prompt, consumed in order, plus a
    record of what each prompt offered."""

    def __init__(self, monkeypatch, tenant='acme', text=(), select=(), confirm=(),
                 path=(), checkbox=None):
        self.text = list(text)
        self.select = list(select)
        self.confirm = list(confirm)
        self.path = list(path)
        self.checkbox = checkbox
        self.offered = {}
        self.path_defaults = []

        def _text(prompt, default=None):
            return self.text.pop(0) if self.text else default
        monkeypatch.setattr('dax_creds.init._q_text', _text)

        def _select(prompt, choices, default=None):
            self.offered[prompt] = [c.value for c in choices]
            answer = self.select.pop(0)
            return answer
        monkeypatch.setattr('dax_creds.init._q_select', _select)

        monkeypatch.setattr('dax_creds.init._q_confirm',
                            lambda prompt, default=False: self.confirm.pop(0))

        def _path(prompt, default=None):
            self.path_defaults.append(default)
            return self.path.pop(0)
        monkeypatch.setattr('dax_creds.init._q_path', _path)

        def _checkbox(prompt, choices):
            self.offered['credentials'] = [c.title for c in choices]
            return self.checkbox or []
        monkeypatch.setattr('dax_creds.init._q_checkbox', _checkbox)

        monkeypatch.setattr('dax._prompt_tenant', lambda subdir, known, **k: tenant)


def _env_dir(home, name='demo'):
    d = home / name
    d.mkdir(exist_ok=True)
    return d.resolve()


def _last_call(tmp_path):
    return (tmp_path / 'last-call.txt').read_text().splitlines()


# --- coding projects -------------------------------------------------------

def test_coding_project_registers_the_folder(home, monkeypatch):
    env_dir = _env_dir(home)
    Script(monkeypatch, select=['coding'], confirm=[True])

    name, entry = _bootstrap_env(load_dax_config(), env_dir)

    assert name == 'demo'
    saved = load_dax_config()['projects']['demo']
    assert saved['dir'] == str(env_dir)
    assert saved['image'] == 'dax-base'
    assert saved['tenant'] == 'acme'
    assert list(saved['creds']) == ['claude']
    assert list(saved['features']) == ['claude_tenant_state']
    assert entry['dir'] == str(env_dir)


def test_the_wizard_opens_by_naming_the_env_after_its_folder(home, monkeypatch, capsys):
    env_dir = _env_dir(home, 'my-proj')
    Script(monkeypatch, select=['coding'], confirm=[True])

    _bootstrap_env(load_dax_config(), env_dir)

    out = capsys.readouterr().out
    assert "This env will be named 'my-proj', after its folder." in out
    assert out.index("named 'my-proj'") < out.index('field')  # before the summary


def test_the_summary_is_a_table(home, monkeypatch, capsys):
    env_dir = _env_dir(home)
    Script(monkeypatch, select=['coding'], confirm=[True])

    _bootstrap_env(load_dax_config(), env_dir)

    lines = capsys.readouterr().out.splitlines()
    header = next(i for i, l in enumerate(lines) if l.startswith('field'))
    assert set(lines[header + 1].replace(' ', '')) == {'-'}
    assert any(l.startswith('tenant') and 'acme' in l for l in lines[header:])


def test_only_existing_non_claude_credentials_are_offered(home, monkeypatch):
    env_dir = _env_dir(home)
    script = Script(monkeypatch, select=['coding'], confirm=[True], checkbox=['github-me'])

    _bootstrap_env(load_dax_config(), env_dir)

    assert script.offered['credentials'] == ['github-me', 'ssh-me']
    assert list(load_dax_config()['projects']['demo']['creds']) == ['github-me', 'claude']


def test_no_credentials_means_no_checkbox(home, monkeypatch):
    (home / '.dax.yaml').write_text('projects: {}\n')
    env_dir = _env_dir(home)
    script = Script(monkeypatch, select=['coding'], confirm=[True])

    _bootstrap_env(load_dax_config(), env_dir)

    assert 'credentials' not in script.offered


def test_the_image_answer_is_used(home, monkeypatch):
    env_dir = _env_dir(home)
    Script(monkeypatch, text=['my/image'], select=['coding'], confirm=[True])

    _bootstrap_env(load_dax_config(), env_dir)

    assert load_dax_config()['projects']['demo']['image'] == 'my/image'


def test_the_registry_is_saved_once(home, monkeypatch):
    env_dir = _env_dir(home)
    Script(monkeypatch, select=['coding'], confirm=[True])
    from dax_creds import init
    real = init.save_config
    calls = []
    monkeypatch.setattr('dax_creds.init.save_config', lambda c: calls.append(1) or real(c))

    _bootstrap_env(load_dax_config(), env_dir)

    assert len(calls) == 1


# --- substrate-backed processes --------------------------------------------

def test_process_runs_new_process_sh_after_confirm_and_registers(home, substrate, tmp_path, monkeypatch):
    env_dir = _env_dir(home, 'proc')
    script = Script(monkeypatch, text=[None, 'My process'],
                    select=['process', ('ops', 'ops')], confirm=[False, True],
                    path=[str(substrate)])

    _bootstrap_env(load_dax_config(), env_dir)

    assert _last_call(tmp_path) == ['--dir', str(env_dir), '--title', 'My process',
                                    '--type', 'ops', '--no-git']
    saved = load_dax_config()['projects']['proc']
    assert saved['substrate'] == str(substrate.resolve())
    assert list(saved['features']) == ['substrate', 'claude_tenant_state']
    assert script.offered['Process type:'] == [('commitment', 'discernment'), ('ops', 'ops')]


def test_process_git_yes_passes_git(home, substrate, tmp_path, monkeypatch):
    env_dir = _env_dir(home, 'proc')
    Script(monkeypatch, text=[None, 'T'], select=['process', ('ops', 'ops')],
           confirm=[True, True], path=[str(substrate)])

    _bootstrap_env(load_dax_config(), env_dir)

    assert _last_call(tmp_path)[-1] == '--git'


def test_a_bad_substrate_path_re_asks(home, substrate, tmp_path, monkeypatch, capsys):
    env_dir = _env_dir(home, 'proc')
    not_a_substrate = tmp_path / 'nope'
    not_a_substrate.mkdir()
    Script(monkeypatch, text=[None, 'T'], select=['process', ('ops', 'ops')],
           confirm=[False, True], path=[str(not_a_substrate), str(substrate)])

    _bootstrap_env(load_dax_config(), env_dir)

    assert 'is {} a substrate repo?'.format(not_a_substrate.resolve()) in capsys.readouterr().out
    assert 'proc' in load_dax_config()['projects']


def test_a_blank_title_re_asks(home, substrate, tmp_path, monkeypatch, capsys):
    env_dir = _env_dir(home, 'proc')
    Script(monkeypatch, text=[None, '', 'Real title'], select=['process', ('ops', 'ops')],
           confirm=[False, True], path=[str(substrate)])

    _bootstrap_env(load_dax_config(), env_dir)

    assert 'a process title is required' in capsys.readouterr().out
    assert '--title' in _last_call(tmp_path) and 'Real title' in _last_call(tmp_path)


def test_the_substrate_prompt_defaults_to_the_top_level_setting(home, substrate, monkeypatch):
    config_text = (home / '.dax.yaml').read_text()
    (home / '.dax.yaml').write_text('substrate: {}\n'.format(substrate) + config_text)
    env_dir = _env_dir(home, 'proc')
    script = Script(monkeypatch, text=[None, 'T'], select=['process', ('ops', 'ops')],
                    confirm=[False, True], path=[str(substrate)])

    _bootstrap_env(load_dax_config(), env_dir)

    assert script.path_defaults == [str(substrate)]


def test_declining_a_process_never_runs_new_process_sh(home, substrate, tmp_path, monkeypatch):
    env_dir = _env_dir(home, 'proc')
    Script(monkeypatch, text=[None, 'T'], select=['process', ('ops', 'ops')],
           confirm=[False, False], path=[str(substrate)])

    assert _bootstrap_env(load_dax_config(), env_dir) is None

    assert not (tmp_path / 'last-call.txt').exists()
    assert list(env_dir.iterdir()) == []
    assert 'proc' not in load_dax_config()['projects']


def test_a_partial_new_process_sh_failure_names_what_it_left(home, substrate, monkeypatch, capsys):
    (substrate / 'shared' / 'new-process.sh').write_text(
        '#!/bin/sh\nwhile [ "$1" != --dir ]; do shift; done\n'
        'touch "$2/context.md" "$2/inbox.md"\nexit 2\n')
    env_dir = _env_dir(home, 'proc')
    Script(monkeypatch, text=[None, 'T'], select=['process', ('ops', 'ops')],
           confirm=[False, True], path=[str(substrate)])

    with pytest.raises(SystemExit):
        _bootstrap_env(load_dax_config(), env_dir)

    out = capsys.readouterr().out
    assert 'It left these in {}: context.md, inbox.md'.format(env_dir) in out
    assert 'Remove them, then run `dax run` here again.' in out
    assert (env_dir / 'context.md').exists()  # reported, never deleted
    assert 'proc' not in load_dax_config()['projects']


def test_registered_tenants_are_offered(home, monkeypatch):
    """Envs made by the wizard record their tenant only in ~/.dax.yaml, so
    the pick-list has to read it there, not only from .dax-tenant files."""
    monkeypatch.undo()  # drop the fixture's _known_tenant_names stub
    monkeypatch.setenv('HOME', str(home))
    from dax_creds.init import save_config
    config = load_dax_config()
    config['projects']['a'] = {'dir': str(home / 'gone-a'), 'tenant': 'ysecurity'}
    config['projects']['b'] = {'dir': str(home / 'gone-b'), 'tenant': 'acme'}
    save_config(config)

    assert dax._known_tenant_names() >= {'ysecurity', 'acme'}


def test_a_failing_new_process_sh_registers_nothing(home, substrate, monkeypatch, capsys):
    (substrate / 'shared' / 'new-process.sh').write_text('#!/bin/sh\nexit 3\n')
    env_dir = _env_dir(home, 'proc')
    Script(monkeypatch, text=[None, 'T'], select=['process', ('ops', 'ops')],
           confirm=[False, True], path=[str(substrate)])

    with pytest.raises(SystemExit) as excinfo:
        _bootstrap_env(load_dax_config(), env_dir)

    assert excinfo.value.code == 1
    assert 'new-process.sh failed (exit 3)' in capsys.readouterr().out
    assert 'proc' not in load_dax_config()['projects']


# --- declining and cancelling ----------------------------------------------

def test_declining_registers_nothing(home, monkeypatch, capsys):
    env_dir = _env_dir(home)
    Script(monkeypatch, select=['coding'], confirm=[False])

    assert _bootstrap_env(load_dax_config(), env_dir) is None

    assert 'nothing created or registered' in capsys.readouterr().out
    assert 'demo' not in load_dax_config()['projects']


def test_ctrl_c_registers_nothing(home, monkeypatch, capsys):
    env_dir = _env_dir(home)
    Script(monkeypatch)

    def _cancel(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr('dax_creds.init._q_select', _cancel)

    assert _bootstrap_env(load_dax_config(), env_dir) is None
    assert 'demo' not in load_dax_config()['projects']


# --- fatal guardrails: nothing asked, nothing written ----------------------

def _assert_fatal(home, monkeypatch, env_dir, capsys, expect):
    before = (home / '.dax.yaml').read_text()
    Script(monkeypatch)  # any prompt reached would pop an empty list and fail

    with pytest.raises(SystemExit) as excinfo:
        _bootstrap_env(load_dax_config(), env_dir)

    assert excinfo.value.code == 1
    assert expect in capsys.readouterr().out
    assert (home / '.dax.yaml').read_text() == before


def _register(home, name, env_dir):
    from dax_creds.init import save_config
    config = load_dax_config()
    config['projects'][name] = {'dir': str(env_dir), 'creds': []}
    save_config(config)


def test_outside_home_is_fatal(home, monkeypatch, capsys, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    _assert_fatal(home, monkeypatch, outside.resolve(), capsys, 'not under your home directory')


def test_inside_a_registered_env_is_fatal(home, monkeypatch, capsys):
    parent = _env_dir(home, 'parent')
    _register(home, 'parent', parent)
    sub = parent / 'sub'
    sub.mkdir()
    _assert_fatal(home, monkeypatch, sub, capsys, "inside already-registered project 'parent'")


def test_enclosing_a_registered_env_is_fatal(home, monkeypatch, capsys):
    outer = _env_dir(home, 'outer')
    inner = outer / 'inner'
    inner.mkdir()
    _register(home, 'inner', inner)
    _assert_fatal(home, monkeypatch, outer, capsys, "would enclose already-registered project 'inner'")


def test_a_name_registered_elsewhere_is_fatal(home, monkeypatch, capsys):
    """Decision 25: register_project used to merge into the existing entry,
    silently repointing the other env at this folder."""
    _register(home, 'demo', _env_dir(home, 'demo'))
    elsewhere = home / 'work' / 'demo'
    elsewhere.mkdir(parents=True)
    _assert_fatal(home, monkeypatch, elsewhere.resolve(), capsys,
                  "an env named 'demo' is already registered")


def test_leftover_state_under_the_name_is_fatal(home, monkeypatch, capsys):
    env_dir = _env_dir(home)
    leftover = state_tree_path('acme', 'demo')
    leftover.mkdir(parents=True)
    before = (home / '.dax.yaml').read_text()
    Script(monkeypatch)  # tenant 'acme' is asked; nothing after it

    with pytest.raises(SystemExit):
        _bootstrap_env(load_dax_config(), env_dir)

    assert 'leftover Claude state' in capsys.readouterr().out
    assert (home / '.dax.yaml').read_text() == before


# --- helpers -----------------------------------------------------------------

def test_a_blank_new_tenant_re_asks(tmp_path, capsys):
    answers = iter(['   ', 'newco'])

    tenant = _prompt_tenant(tmp_path, set(), _text=lambda p, d: next(answers))

    assert tenant == 'newco'
    assert 'a tenant is required' in capsys.readouterr().out


def test_read_process_types_ignores_comments(substrate):
    assert _read_process_types(substrate) == [
        ('commitment', 'discernment', 'Career, role, or partnership decisions'),
        ('ops', 'ops', 'Structural or operational setup for a new way of working'),
    ]


def test_read_process_types_errors_when_tsv_missing(tmp_path):
    with pytest.raises(ValueError):
        _read_process_types(tmp_path)


# --- decision 37 and the CLI surface -----------------------------------------

def _cli(home, *argv):
    repo = Path(__file__).resolve().parent.parent
    env = dict(os.environ, HOME=str(home))
    return subprocess.run([sys.executable, str(repo / 'dax.py'), *argv],
                          capture_output=True, text=True, env=env, cwd=str(home))


@pytest.mark.parametrize('argv', [
    ('-t', 'run'), ('env', 'restore'), ('envs', 'list'), ('creds', 'list'), ('backup',),
])
def test_a_missing_config_exits_with_instructions_and_writes_nothing(tmp_path, argv):
    home = tmp_path / 'empty-home'
    home.mkdir()

    result = _cli(home, *argv)

    assert result.returncode == 1, result.stdout + result.stderr
    assert 'never creates one itself' in result.stdout
    assert 'dax init' not in result.stdout + result.stderr
    assert not (home / '.dax.yaml').exists()


@pytest.mark.parametrize('sub', ['init', 'process'])
def test_init_and_process_are_gone(tmp_path, sub):
    home = tmp_path / 'h'
    home.mkdir()
    result = _cli(home, sub)
    assert result.returncode == 2
    assert "invalid choice: '{}'".format(sub) in result.stderr
