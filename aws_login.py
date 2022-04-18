#!/usr/bin/env python

from __future__ import print_function
import json
import os
import subprocess
import sys

def usage():
    print("Usage: aws_login.py MFA_CODE")
    exit(-1)

if __name__ == '__main__':

    if len(sys.argv) != 2:
        usage()
    else:
        token = sys.argv[1]

    # cook up the sts serial number
    user = os.getenv('AWS_USERNAME')
    if not user:
        user = os.getenv('LOGNAME')

    os.unsetenv('AWS_ACCESS_KEY_ID')
    os.unsetenv('AWS_SECRET_ACCESS_KEY')
    os.unsetenv('AWS_SESSION_TOKEN')

    serial = 'arn:aws:iam::276883211366:mfa/'
    serial += user

    #print('Enter your MFA code: ', end='')
    #sys.stdout.flush()
    #token = sys.stdin.readline().rstrip()

    cmd = 'aws sts get-session-token --serial-number '
    cmd += serial
    cmd += ' --token-code '
    cmd += token

    #print(cmd)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    (output, err) = p.communicate()
    p_status = p.wait()
    if err:
        print(err)
        exit(-1)
    
    if output:
        creds = json.loads(output)
        print('export AWS_ACCESS_KEY_ID=' + creds['Credentials']['AccessKeyId'])
        print('export AWS_SECRET_ACCESS_KEY=' + creds['Credentials']['SecretAccessKey'])
        print('export AWS_SESSION_TOKEN=' + creds['Credentials']['SessionToken'])
