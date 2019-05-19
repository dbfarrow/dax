#!/usr/bin/env python

def dax_print(msg):

	msg = msg.replace("[+]", '\033[92m' + "[+]" + '\033[0m')
	msg = msg.replace("[-]", '\033[93m' + "[-]" + '\033[0m')
	msg = msg.replace("[!]", '\033[91m' + "[!]" + '\033[0m')
	print(msg)
