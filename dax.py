#!/usr/bin/env python3

import os
import sys
from pathlib import Path
import yaml
from util import dax_print


def _template_path():
    return Path(__file__).parent / 'Dockerfile.tmpl'


def render_dockerfile(user, shell, passwd):
    content = _template_path().read_text()
    content = content.replace('%%USER%%', user)
    content = content.replace('%%SHELL%%', shell)
    content = content.replace('%%PASSWD%%', passwd)
    return content


def load_config():
    home = os.environ['HOME']
    cwd = os.getcwd()

    if not cwd.startswith(home):
        dax_print("[!] dax must be run from somewhere under your home dir")
        sys.exit(-1)

    with open(os.path.join(home, '.dax.yaml'), 'r') as f:
        defaults = yaml.safe_load(f)

    defaults['cwd'] = cwd

    dax_print("[+] looking for config file in {}".format(cwd))
    local_cfg_path = os.path.join(cwd, '.dax.yaml')
    local = {}
    if os.path.isfile(local_cfg_path):
        dax_print("[-]   config file is in {}".format(local_cfg_path))
        with open(local_cfg_path, 'r') as f:
            local = yaml.safe_load(f) or {}

    defaults['cfgdir'] = cwd
    defaults['envname'] = cwd.replace(home, '').lstrip('/').replace('/', '-')

    local_features = local.pop('features', [])
    defaults.update(local)
    defaults['features'].extend(local_features)

    return defaults
