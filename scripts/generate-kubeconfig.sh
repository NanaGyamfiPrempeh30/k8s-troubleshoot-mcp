#!/usr/bin/env bash
#
# generate-kubeconfig.sh — provision RBAC and mint a kubeconfig for
# k8s-troubleshoot-mcp (REQ-014).
#
# Usage:
#   scripts/generate-kubeconfig.sh <output-kubeconfig-path> <namespace> [namespace...]
#
# REQ-014 specifies $1 as the operator-chosen kubeconfig output path, so the
# namespaces the server may read follow as $2..$N. Pass the same set you intend
# to put in ALLOWED_NAMESPACES: RBAC is the enforcement boundary, and a
# namespace bound here but absent from ALLOWED_NAMESPACES (or vice versa) is a
# mismatch between real permission and configured capability.
#
# What this does NOT do: a blanket `kubectl apply -f kubernetes/`. That command
# does not error — it silently does the wrong thing, which is why this script
# handles the two namespaced manifests explicitly. Verified against a v1.35
# API server with --dry-run=server: a blanket apply reports 5 resources, not 6.
#   * role.yaml carries no metadata.namespace, so with no -n it is created in
#     the current context's namespace (typically `default`). The apply succeeds
#     and reports "created" while granting read access to the wrong namespace.
#   * rolebinding.yaml.template is never read at all. `kubectl apply -f <dir>`
#     only picks up .yaml/.yml/.json, so the .template extension is skipped
#     without comment and NO RoleBinding is created anywhere.
# The combined effect is a provisioning run that looks successful and leaves
# the server unable to read a single namespace.
#
# The four cluster-level manifests are applied together; the other two are
# rendered and applied per namespace, explicitly.
#
# On success the generated kubeconfig path is printed to stdout and nothing
# else is. All diagnostics go to stderr.

set -euo pipefail

# The token is written into a file; never let it reach a shell trace.
set +x

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MANIFEST_DIR="${REPO_ROOT}/kubernetes"

SA_NAME="k8s-mcp-server"
SA_NAMESPACE="k8s-mcp"
CONTEXT_NAME="k8s-mcp"
CLUSTER_ENTRY="k8s-mcp-cluster"

# TokenRequest lifetime. The API server caps this at its own configured maximum
# (--service-account-max-token-expiration), so the token actually issued may be
# shorter than requested — the real expiry is reported below.
TOKEN_DURATION="${TOKEN_DURATION:-8760h}"

# Namespaces the server refuses to read at startup (REQ-008). Binding them here
# would create real permission with no matching capability.
readonly REFUSED_NAMESPACES=("kube-system" "kube-public")

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

note() {
    printf '%s\n' "$*" >&2
}

usage() {
    cat >&2 <<USAGE
Usage: $(basename "$0") <output-kubeconfig-path> <namespace> [namespace...]

  <output-kubeconfig-path>  Where to write the generated kubeconfig.
  <namespace>...            Namespaces the server may read. Bind the same set
                            you will pass in ALLOWED_NAMESPACES.

Environment:
  TOKEN_DURATION  Requested token lifetime (default: ${TOKEN_DURATION}). The API
                  server may cap this; the effective expiry is reported.

Example:
  $(basename "$0") ~/.k8s-mcp/kubeconfig-k8s-mcp.yaml staging production
USAGE
    exit 1
}

# --------------------------------------------------------------- arguments

[ "$#" -ge 2 ] || usage

OUTPUT_PATH="$1"
shift
NAMESPACES=("$@")

for ns in "${NAMESPACES[@]}"; do
    [ -n "${ns}" ] || die "empty namespace argument"
    for refused in "${REFUSED_NAMESPACES[@]}"; do
        if [ "${ns}" = "${refused}" ]; then
            die "refusing to bind '${ns}'. The server strips it from ALLOWED_NAMESPACES at startup (REQ-008), so a binding here would grant access it will never use."
        fi
    done
    if ! printf '%s' "${ns}" | grep -Eq '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$'; then
        die "'${ns}' is not a valid namespace name (RFC 1123 label)."
    fi
done

# --------------------------------------------------------------- preflight

command -v kubectl >/dev/null 2>&1 || die "kubectl not found on PATH."

