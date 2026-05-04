# DAX - treating my laptop like cattle since 2018

Dockerizes my security tools*.

1. [Prerequisites](#prerequisites)
1. [Building](#building)
  1. [Dotfiles](#dotfiles) 
1. [Running](#running)
  1. [Usage](#usage)
  1. [Configuration](#configuration)
  1. [Features](#features)
  1. [Adding Features](#adding-features)
1. [Origins](#origins)


Dax is motivated by the ideal of infrastructure as code: It should be a simple and repeatable process to create a consistent working environment on a brand new machine. Such a process makes recovery from system failure a quick and simple thing.

We take this idea one step further and maintain that it should be simple and repeatable to create multiple simultaneous such evironments on the same system. It is often desirable whileing work on a project to install packages that one would not normally want on a primary system. Dax allows a user to create a new environment, experiment with new packages, persist the work produced, and tear down the envrinment without changing the primary system state. And all work product from the experimental environment is retained on the primary system for future use.

\* This pattern can be applied to any tools.. not just security tools. I just happen to have mostly security tools in my environment.

>This project was inspired by a good friend's implementation of the same idea. While this code is free to use, I highly recommend you build this for yourself. The exercise of thinking through what parts of your working envrioment can be replaced at will and which must persist has been a fruitful experience for me and I expect it would be for you as well.

## <a id='prerequisites'></a>Prerequisites

To build DAX you will need the following:

* Python 3.7+
* virtualenv
* git
* docker
 
**NOTE:** DAX has been built and tested using MacOS 10.14, python2.7, and Docker Desktop Community 2.0.0.3. Some minimal testing has been done to ensure that the scripts run under python3 but your mileage may vary.

## <a id='building'></a>Building

Clone the repository and install:

```
git clone https://github.com/dbfarrow/dax.git
cd dax
pip install -e .
```

This installs the `dax` command globally so you can run it from any directory.

Build the image:

```
dax build
```

Use `-c` for a clean build (no cache):

```
dax build -c
```

The build script will prompt for a container password unless a `.daxpw` file exists with `0600` permissions.

### <a id='dotfiles'></a>Dotfiles

Dotfiles are mounted from the host at runtime rather than baked into the image. Configure them in `~/.dax.yaml` under the `dotfiles` key:

```yaml
dotfiles:
  ro:
    - ~/.zshrc
    - ~/.vimrc
    - ~/.gitconfig
    - ~/.tmux.conf
  rw:
    - ~/.zsh_history
```

Files in `ro` are mounted read-only. Files in `rw` are mounted read-write. The `dotfiles/` directory in the repo is no longer used.

## <a id='running'></a>Running

### <a id='usage'></a>Usage
```
usage: dax.py [-h] [-t] {build,run,features} ...

optional arguments:
  -h, --help   show this help message and exit
  -t           test only - print the docker command and exit

subcommands:
  build        Build the dax Docker image
  run          Launch a dax container
  features     List available features

dax.py run [-f FEATURES] [-p PORTS]
  -f FEATURES  comma-separated features to include
  -p PORTS     port mapping host:container (repeatable)
```



### <a id='configuration'></a>Configuration

Dax has a global configuration file that defines the base image for the environment, the default features to include in an instance, and default properties for the features supported.

#### ~/.dax.yaml

```yaml
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
  host: /Users/dfarrow/fatsec/opt
  container: /home/dfarrow/opt

msf:
  host: /Users/dfarrow/.msf4
  container: /home/dfarrow/.msf4

awsdir:
  host: /Users/dfarrow/opt/dax/dotfiles/.aws
  container: /home/dfarrow/.aws


```

#### Contents of ~/fatsec/code/aws/.dax.yaml:
```yaml
features:
  - aws
```



## <a id='features'></a>Features

The following features are currently supported:

| name | description |  
| -----| ----------- |
| workdir | Specifies the path in the dax instance where the current host directory will be mapped |
| optdir | Maps a directory of additional tools into the running container. Useful for things like wordlists and tools that aren't installed but, rather, run from a local directory. |
| ports | Specifies the ports to expose to the host |
| aws | Maps your AWS access tokens into the instance |
| msf | Maps your metasploit database into the instance and exposes a default port |
| X11 | Establishes the conditions necessary to forward X11 applications back to the host. 

---
#### workdir

This feature is the key to dax. It allows you to spin up a pristine working environment, do some work, and the results will persist on the host as long as they are written, in the instance, to the directory configured in this feature. 

By default, when starting dax, the current directory will be mapped to `~/work` in the instance.

Example config:

```yaml
workdir:
  container: /home/dfarrow/work
```
where:

  * **container** is the path where the host current directory will be mounted in the running instance.
  
---
#### optdir

Use this feature to map into the dax instances tools that don't require installation. For example, if you run a development instance of metasploit, you can map your msf project directory into each dax instance by putting the project in to the opt directory that gets mapped in. This is also useful for supplying large data sets like the wordlists from SecLists to each of your instances without making copies everywhere.

Example config:

```yaml
optdir:
  host: /Users/dfarrow/fatsec/opt
  container: /home/dfarrow/opt
```
  
where: 

  * ***host*** is the source path on the host for additional tools to map into the running instance and 
  * **container** is the path to those tools in the instance. 

---

#### ports

By default, no ports are exposed from the container when it starts. To expose a service, you must specify the ports to expose at run time. They cannot be exposed while the container is running.

Example config:

```yaml
ports:
  - 8000:80
  - 8443:443
```

The configured values are passed directly to the `-p` paramter of `docker run`. In this example, port 80 of the running instance will be bound to port 8000 of the host machine. And port 443 will be bound to 8443. When exposing services from the container it may be necessary to bind the service to `0.0.0.0` for the host to be able to see it.

Alternately, you can pass ports on the command line using the `-p` option if you don't want to set up a local config file.

---
#### aws

This feature makes your AWS client access tokens available to the running container. 

Example config:

```yaml
awsdir:
  host: /Users/dfarrow/opt/dax/dotfiles/.aws
  container: /home/dfarrow/.aws
```

where
  * **host** is the path to your AWS config and credentials on the host
  * **container** is the path where the config and credentials will be mounted in the container

A future release of dax will generate Amazon STS tokens and map them into the running container rather than exposing the user's client access tokens directly.
 
---
#### msf

This feature exists to persist your MSF database between runs of dax. The MSF database files are stored on the host filesystem and mounted at runtime.

The feature also exposes a single port, `4444`, by default. Use the `-p` option or create a local `.dax.yaml` file to specify other ports you want exposed.

Example config:

```yaml
msfdir:
  host: /Users/dfarrow/.msf4
  container: /home/dfarrow/.msf4
```

where:
  * **host** is the path to the MSF database files you want to have persist across dax runs
  * **container** is the path in the container to the MSF database. Metasploit expects it to be in ~/.msf4 so it's probably best to not move it

Note that containers can share the MSF database but they should not try to share it simultaneously. Each container will start its own instance of postgres to manage the database and those postgres processes will interfere with each other.

I recommend using a workspace for each environment you use this feature in so that you can keep your work separated.  

#### X11

This feature configure the container to forward X11 application user interfaces back to the host display. It requires no configuration.

Before running dax with this feature, start the X11 service in your host if it's not already started.

On MacOS, the following command does the trick: `xhost + <hostip>`

---

#### ssh

SSH access inside the container uses agent forwarding via Docker Desktop's host agent socket. No private keys are ever copied into the container.

Make sure your keys are loaded before launching:

```
ssh-add ~/.ssh/id_rsa
```

Then use the `ssh` feature:

```
python dax.py run -f ssh
```

From inside the container, `ssh -A user@host` will forward the agent onward to remote hosts.

### <a id='adding-features'></a>Adding features

To add a new feature to dax, implement a function in `dax.py` following the naming and calling convention shown below:

```python
def feature_hosttmp(config):
	opts = []
	opts.append("-v")
	opts.append("/tmp:/tmp/host")
	return opts
```

The feature above maps the hosts `/tmp` directory into the `/tmp/host` dir in the running container.

The feature can now be referenced in config files or on the command line by the name `hosttmp`.


## Origins

The following material came from discussions with my muse who will rename nameless until he approves of the messages attributed to him herein.

#### Image tasks

* copy ~/opt tools: gobuster, peda
* install: python, pip, virtualenv, msf, john, nmap, cmsmap, sqlmap, gdb, radare2, build-essentials, 32bit arch support?, socat, postgresql, ruby, rbenv
* run browser in container
* strace/ptrace

#### Collection of thoughts from my muse

>ahhh. a word of warning: I'm told doing the same X trickery on MacOS is going to be a headache if not impossible 😕

Getting started with docker
> https://docs.docker.com/get-started/#test-docker-installation

> https://docs.docker.com/engine/reference/builder/ - read up on FROM, RUN, COPY (briefly research COPY vs. ADD and why you should prefer COPY), WORKDIR, CMD. skim everything else very quickly

> https://docs.docker.com/engine/reference/run/ - read up on --rm, -t, -i (you'll almost always do '-ti'), --volume (-v) - pay attention to the "ro" decorator, --cap-add, --env. read --security-opt but don't get too bogged down (I treat it as a bit of a black box - it's important for making Chrome behave). skim everything else very quickly

> https://github.com/jessfraz/dockerfiles/blob/master/chrome/stable/Dockerfile - read it in conjunction with the Dockerfile reference, read the commented-out 'docker run' monster at the top in conjunction with the docker-run reference

>other handy references:
> man Dockerfile
> man docker-run
> other handy things: 'docker ps', 'docker exec', 'docker cp'

Some video tutorials that may help

> twitch.tv/theJustinSteven - Docker for Hackers #programmingtwitch.tv/theJustinSteven - Docker for Hackers #programming

> https://www.youtube.com/watch?v=9j15H-YIYb0

Security considerations of running docker on your machine:

> https://docs.docker.com/engine/security/security/#docker-daemon-attack-surface (takeaway: putting people in the 'docker' group essentially is the same as making them a passwordless sudoer)
(I have the habit of doing 'sudo -g docker docker run ...' to passwordfully switch into the docker group to give me permission to use the 'docker' client binary to talk to the 'docker socket')

> ```
13:27:01[~]% ls -la /run/docker.sock
srw-rw---- 1 root docker 0 Aug 30 19:55 /run/docker.sock
that bad boy there, writable by group 'docker', is a socket that can be used to root your host
```

My muse expresses his opinion of using the kali docker image for this exercise. 
> I wouldn't spend tooo much time playing with the kali Docker image personally
> 
> I think understanding jessfraz's Chrome Image, and playing with 'sudo -g docker docker run -ti --rm debian bash' to drop yourself into a naked Debian container (and seeing just how bare it is) is a much more efficient use of time

After pressing him, he reiterates  The recommendation is not a reflection on Kali but rather an reiteration of his belief that the practice of accumulating your own security tools is a valuable exercise.



