#!/usr/bin/env bash
# Write a short-lived kubeconfig for an OpsPilot ServiceAccount to .secrets/<sa>.kubeconfig.
# Usage: scripts/make_kubeconfig.sh <serviceaccount> [duration]
set -euo pipefail

SA="${1:?usage: $0 <serviceaccount> [duration]}"
DURATION="${2:-12h}"
CONTEXT="${OPSPILOT_ADMIN_CONTEXT:-k3d-opspilot}"
NAMESPACE="opspilot-system"
OUT_DIR="${OPSPILOT_SECRETS_DIR:-.secrets}"
OUT="${OUT_DIR}/${SA}.kubeconfig"

cluster="$(kubectl config view -o jsonpath="{.contexts[?(@.name==\"${CONTEXT}\")].context.cluster}")"
if [[ -z "${cluster}" ]]; then
  echo "context ${CONTEXT} not found; run 'make cluster-up' first" >&2
  exit 1
fi
server="$(kubectl config view -o jsonpath="{.clusters[?(@.name==\"${cluster}\")].cluster.server}")"
ca="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name==\"${cluster}\")].cluster.certificate-authority-data}")"
token="$(kubectl --context "${CONTEXT}" -n "${NAMESPACE}" create token "${SA}" --duration="${DURATION}")"

mkdir -p "${OUT_DIR}"
chmod 700 "${OUT_DIR}"
umask 077
cat > "${OUT}" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: opspilot
    cluster:
      server: ${server}
      certificate-authority-data: ${ca}
users:
  - name: ${SA}
    user:
      token: ${token}
contexts:
  - name: ${SA}
    context:
      cluster: opspilot
      user: ${SA}
      namespace: shop
current-context: ${SA}
EOF
echo "wrote ${OUT} (token valid for ${DURATION})"
