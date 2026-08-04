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

exec "$@"
