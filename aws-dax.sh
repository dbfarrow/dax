#!/bin/bash
set +e 

yum update -y
amazon-linux-extras install docker
systemctl enable docker 
systemctl start docker 

yum install -y git

useradd -m -G wheel,docker dfarrow
mkdir ~dfarrow/.ssh
chmod 0700 ~dfarrow/.ssh
cat <<EOF > ~dfarrow/.ssh/authorized_keys
ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQDQf4x2JZjEXIxkqKu+EpjYX9EDFssgD7OPLWgtzuBgG/QNi4yv7gz/nxmGhZyveCJdka6qFe/+GngtdFwupDPiYzGvW8NVEQOCBjQrS3/CNkjjQ44aTFCn86Os4x8Nbx6sPXr1IoCnYFVEdt/HcsAJ+mGFmkQXZiWS1SdhgpIPChB6iO13wDYamg3Sj+hms0yWtN1gIRKzobyvLFgXfY0vXdXLYrrErVa/67LZVRq2lPiesMJFjmafRhJ4LMp3+Q94PlDE1U+h6R1PvIENpKs7POgAxZd1f/g9lICtS/vzFUD2SzzghollPYnEmfE8gHkpnigFAuukyxmR6a21Yrfj dfarrow@daves-mbp.cudanet.local
EOF

chown -R dfarrow:dfarrow ~dfarrow/.ssh

cat <<EOF > ~dfarrow/finish.sh
#!/bin/bash

git clone git@github.com:dbfarrow/dax.git
git clone git@github.com:dbfarrow/shello-world.git

cat <<FIN > ~dfarrow/.dax.yaml
# DAX config file for a bare tools enviroment. The current working dir
# will be mounted as the work directory. 
#
# If there are features that you want to define to be available to any 
# instance then define their configuration here and reference them
# in the local .dax.yaml file feature key or on the command line 
# with -f
#

image: dfarrow/dax:latest

features: 
  - workdir
  - optdir

workdir:
  container: /home/dfarrow/work

optdir:
  host: /home/dfarrow/fatsec/opt
  container: /home/dfarrow/opt

msf:
  host: /home/dfarrow/.msf4
  container: /home/dfarrow/.msf4

awsdir:
  host: /home/dfarrow/.aws
  container: /home/dfarrow/.aws

sshdir:
  host: /home/dfarrow/.ssh
  container: /home/dfarrow/.ssh

gpgdir:
  host: /home/dfarrow/.gnupg
  container: /home/dfarrow/.gnupg
FIN

chown dfarrow:dfarrow ~dfarrow/finish.sh
chmod 0755 ~dfarrow/finish.sh

cd dax && ./daxbuild.py

EOF

echo ""
echo ""
echo ""
echo "DAX setup is almost complete. Before logging in to the new dax server as dfarrow, log"
echo "in as ec-user and change dfarrow's password. Then log in as dfarrow and run ./finish.sh"
echo ""
echo ""
echo ""
