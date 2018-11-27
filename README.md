# DAX

Dockerizes my security tools.

## scripts

* image builder
* container launcher

## Image tasks

* cheatsheets - make writes from container persist
* copy ~/opt tools: gobuster, peda
* install: python, pip, virtualenv, msf, john, nmap, cmsmap, sqlmap, gdb, radare2, build-essentials, 32bit arch support?, socat, postgresql, ruby, rbenv
* run browser in container
* strace/ptrace

## pointers from justinsteven
```
ahhh. a word of warning: I'm told doing the same X trickery on MacOS is going to be a headache if not impossible 😕

https://docs.docker.com/get-started/#test-docker-installation

https://docs.docker.com/engine/reference/builder/ - read up on FROM, RUN, COPY (briefly research COPY vs. ADD and why you should prefer COPY), WORKDIR, CMD. skim everything else very quickly

https://docs.docker.com/engine/reference/run/ - read up on --rm, -t, -i (you'll almost always do '-ti'), --volume (-v) - pay attention to the "ro" decorator, --cap-add, --env. read --security-opt but don't get too bogged down (I treat it as a bit of a black box - it's important for making Chrome behave). skim everything else very quickly

https://github.com/jessfraz/dockerfiles/blob/master/chrome/stable/Dockerfile - read it in conjunction with the Dockerfile reference, read the commented-out 'docker run' monster at the top in conjunction with the docker-run reference

other handy references:
man Dockerfile
man docker-run
other handy things: 'docker ps', 'docker exec', 'docker cp'

twitch.tv/theJustinSteven - Docker for Hackers #programmingtwitch.tv/theJustinSteven - Docker for Hackers #programming

https://www.youtube.com/watch?v=9j15H-YIYb0

https://docs.docker.com/engine/security/security/#docker-daemon-attack-surface (takeaway: putting people in the 'docker' group essentially is the same as making them a passwordless sudoer)
(I have the habit of doing 'sudo -g docker docker run ...' to passwordfully switch into the docker group to give me permission to use the 'docker' client binary to talk to the 'docker socket')

13:27:01[justin@diablo ~]% ls -la /run/docker.sock
srw-rw---- 1 root docker 0 Aug 30 19:55 /run/docker.sock
that bad boy there, writable by group 'docker', is a socket that can be used to root your host

I wouldn't spend tooo much time playing with the kali Docker image perosnally

I think understanding jessfraz's Chrome Image, and playing with 'sudo -g docker docker run -ti --rm debian bash' to drop yourself into a naked Debian container (and seeing just how bare it is) is a much more efficient use of time

then, grow your own version of jax bit by bit

I'll help with the foundations

-----
I should probably write course material on "docker for hackers" 😉
```
