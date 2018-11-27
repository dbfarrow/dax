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
		pwfile = "./.daxpw"
		stat = os.stat(pwfile)
		if (os.stat(pwfile).st_mode == 33152):
			print("file perms = {}".format(os.stat(pwfile).st_mode))
			with open("./.daxpw", "r") as vfile:
				return vfile.readline().rsplit()[0]
		else:
			print("password file must be read only for the owner")
			exit(-1)

	except:
		print("If you tire of typing in a password for dax, put it in {} and set the permissions to 0600... then try again".format(pwfile))
		return raw_input("Enter a password for the dax container:")
		exit(-1)

def get_user_info():

	opts = []

	opts.append('--build-arg')
	opts.append('user={}'.format(os.environ['USER']))
	opts.append('--build-arg')
	opts.append('user_id={}'.format(os.geteuid()))
	opts.append('--build-arg')
	opts.append('user_gid={}'.format(os.getgid()))

	return opts

def build_dockerfile():
	
	cmd = []
	cmd.append('sed')
	cmd.append('-e')
	cmd.append("'s/$user/{}/g'".format(os.environ['USER']))
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

def cleanup():
	
	cmd = []
	cmd.append('/bin/rm')
	cmd.append('-f')
	cmd.append('./Dockerfile')
	runcmd(cmd)

def runcmd(cmd):

	print(' '.join(cmd))
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
	cleanup()
	
