#!/usr/bin/env python
#
# script to build a DAX container tailored for a specific environment
#

import argparse
import os
import os.path
import socket
import subprocess
import sys
import yaml
from util import dax_print

DAX_TMPL_FILE = './Dockerfile.tmpl'

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
		return raw_input("Enter a password for the dax container:")

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

	cmd = []
	cmd.append('sed')
	cmd.append('-e')
	cmd.append("'s/$user/{}/g'".format(get_username()))
	cmd.append('-e')
	cmd.append("'s/$euid/{}/g'".format(os.geteuid()))
	cmd.append('-e')
	cmd.append("'s/$gid/{}/g'".format(os.getgid()))
	cmd.append('-e')
	cmd.append("'s/$passwd/{}/g'".format(get_dax_passwd()))
	cmd.append('-e')
	#cmd.append("'s/$shell/{}/g'".format(os.environ['SHELL']))
	cmd.append("'s/$shell/\/bin\/{}/g'".format('zsh'))
	cmd.append(DAX_TMPL_FILE)
	cmd.append(">")
	cmd.append("./Dockerfile")
	runcmd(cmd)

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
	
