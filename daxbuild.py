#!/usr/bin/env python3
#
# script to build a DAX container tailored for a specific environment
#

import argparse
import os
import os.path
import socket
import subprocess
import sys
#import yaml
from util import dax_print

def parse_cmdline():

    parser = argparse.ArgumentParser(description='Build a docker image containing my standard tools.')
    parser.add_argument('-t', dest='testOnly', action='store_true', help='test only - just print the docker command and exit')
    parser.add_argument('-c', dest='clean', action='store_true', help='clean build - don\'t use cached iamges')
    parser.set_defaults(testOnly=False)

    args = parser.parse_args()
    return args

def get_version():
    
    with open("./VERSION", "r") as vfile:
        return vfile.readline().rsplit()[0]

def get_dax_passwd():
    
    try:
        # For the lazy among us, we can store the default password for the 
        # container in the build dir.
        pwfile = "./.daxpw"
        stat = os.stat(pwfile)
        if (os.stat(pwfile).st_mode == 33152):
            dax_print("[-]   read password from .daxpw")
            with open("./.daxpw", "r") as vfile:
                return vfile.readline().rsplit()[0]
        else:
            dax_print("[!] password file must be read only for the owner")

    except:
        dax_print("[-]   if you tire of typing in a password for dax, put it in {} and set the permissions to 0600... then try again".format(pwfile))
        return input("Enter a password for the dax container:")

    # If we get here, there was a .daxpw file, it didn't have perms of 0600. When I put
    # the exit in the else statement above, the exit() call appears to have raised
    # an exception that was caught in the except clause... wierd. So here we are
    exit(-1)

#
# I've noticed that not all systems user $USER for the current user name (looking at you Ubuntu).
# So here's a little help not failing to get the current user's name.
#
def get_username():
    try:
        return os.environ['USER']
    except:
        return os.environ['LOGNAME']


def get_user_info():

    opts = []

    opts.append('--build-arg')
    opts.append('user={}'.format(get_username()))
    opts.append('--build-arg')
    opts.append('user_id={}'.format(os.geteuid()))
    opts.append('--build-arg')
    opts.append('user_gid={}'.format(os.getgid()))

    return opts

def build_dockerfile():
    
    dax_print("[+] building Dockerfile from Dockerfile.tmpl and magic")

    with open('./Dockerfile', 'w') as df:
        user = get_username()
        shell = os.environ['SHELL']
        passwd = get_dax_passwd()
        df.write(f'''
#
# Inspired by justinsteven's jax... a replacement for my kali VM
#

FROM ubuntu:latest

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \\
	apt-utils \\
	apt-transport-https \\
	build-essential \\
	ca-certificates \\
	curl \\
	default-jdk \\
	openjdk-8-jre \\
	dnsutils \\
	ftp \\
	gcc-multilib \\
	gdb \\
	git \\
	golang \\
	inetutils-ping \\
	iproute2 \\
    jq \\
	locales \\
	lsb-release \\
	netcat \\
	net-tools \\
	nmap \\
	man \\
	pass \\
	procps \\
	python3-pip \\
	python3-virtualenv \\
	rbenv \\
	ruby-full \\
	socat \\
	sudo \\
	tcpdump \\
	tmux \\
	vim \\
	virtualenv \\
	wget \\
	zsh

# install metasploit and pwntools
#RUN curl https://raw.githubusercontent.com/rapid7/metasploit-omnibus/master/config/templates/metasploit-framework-wrappers/msfupdate.erb > /tmp/msfinstall \\
#	&& chmod 755 /tmp/msfinstall \\
#	&& /tmp/msfinstall \\
#	&& pip install pwntools

# install AWS CLI v2
RUN apt-get install -y libssl-dev libffi-dev \\
    && cd /tmp \\
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \\
    && unzip awscliv2.zip \\
    && ./aws/install

# setup my user
RUN echo 'en_US.UTF-8 UTF-8' >> /etc/locale.gen \\
    && locale-gen \\
	&& useradd -m -s {shell} {user} \\
	&& usermod -a -G sudo {user} \\
	&& echo {user}:{passwd} | chpasswd \\
	&& echo "done setting up user"

RUN pip install scoutsuite
COPY --chown={user} dotfiles/ /home/{user}/


#
# add new install stuff here until ready to rebuild the base image
# move these instructions to the right place before the next complete rebuild
# end of temp instructions

USER {user}	

# install Azure CLI - must be installed as the user...
#RUN cd tmp/ \\
#    && curl -L https://aka.ms/InstallAzureCli -o a \\
#    && echo "\\n\\n\\n\\n" > ./b \\
#    && /bin/sed -e 's|/dev/tty|./b|' -i a \\
#    && /bin/bash a \\
#    && rm a b

WORKDIR /home/{user}
CMD ["/bin/zsh"]
''')

    return

def build_container():

    dax_print("[+] building container")

    # build the base command
    cmd = []
    cmd.append('docker')
    cmd.append('build')
    cmd.extend(get_user_info())
    if (args.clean):
        cmd.append('--no-cache')
    cmd.append('-t')
    cmd.append('dfarrow/dax:{}'.format(get_version()))
    cmd.append('.')
    
    runcmd(cmd)

def tag_container():
    
    cmd = []
    cmd.append("docker")
    cmd.append("rmi")
    cmd.append("dfarrow/dax:latest")
    runcmd(cmd)

    cmd = []
    cmd.append("docker")
    cmd.append("tag")
    cmd.append('dfarrow/dax:{}'.format(get_version()))
    cmd.append("dfarrow/dax:latest")
    runcmd(cmd)

def cleanup():
    
    cmd = []
    cmd.append('/bin/rm')
    cmd.append('-f')
    cmd.append('./Dockerfile')
    runcmd(cmd)

def runcmd(cmd):

    dax_print("[-]   " + ' '.join(cmd))
    if (args.testOnly != True):
        if sys.version_info < (3, 7, 0):
            os.system(' '.join(cmd))
        else:
            subprocess.run(cmd)



##
##
## Entry point
##
if __name__ == "__main__":
    
    args = parse_cmdline()
    build_dockerfile()
    build_container()
    tag_container()
    cleanup()
    dax_print("[+] Commence to take over the world...")
