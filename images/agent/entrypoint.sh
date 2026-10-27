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
# model selection and the skills source (/etc/mushroom/skills). Both are baked into the image and copied rather than
# symlinked so opencode can write sibling state next to them.
#
# opencode.json deliberately has no `providers` block. opencode v2's
# documented way to supply a credential is the provider's own environment
# variable reaching the server process — which is what the worker does, and
# why it invokes opencode with --standalone. A `providers.<name>.settings.apiKey`
# entry is documented only for Ollama and custom openai-compatible providers,
# not for the native gateway packages, so relying on it would be guessing.
if [ -f /etc/mushroom/opencode-cli.json ]; then
    cp -f /etc/mushroom/opencode-cli.json "${HOME}/.config/opencode/cli.json"
fi
if [ -f /etc/mushroom/opencode.json ]; then
    cp -f /etc/mushroom/opencode.json "${HOME}/.config/opencode/opencode.json"
fi

exec "$@"
