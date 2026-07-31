import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dax import render_dockerfile

def test_render_dockerfile_substitutes_user():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'testuser' in content
    assert '%%USER%%' not in content

def test_render_dockerfile_substitutes_shell():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert '/bin/zsh' in content
    assert '%%SHELL%%' not in content

def test_render_dockerfile_substitutes_passwd():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'testuser:testpass' in content
    assert '%%PASSWD%%' not in content

def test_render_dockerfile_contains_key_tools():
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'nmap' in content
    assert 'tmux' in content
    assert 'claude' in content.lower() or 'claude-code' in content


def test_render_dockerfile_activates_the_generated_locale():
    """locale-gen only builds en_US.UTF-8's data; found 2026-07-31 that nothing
    ever selected it, so every shell defaulted to POSIX/C and `less` flagged a
    plain-text CLAUDE.md (ordinary em-dashes) as binary."""
    content = render_dockerfile('testuser', '/bin/zsh', 'testpass')
    assert 'locale-gen' in content
    assert 'ENV LANG=en_US.UTF-8' in content
    assert 'ENV LC_ALL=en_US.UTF-8' in content