for manifest in namespace.yaml serviceaccount.yaml clusterrole.yaml \
                clusterrolebinding.yaml role.yaml rolebinding.yaml.template; do
    [ -f "${MANIFEST_DIR}/${manifest}" ] \
        || die "missing manifest: ${MANIFEST_DIR}/${manifest}"
done

kubectl cluster-info >/dev/null 2>&1 \
    || die "cannot reach a Kubernetes cluster. Check your current kubectl context: $(kubectl config current-context 2>/dev/null || echo 'none set')."

# TokenRequest (1.24+). The legacy path — a long-lived auto-mounted Secret — is
# deliberately not used, and serviceaccount.yaml sets
# automountServiceAccountToken: false to make that explicit.
kubectl create token --help >/dev/null 2>&1 \
    || die "'kubectl create token' unavailable. Kubernetes 1.24+ and a matching kubectl are required; this script does not fall back to legacy ServiceAccount token Secrets."

for ns in "${NAMESPACES[@]}"; do
    kubectl get namespace "${ns}" >/dev/null 2>&1 \
        || die "namespace '${ns}' does not exist. Create it first; this script binds permissions but does not create application namespaces."
done

note "Cluster context : $(kubectl config current-context)"
note "Namespaces      : ${NAMESPACES[*]}"

# ------------------------------------------------- cluster-level manifests

note ""
note "Applying cluster-level manifests..."
kubectl apply \
    -f "${MANIFEST_DIR}/namespace.yaml" \
    -f "${MANIFEST_DIR}/serviceaccount.yaml" \
    -f "${MANIFEST_DIR}/clusterrole.yaml" \
    -f "${MANIFEST_DIR}/clusterrolebinding.yaml" >&2 \
    || die "failed to apply cluster-level manifests."

# ----------------------------------------------- per-namespace manifests

for ns in "${NAMESPACES[@]}"; do
    note ""
    note "Binding namespace '${ns}'..."

    # role.yaml has no metadata.namespace by design; -n supplies it.
    kubectl apply -n "${ns}" -f "${MANIFEST_DIR}/role.yaml" >&2 \
        || die "failed to apply Role in namespace '${ns}'."

    # Render the template before applying; __NAMESPACE__ is not a valid name.
    if ! sed "s/__NAMESPACE__/${ns}/g" "${MANIFEST_DIR}/rolebinding.yaml.template" \
        | kubectl apply -f - >&2; then
        die "failed to apply RoleBinding in namespace '${ns}'."
    fi
done

# --------------------------------------------------------------- token

note ""
note "Requesting a ServiceAccount token (TokenRequest API)..."
TOKEN="$(kubectl create token "${SA_NAME}" \
            -n "${SA_NAMESPACE}" \
            --duration="${TOKEN_DURATION}" 2>/dev/null)" \
    || die "failed to create a token for ServiceAccount ${SA_NAMESPACE}/${SA_NAME}."
[ -n "${TOKEN}" ] || die "received an empty token from the API server."

# Report the expiry the API server actually granted, which may be shorter than
# TOKEN_DURATION if the cluster caps token lifetime. A silently-capped token
# that expires early is the failure mode this guards against.
if command -v python3 >/dev/null 2>&1; then
    EXPIRY="$(printf '%s' "${TOKEN}" | python3 -c '
import base64, datetime, json, sys
try:
    payload = sys.stdin.read().split(".")[1]
    payload += "=" * (-len(payload) % 4)
    exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
    print(datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
          .strftime("%Y-%m-%d %H:%M:%S UTC"))
except Exception:
    pass
' 2>/dev/null || true)"
    if [ -n "${EXPIRY:-}" ]; then
        note "Token expires    : ${EXPIRY} (requested ${TOKEN_DURATION})"
        note "                   Re-run this script before then; the server will"
        note "                   return kubernetes_api_error 401 once it lapses."
    fi
fi

# ------------------------------------------------- cluster connection info

CURRENT_CONTEXT="$(kubectl config current-context)"
CLUSTER_NAME="$(kubectl config view -o jsonpath="{.contexts[?(@.name=='${CURRENT_CONTEXT}')].context.cluster}")"
[ -n "${CLUSTER_NAME}" ] || die "could not determine the cluster for context '${CURRENT_CONTEXT}'."

