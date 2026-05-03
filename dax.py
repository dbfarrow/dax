#!/usr/bin/env python3

import os
import sys
from pathlib import Path


def _template_path():
    return Path(__file__).parent / 'Dockerfile.tmpl'


def render_dockerfile(user, shell, passwd):
    content = _template_path().read_text()
    content = content.replace('%%USER%%', user)
    content = content.replace('%%SHELL%%', shell)
    content = content.replace('%%PASSWD%%', passwd)
    return content
