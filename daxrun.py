#!/usr/bin/env python3
#
# script to instantiate a DAX container tailored for a specific environment
#

import argparse
import os
import os.path
import socket
import subprocess
import sys
import json
from util import dax_print

def load_config():
    
    # load the system defaults from $HOME/.dax.yaml. This file contains
    # the base image and features to use by default. It also defines
    # the default parameters for other builtin features
    with open("{}/.dax.json".format(os.environ['HOME']), 'r') as deffile:
        defaults = json.load(deffile)
        defaults['envname'] = 'DAX'

    # Dax will launch the specified container with the current directory
    # mapped to $HOME/work in the container. However, the current directory
    # must be under your home dir. If it's not then dax maps /tmp to the container 
    # work directory instead    
    home = os.environ['HOME']
    cwd = os.getcwd()
    if (not cwd.startswith(home)):
        dax_print("[!] dax must be run from somewhere under your home dir")
        exit(-1)
    defaults['cwd'] = cwd

    # You can supply additional configuration information for the contianer
    # by putting a .dax.yaml file in the directory that daxrun.py will be
    # run from. The settings in the current directory will override the 
    # settings from the global config file  
    dax_print("[+] looking for config file in {}".format(cwd))
    cfgfile = "{}/.dax.yaml".format(cwd)
    config = {}
    if os.path.isfile(cfgfile):
        dax_print("[-]   config file is in {}".format(cfgfile))
        with open(cfgfile, 'r') as ymlfile:
            config = yaml.load(ymlfile)

    config['envname'] = cwd.replace(home, "").replace("/", "", 1).replace("/", "-")
    features = None
    if 'features' in config:
        features = config['features']
        del config['features']
        dax_print("[-]   subdir has features")
    
    # merge the defaults and the environment config together # and return it
    defaults['cfgdir'] = cwd
    defaults.update(config)
    if features:
        defaults['features'].extend(features)

    return defaults
    
def add_feature(feature, config):
    
    try:
        dax_print("[+] adding {}".format(feature))
        return globals()["feature_"+feature](config)
    except KeyError as ex:
        dax_print("[!] unknown feature: {}".format(feature))
        print_features()
        exit(-1)

def print_features():
    
    dax_print("[!] {} supports the following features:".format(__file__))
    funcs = globals()
    for func in funcs.keys():
        if (func.startswith("feature_")):
            feature = func.replace("feature_", "")
            dax_print("\t{}".format(feature))

def feature_optdir(config):
    return add_volume(config, 'optdir')

def feature_msf(config):
    opts = add_volume(config, 'msf')
    opts.append("-p")
    opts.append("4444:4444")
    return opts

def feature_aws(config):
    dax_print("[!]   WARNING: your AWS access tokens are available inside the dax container.")
    dax_print("[!]   a future version of DAX will support only exposing STS tokens in the container.")
    return add_volume(config, "awsdir")

def feature_ssh(config):
    dax_print("[!]   WARNING: your SSH keys are available inside the dax container.")
    return add_volume(config, "sshdir")

def feature_ovpn(config):
    dax_print("[!]   WARNING: ovpn feature not implemented yet.")
    opts = []
    opts.append("--cap-add=NET_ADMIN")
    opts.append("--device=/dev/net/tun")
    opts.append("--sysctl net.ipv6.conf.all.disable_ipv6=0")
    return opts

def feature_workdir(config):
    
    cwd = config['cwd']
    opts = []
    if (not cwd.startswith(os.environ['HOME'])):
        dax_print("[+] DAX is running outside any specific environment... mounting /tmp as working dir")
        tmppath = "/tmp/dax"
        if (os.path.isdir(tmppath) == False):
            os.mkdir(tmppath)
        cwd = tmppath
        
    opts.append("--volume={}:{}".format(cwd, config['workdir']['container']))
    return opts

def add_volume(config, feature):
    opts = []
    opts.append("--volume={}:{}".format(config[feature]['host'], config[feature]['container']))
    return opts

def feature_X11(config):

    # Crazy bit of nonsense to get our local IP address. Try to connect to 
    # somewhere... doesn't matter where... and our IP will be available
    # as the current socket name
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # doesn't even have to be reachable
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
    except:
        ip = '127.0.0.1'
    finally:
        s.close()

    opts = [
        '-e',
        'DISPLAY={}:0'.format(ip),
        '--volume',
        '/tmp/.X11-unix:/tmp/.X11-unix',
    ]
    return opts

def feature_ports(config):

    opts = []
    try:
        for port in config['ports']:
            #dax_print("exposing port: {}".format(port))
            opts.append('-p')
            opts.append(port)
    except:
        dax_print("[!] no ports defined in config file")

    return opts

def parse_cmdline():

    parser = argparse.ArgumentParser(description='Start a docker container for a specific environment.')
    parser.add_argument('-t', dest='testOnly', action='store_true', help='test only - just print the docker command and exit')
    parser.add_argument('-f', dest='features', help='list of comma separated features to include')
    parser.add_argument('-p', dest='ports', action='append', help='additional ports to map <host-port>:<container-port>')
    parser.add_argument('--showFeatures', action='store_true', help='lists available features')
    parser.set_defaults(testOnly=False)

    args = parser.parse_args()

    if args.showFeatures:
        print_features()
        exit(0)

    return args

def launch_container(config):

    name = config['envname']

    # build the base command
    cmd = []
    cmd.append('docker')
    cmd.append('run')
    cmd.append('-it')
    cmd.append('--rm')
    cmd.append('--name')
    cmd.append(name)
    cmd.append('-h')
    cmd.append("{}.fatsec.docker".format(name))

    # find all the requested features from the config 
    # file and the command line
    features = config['features']
    if args.features:
        features.extend(args.features.split(','))

    # Merge any ports requested on the command line
    # with ports specified in the config file
    if args.ports:
        if not 'ports' in config:
            config['ports'] = []
        config['ports'].extend(args.ports)
        if not 'ports' in features:
            features.append('ports')                

    # Now build the command line args for the features
    for feature in features:
        opts = add_feature(feature, config)
        cmd.extend(opts)

    # and finally, the image to use
    cmd.append(config['image'])

    dax_print("[+] running: " + ' '.join(cmd))
    if (args.testOnly  != True):
        if sys.version_info < (3, 7, 0):
            os.system(' '.join(cmd))
        else:
            subprocess.run(cmd)

##
## Entry point
##
if __name__ == "__main__":
    
    args = parse_cmdline();
    config = load_config()
    launch_container(config)
