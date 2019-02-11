#!/usr/bin/env python
#
# script to instantiate a DAX container tailored for a specific environment
#

import argparse
import os
import os.path
import socket
import subprocess
import sys
import yaml

DAX_CONFIG = "{}/.dax.yaml".format(os.environ['HOME'])

def load_config():
	
	home = os.environ['HOME']

	# load the system defaults
	with open(DAX_CONFIG, 'r') as deffile:
		defaults = yaml.load(deffile)
		defaults['envname'] = 'DAX'

	cwd = os.getcwd()
	if (not cwd.startswith(home)):
		print "dax must be run from somewhere under your home dir"
		exit(-1)
	defaults['cwd'] = cwd

	print("looking for config file in {}".format(cwd))
	cfgfile = "{}/.dax.yaml".format(cwd)
	config = {}

	if os.path.isfile(cfgfile):
		print("config file is in {}".format(cfgfile))
		with open(cfgfile, 'r') as ymlfile:
			config = yaml.load(ymlfile)

	config['envname'] = cwd.replace(home, "").replace("/", "", 1).replace("/", "-")

	# merge the defaults and the environment config together # and return it
	defaults['cfgdir'] = cwd
	defaults.update(config)
	return defaults
	
def add_feature(feature, config):
	
	return globals()["feature_"+feature](config)

	#try:
		#print("adding {}".format(feature))
		#opts = globals()["feature_"+feature](config)
		#return opts
	#except KeyError as ex:
		#print("Unknown feature: {}".format(feature))
		#print_features()
		#exit(-1)
	#else:
		#exit(-1)

def print_features():
	
	print("ERROR: {} supports the following features:".format(__file__))

	funcs = globals()
	for func in funcs.keys():
		if (func.startswith("feature_")):
			feature = func.replace("feature_", "")
			print("\t{}".format(feature))

def feature_optdir(config):
	return add_volume(config, 'optdir')

def feature_cheatdir(config):
	return add_volume(config, 'cheatdir')

def feature_msf(config):
	return add_volume(config, 'msf')

def feature_workdir(config):
	
	cwd = config['cwd']
	opts = []
	if (not cwd.startswith(os.environ['HOME'])):
		print("DAX is running outside any specific environment... mounting /tmp as working dir")
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

	s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
	try:
		# doesn't even have to be reachable
		s.connect(('10.255.255.255', 1))
		IP = s.getsockname()[0]
	except:
		IP = '127.0.0.1'
	finally:
		s.close()

	opts = [
		'-e',
		'DISPLAY={}:0'.format(IP),
		'--volume',
		'/tmp/.X11-unix:/tmp/.X11-unix',
		'-p',  
		'8080:8080'
	]
	return opts

def feature_ports(config):

	opts = []
	for port in config['ports']:
		#print("exposing port: {}".format(port))
		opts.append('-p')
		opts.append(port)

	return opts

def feature_entrypoint(config):

	opts = [
		'--entrypoint', 
		'/home/{}/work/{}'.format(os.environ['USER'], config['entry']),
	]
	return opts

def parse_cmdline():

	parser = argparse.ArgumentParser(description='Start a docker container for a specific environment.')
	parser.add_argument('-e', help='name of the enviroment (see ~/.dax.json)')
	parser.add_argument('-t', dest='testOnly', action='store_true', help='test only - just print the docker command and exit')
	#parser.add_argument('-p', dest='persist', action='store_true', help='persist - do not remove the instance upon exit')
	parser.set_defaults(testOnly=False)

	args = parser.parse_args()
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
	
	# add in the desired features
	for feature in config['features']:
		opts = add_feature(feature, config)
		cmd.extend(opts)

	# if a subdir has added on any extra features, add them
	if ('extras' in config):
		for feature in config['extras']:
			opts = add_feature(feature, config)
			cmd.extend(opts)
		
	# and finally, the image to use
	cmd.append(config['image'])

	print(' '.join(cmd))
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