SERVER="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='${CLUSTER_NAME}')].cluster.server}")"
[ -n "${SERVER}" ] || die "could not determine the API server URL for cluster '${CLUSTER_NAME}'."

CA_DATA="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='${CLUSTER_NAME}')].cluster.certificate-authority-data}")"
if [ -z "${CA_DATA}" ]; then
    CA_FILE="$(kubectl config view --raw -o jsonpath="{.clusters[?(@.name=='${CLUSTER_NAME}')].cluster.certificate-authority}")"
    if [ -n "${CA_FILE}" ] && [ -r "${CA_FILE}" ]; then
        CA_DATA="$(base64 < "${CA_FILE}" | tr -d '\n')"
    else
        die "cluster '${CLUSTER_NAME}' has no certificate authority data. Generating a kubeconfig without a CA would require disabling TLS verification, which this script will not do."
    fi
fi

# --------------------------------------------------------------- write out

OUTPUT_DIR="$(dirname "${OUTPUT_PATH}")"
mkdir -p "${OUTPUT_DIR}" || die "cannot create output directory '${OUTPUT_DIR}'."

# Create with restrictive permissions from the outset: the file holds a bearer
# token, so it must never exist world-readable, not even briefly.
umask 077
TMP_OUTPUT="$(mktemp "${OUTPUT_DIR}/.kubeconfig.XXXXXX")" \
    || die "cannot create a temporary file in '${OUTPUT_DIR}'."
trap 'rm -f "${TMP_OUTPUT}"' EXIT

cat > "${TMP_OUTPUT}" <<KUBECONFIG
apiVersion: v1
kind: Config
clusters:
  - name: ${CLUSTER_ENTRY}
    cluster:
      server: ${SERVER}
      certificate-authority-data: ${CA_DATA}
users:
  - name: ${SA_NAME}
    user:
      token: ${TOKEN}
contexts:
  - name: ${CONTEXT_NAME}
    context:
      cluster: ${CLUSTER_ENTRY}
      user: ${SA_NAME}
current-context: ${CONTEXT_NAME}
KUBECONFIG

chmod 600 "${TMP_OUTPUT}"
mv "${TMP_OUTPUT}" "${OUTPUT_PATH}" || die "cannot write '${OUTPUT_PATH}'."
trap - EXIT

# ------------------------------------------------------------ verification

note ""
note "Verifying the generated credentials..."
if ! KUBECONFIG="${OUTPUT_PATH}" kubectl auth can-i get pods \
        -n "${NAMESPACES[0]}" >/dev/null 2>&1; then
    note "warning: 'can-i get pods -n ${NAMESPACES[0]}' returned no. RBAC may not"
    note "         have propagated yet, or the binding did not take effect."
fi

if KUBECONFIG="${OUTPUT_PATH}" kubectl auth can-i get secrets \
        -n "${NAMESPACES[0]}" >/dev/null 2>&1; then
    die "the generated credentials can read Secrets in '${NAMESPACES[0]}'. That contradicts REQ-013 and section 3 of requirements.md. Refusing to hand over this kubeconfig; inspect the cluster for a pre-existing binding that over-grants ${SA_NAMESPACE}/${SA_NAME}."
fi

# If the operator wrote the kubeconfig into the repo, make sure git will not
# pick it up. The file contains a bearer token.
if git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    ABS_OUTPUT="$(cd "$(dirname "${OUTPUT_PATH}")" && pwd)/$(basename "${OUTPUT_PATH}")"
    case "${ABS_OUTPUT}" in
        "${REPO_ROOT}"/*)
            REL="${ABS_OUTPUT#"${REPO_ROOT}/"}"
            if ! git -C "${REPO_ROOT}" check-ignore -q "${REL}"; then
                note ""
                note "WARNING: ${REL} is inside the repository and is NOT covered by"
                note "         .gitignore. It contains a bearer token. Move it outside"
                note "         the repo or add a matching ignore rule before committing."
            fi
            ;;
    esac
fi

note ""
note "Done. Set ALLOWED_NAMESPACES to match the namespaces bound above:"
note "  export KUBECONFIG=${OUTPUT_PATH}"
note "  export ALLOWED_NAMESPACES=$(IFS=,; printf '%s' "${NAMESPACES[*]}")"
note ""

# REQ-014: the generated path is the only thing on stdout.
printf '%s\n' "${OUTPUT_PATH}"
