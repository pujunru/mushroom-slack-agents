#!/usr/bin/env bash
# Provision Vault for Mushroom: a KV v2 secret, a read-only policy, and a
# Kubernetes auth role that External Secrets Operator can assume.
#
# Run against a Vault you have already initialised and unsealed. Requires the
# `vault` CLI, with VAULT_ADDR and a token that can write policies and auth
# configuration.
#
#   export VAULT_ADDR=https://vault.example.com
#   export VAULT_TOKEN=<admin token>
#   ./scripts/vault-bootstrap.sh
#
# Values are read from the environment so no secret is ever passed on the
# command line, where it would land in shell history:
#
#   SLACK_SIGNING_SECRET=... SLACK_BOT_TOKEN=... ANTHROPIC_API_KEY=... \
#     ./scripts/vault-bootstrap.sh

set -euo pipefail

KV_MOUNT="${KV_MOUNT:-secret}"
SECRET_PATH="${SECRET_PATH:-mushroom}"
POLICY_NAME="${POLICY_NAME:-mushroom-read}"
K8S_AUTH_PATH="${K8S_AUTH_PATH:-kubernetes}"
VAULT_ROLE="${VAULT_ROLE:-mushroom}"
# The ServiceAccount and namespace ESO presents when it authenticates. These
# must match the chart's serviceAccount name and release namespace.
K8S_NAMESPACE="${K8S_NAMESPACE:-mushroom}"
K8S_SERVICE_ACCOUNT="${K8S_SERVICE_ACCOUNT:-mushroom}"
# Vault 2.x requires an audience on the Kubernetes role.
VAULT_AUDIENCE="${VAULT_AUDIENCE:-vault}"

command -v vault >/dev/null || { echo "vault CLI not found on PATH" >&2; exit 1; }
: "${VAULT_ADDR:?set VAULT_ADDR}"

echo "==> Vault at ${VAULT_ADDR}"
vault status >/dev/null || { echo "Vault is unreachable or sealed" >&2; exit 1; }

# --- KV v2 mount -----------------------------------------------------------
if ! vault secrets list -format=json | grep -q "\"${KV_MOUNT}/\""; then
    echo "==> Enabling KV v2 at ${KV_MOUNT}/"
    vault secrets enable -path="${KV_MOUNT}" -version=2 kv
else
    echo "==> KV mount ${KV_MOUNT}/ already present"
fi

# --- the application's secrets --------------------------------------------
# Only write keys that were actually provided, so re-running without a
# variable set does not blank an existing value.
args=()
[[ -n "${SLACK_SIGNING_SECRET:-}" ]] && args+=("slack_signing_secret=${SLACK_SIGNING_SECRET}")
[[ -n "${SLACK_BOT_TOKEN:-}" ]]      && args+=("slack_bot_token=${SLACK_BOT_TOKEN}")
[[ -n "${ANTHROPIC_API_KEY:-}" ]]    && args+=("anthropic_api_key=${ANTHROPIC_API_KEY}")
[[ -n "${DATABASE_URL:-}" ]]         && args+=("database_url=${DATABASE_URL}")

if (( ${#args[@]} )); then
    echo "==> Writing ${#args[@]} key(s) to ${KV_MOUNT}/${SECRET_PATH}"
    # `patch` preserves keys that are not being set. Falls back to `put` when
    # the secret does not exist yet.
    vault kv patch -mount="${KV_MOUNT}" "${SECRET_PATH}" "${args[@]}" 2>/dev/null \
      || vault kv put -mount="${KV_MOUNT}" "${SECRET_PATH}" "${args[@]}"
else
    echo "==> No secret values in the environment; skipping the write."
    echo "    Set SLACK_SIGNING_SECRET, SLACK_BOT_TOKEN, ANTHROPIC_API_KEY to populate."
fi

# --- read-only policy ------------------------------------------------------
echo "==> Writing policy ${POLICY_NAME}"
vault policy write "${POLICY_NAME}" - <<POLICY
# Read-only access to Mushroom's secrets. KV v2 nests the data under data/.
path "${KV_MOUNT}/data/${SECRET_PATH}" {
  capabilities = ["read"]
}
path "${KV_MOUNT}/metadata/${SECRET_PATH}" {
  capabilities = ["read", "list"]
}
POLICY

# --- Kubernetes auth -------------------------------------------------------
if ! vault auth list -format=json | grep -q "\"${K8S_AUTH_PATH}/\""; then
    echo "==> Enabling Kubernetes auth at ${K8S_AUTH_PATH}/"
    vault auth enable -path="${K8S_AUTH_PATH}" kubernetes
    echo
    echo "    Configure it with your cluster's API address and CA, e.g. from"
    echo "    inside the cluster:"
    echo
    echo "    vault write auth/${K8S_AUTH_PATH}/config \\"
    echo "        kubernetes_host=https://\$KUBERNETES_SERVICE_HOST:\$KUBERNETES_SERVICE_PORT"
    echo
else
    echo "==> Kubernetes auth already enabled at ${K8S_AUTH_PATH}/"
fi

echo "==> Writing role ${VAULT_ROLE}"
vault write "auth/${K8S_AUTH_PATH}/role/${VAULT_ROLE}" \
    bound_service_account_names="${K8S_SERVICE_ACCOUNT}" \
    bound_service_account_namespaces="${K8S_NAMESPACE}" \
    audience="${VAULT_AUDIENCE}" \
    policies="${POLICY_NAME}" \
    ttl=1h

cat <<SUMMARY

Done.

  secret path   ${KV_MOUNT}/${SECRET_PATH}
  policy        ${POLICY_NAME}
  k8s role      ${VAULT_ROLE}  (sa=${K8S_SERVICE_ACCOUNT}, ns=${K8S_NAMESPACE}, audience=${VAULT_AUDIENCE})

Install the chart against Vault with:

  helm install mushroom deploy/helm/mushroom \\
    --namespace ${K8S_NAMESPACE} --create-namespace \\
    --set secrets.provider=externalSecrets \\
    --set secrets.externalSecrets.vault.server=${VAULT_ADDR} \\
    --set secrets.externalSecrets.vault.auth.kubernetes.role=${VAULT_ROLE}

SUMMARY
