"""`dax config`: edit ~/.dax.yaml in an editor, keeping the previous
version, then check it (env-lifecycle consolidation, decision 38).

The editor is replaced by a scripted stand-in that rewrites the file, so
each test controls exactly what the "user" typed.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest

import dax


GOOD = """\
# my config
projects:
  fabric:
    dir: /src/fabric
    tenant: personal
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.delenv('EDITOR', raising=False)
    monkeypatch.setattr(dax, '_in_dax_container', lambda: False)
    (tmp_path / '.dax.yaml').write_text(GOOD)
    return tmp_path


def _editor(monkeypatch, *edits):
    """Each editor run applies the next edit (a function of the old text);
    returns the list of editor commands run."""
    edits = list(edits)
    runs = []

    def fake_run(cmd, *a, **k):
        runs.append(cmd)
        path = Path(cmd[-1])
        path.write_text(edits.pop(0)(path.read_text()))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(dax.subprocess, 'run', fake_run)
    return runs


def _run():
    dax.cmd_config(argparse.Namespace())


# --- which editor ----------------------------------------------------------

def test_visual_wins_over_editor(monkeypatch):
    monkeypatch.setenv('VISUAL', 'nano')
    monkeypatch.setenv('EDITOR', 'emacs')
    assert dax._editor_command() == ['nano']


def test_editor_is_split_so_arguments_work(monkeypatch):
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.setenv('EDITOR', 'code -w')
    assert dax._editor_command() == ['code', '-w']


def test_vi_when_neither_is_set(monkeypatch):
    monkeypatch.delenv('VISUAL', raising=False)
    monkeypatch.delenv('EDITOR', raising=False)
    assert dax._editor_command() == ['vi']


def test_the_editor_is_run_on_the_config_file(home, monkeypatch):
    monkeypatch.setenv('EDITOR', 'code -w')
    runs = _editor(monkeypatch, lambda t: t)

    _run()

    assert runs == [['code', '-w', str(home / '.dax.yaml')]]


# --- the .prev copy ----------------------------------------------------------

def test_the_previous_version_is_kept(home, monkeypatch):
    _editor(monkeypatch, lambda t: t.replace('personal', 'acme'))
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: False)  # tenant move

    _run()

    assert (home / '.dax.yaml.prev').read_text() == GOOD
    assert 'acme' in (home / '.dax.yaml').read_text()


def test_no_change_says_so_and_keeps_the_older_prev(home, monkeypatch, capsys):
    """Opening the file just to look must not cost the undo copy."""
    (home / '.dax.yaml.prev').write_text('an older good version\n')
    _editor(monkeypatch, lambda t: t)

    _run()

    assert 'no changes' in capsys.readouterr().out
    assert (home / '.dax.yaml.prev').read_text() == 'an older good version\n'


@pytest.mark.parametrize('broken', ['', 'projects: [fabric]\n',
                                    'projects:\n  fabric: /src/fabric\n', '- just a list\n'])
def test_a_file_that_parses_but_has_the_wrong_shape_is_a_problem_too(
        home, monkeypatch, capsys, broken):
    """Empty, a list of projects, or an env as a plain value: every dax
    command would crash on these, so they get the same reopen offer."""
    runs = _editor(monkeypatch, lambda t: broken, lambda t: GOOD)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    _run()

    assert len(runs) == 2
    assert 'has a problem' in capsys.readouterr().out
    assert (home / '.dax.yaml').read_text() == GOOD


def test_a_missing_editor_is_reported_not_a_traceback(home, monkeypatch, capsys):
    monkeypatch.setenv('EDITOR', 'no-such-editor-xyz')

    def missing(cmd, *a, **k):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(dax.subprocess, 'run', missing)

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    assert "editor 'no-such-editor-xyz' not found" in capsys.readouterr().out
    assert not (home / '.dax.yaml.prev').exists()


# --- checking the result ---------------------------------------------------------

def test_a_parse_error_reopens_the_editor_until_fixed(home, monkeypatch, capsys):
    runs = _editor(monkeypatch,
                   lambda t: t.replace('tenant: personal', 'tenant: [unclosed'),
                   lambda t: GOOD)
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: True)

    _run()

    assert len(runs) == 2
    out = capsys.readouterr().out
    assert 'no longer parses' in out
    assert (home / '.dax.yaml').read_text() == GOOD


def test_a_duplicate_key_is_caught(home, monkeypatch, capsys):
    """The runbook nearly produced one: a second `creds:` line."""
    _editor(monkeypatch, lambda t: t + '    tenant: other\n')
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: False)

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    assert 'no longer parses' in capsys.readouterr().out


def test_declining_to_fix_leaves_it_and_points_at_prev(home, monkeypatch, capsys):
    _editor(monkeypatch, lambda t: 'projects: [\n')
    monkeypatch.setattr('dax_creds.init._q_confirm', lambda *a, **k: False)

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert 'the version from before this edit is at' in out and '.dax.yaml.prev' in out
    assert (home / '.dax.yaml.prev').read_text() == GOOD


def test_unknown_keys_are_reported_after_saving(home, monkeypatch, capsys):
    _editor(monkeypatch, lambda t: t + '    tennant: x\n')

    _run()

    assert "env 'fabric' has unknown key(s) tennant" in capsys.readouterr().out


def test_old_claude_entries_are_reported_after_saving(home, monkeypatch, capsys):
    _editor(monkeypatch, lambda t: t + '    features: [claude_tenant_state]\n')

    _run()

    assert "env 'fabric' still lists claude_tenant_state (features)" in capsys.readouterr().out


def test_inside_a_container_it_refuses_before_touching_anything(home, monkeypatch, capsys):
    """A write-then-rename save can silently detach the container's copy of
    a single-file bind mount from the host file."""
    monkeypatch.setattr(dax, '_in_dax_container', lambda: True)
    ran = []
    monkeypatch.setattr(dax.subprocess, 'run', lambda *a, **k: ran.append(a))

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    assert 'only runs on the host' in capsys.readouterr().out
    assert ran == []
    assert not (home / '.dax.yaml.prev').exists()


def test_container_detection_uses_the_dax_image_marker(monkeypatch):
    monkeypatch.setattr(dax.os.path, 'exists', lambda p: p == '/usr/local/bin/dax-entrypoint.sh')
    assert dax._in_dax_container() is True
    monkeypatch.setattr(dax.os.path, 'exists', lambda p: False)
    assert dax._in_dax_container() is False


# --- missing config, and the CLI ----------------------------------------------

def test_a_missing_config_is_never_created(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr(dax, '_in_dax_container', lambda: False)
    ran = []
    monkeypatch.setattr(dax.subprocess, 'run', lambda *a, **k: ran.append(a))

    with pytest.raises(SystemExit) as excinfo:
        _run()

    assert excinfo.value.code == 1
    assert 'never creates one itself' in capsys.readouterr().out
    assert ran == []
    assert not (tmp_path / '.dax.yaml').exists()


def test_dax_config_is_a_top_level_command(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    result = subprocess.run([sys.executable, str(repo / 'dax.py'), '--help'],
                            capture_output=True, text=True,
                            env=dict(os.environ, HOME=str(tmp_path)))
    assert 'config' in result.stdout.split('{', 1)[1].split('}', 1)[0]
