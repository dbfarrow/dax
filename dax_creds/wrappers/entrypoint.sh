#!/bin/sh
# Runs `wire.sh --quiet` once per container start, before the shell (or
# whatever CMD-override a feature set) is exec'd. Deliberately here rather than
# in the `claude` wrapper or a per-feature `_shell_cmd` string: wire.sh's
# self-heal must be scoped to container-boot, not to every `claude` launch —
# docs/design/VALIDATION.md's Part 4 failure-mode tests restart a Claude
# session without restarting the container specifically to prove the
# PreToolUse guard blocks a broken substrate, and re-running wire.sh on every
# `claude` launch would silently repair the breakage before that guard could
# be exercised. The `claude` wrapper only ever runs `wire.sh --check`.
#
# A complete no-op for envs without SUBSTRATE_ROOT set (every env not opted
# into the `substrate` feature).
#
# Never exits on failure: VALIDATION.md Part 4.3 requires the container shell
# to still start even when substrate wiring is broken, so repair is possible
# from inside it.
if [ -n "$SUBSTRATE_ROOT" ]; then
    if [ -x "$SUBSTRATE_ROOT/shared/wire.sh" ]; then
        "$SUBSTRATE_ROOT/shared/wire.sh" --quiet \
            || echo "dax: wire.sh failed — substrate wiring is broken, repair from this shell" >&2
    else
        echo "dax: SUBSTRATE_ROOT=$SUBSTRATE_ROOT but shared/wire.sh is missing — is the mount up?" >&2
    fi
fi

# --- SSH agent bridge -------------------------------------------------------
#
# DAX_SSH_AGENT_TCP_PORT is set by `dax run` (dax.py's feature_ssh) when this
# env has the `ssh` feature and a host agent socket resolved. It used to be a
# bind-mounted Unix socket instead — the same class of problem the Claude
# credential daemon already hit: Docker Desktop's Mac-VM file sharing does
# not reliably keep a live Unix socket's connect/accept semantics working
# across a sleep/wake cycle or a VM restart, even when both real endpoints
# (host agent, container) never went away. TCP over host.docker.internal
# survives that boundary; a bind-mounted socket does not.
#
# ssh/git need a real Unix socket for SSH_AUTH_SOCK, not a TCP address, so
# this relays: local Unix socket <-> TCP to the host bridge dax run started.
# socat forwards raw bytes — it has no idea it's carrying the SSH agent
# protocol, and doesn't need to.
#
# Started once per container boot, alongside wire.sh above, not per `claude`
# launch — the bridge is a property of the container's lifetime, not of any
# one command run inside it.
if [ -n "$DAX_SSH_AGENT_TCP_PORT" ]; then
    socat UNIX-LISTEN:/tmp/ssh-agent.sock,fork,unlink-early \
          TCP:host.docker.internal:"$DAX_SSH_AGENT_TCP_PORT" &
    export SSH_AUTH_SOCK=/tmp/ssh-agent.sock
fi

exec "$@"
