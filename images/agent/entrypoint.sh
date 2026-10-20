#!/usr/bin/env sh
# Place opencode's configuration where it will be found, then exec the worker.
#
# opencode searches from the working directory upward and honours
# ~/.config/opencode. HOME is set to the writable workspace so this works
# under a read-only root filesystem.
set -eu

: "${WORKSPACE_ROOT:=/workspace}"
export HOME="${WORKSPACE_ROOT}"

mkdir -p "${HOME}/.config/opencode"

# cli.json carries the unattended approval mode; opencode.json carries the
# model and provider wiring. Both are baked into the image and copied rather
# than symlinked so opencode can write sibling state next to them.
if [ -f /etc/mushroom/opencode-cli.json ]; then
    cp -f /etc/mushroom/opencode-cli.json "${HOME}/.config/opencode/cli.json"
fi
if [ -f /etc/mushroom/opencode.json ]; then
    cp -f /etc/mushroom/opencode.json "${HOME}/.config/opencode/opencode.json"
fi

exec "$@"
