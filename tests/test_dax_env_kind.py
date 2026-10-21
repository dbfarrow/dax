"""What an env is, in `dax env list` (kind column) and `dax env show`: a code
project, or a substrate-backed process with the type its own context.md
records (new-process.sh writes `type:`/`skill:` frontmatter and a `# Title`).
dax keeps no copy of the type; the process's own file is the record.
"""
import pytest

from dax_creds.init import process_info, run_env_list, run_env_show

CONTEXT = """---
type: commitment
skill: discernment
---

# Should I take the job

*Private reference document.*
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setattr('dax_creds.init._is_container_running', lambda name: False)
    for d in ('code', 'proc', 'bare'):
        (tmp_path / d).mkdir()
    (tmp_path / 'proc' / 'context.md').write_text(CONTEXT)
    return tmp_path


def _config(home):
    return {'projects': {
        'code': {'dir': str(home / 'code'), 'tenant': 'personal', 'image': 'dax-base'},
        'proc': {'dir': str(home / 'proc'), 'tenant': 'personal', 'features': ['substrate']},
        'bare': {'dir': str(home / 'bare'), 'tenant': 'personal', 'features': ['substrate']},
    }}


def test_process_info_reads_frontmatter_and_title(home):
    assert process_info(home / 'proc') == {
        'type': 'commitment', 'skill': 'discernment', 'title': 'Should I take the job'}


@pytest.mark.parametrize('text', ['', 'no frontmatter\n', '---\ntype:\n---\n', '\x00\xff'])
def test_process_info_tolerates_odd_files(home, text):
    (home / 'bare' / 'context.md').write_text(text, encoding='latin-1')
    assert 'type' not in process_info(home / 'bare')


def test_process_info_without_context_md(home):
    assert process_info(home / 'code') == {}
    assert process_info(home / 'missing') == {}


def test_env_list_has_a_kind_column(home, capsys):
    run_env_list(_config(home))

    lines = capsys.readouterr().out.splitlines()
    head = next(l for l in lines if 'kind' in l and 'tenant' in l)
    name_at, kind_at = head.index('name'), head.index('kind')
    kinds = {l[name_at:].split()[0]: l[kind_at:head.index('dir')].strip()
             for l in lines if l.startswith('  [')}
    assert kinds == {'code': 'code', 'proc': 'commitment', 'bare': '?'}


def test_env_show_says_code_project(home, capsys):
    run_env_show(_config(home), 'code')
    assert '  kind     code project' in capsys.readouterr().out


def test_env_show_gives_process_type_skill_and_title(home, capsys):
    run_env_show(_config(home), 'proc')
    out = capsys.readouterr().out
    assert '  kind     substrate-backed process, type commitment (skill discernment)' in out
    assert '  title    Should I take the job' in out


def test_env_show_says_when_the_type_is_unknown(home, capsys):
    run_env_show(_config(home), 'bare')
    out = capsys.readouterr().out
    assert 'type unknown: no `type:` in' in out and 'context.md' in out
    assert '  title' not in out


def test_env_list_has_no_image_column(home, capsys):
    """Dropped at the user's request; `dax env show` still has it."""
    run_env_list(_config(home))
    out = capsys.readouterr().out
    head = next(l for l in out.splitlines() if 'kind' in l and 'tenant' in l)
    assert 'image' not in head
    assert 'dax-base' not in out
