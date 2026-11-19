#!/usr/bin/env bash
# Checks rendered manifests against the Kubernetes schema (kubeconform) and the
# Kyverno policies in policies/kyverno, then proves the policies still reject
# tests/policy/insecure-workload.yaml. Tools run from digest-pinned images.
# Usage: scripts/validate-manifests.sh [RENDERED_DIR]   (default: ./rendered)
set -euo pipefail

chart="$(cd "$(dirname "$0")/.." && pwd)"
rendered="${1:-$chart/rendered}"

KYVERNO=ghcr.io/kyverno/kyverno-cli:v1.20.0@sha256:bd2b8b7c9d550ecf04e0bacaf351adce2f82ce67fd3f291075edcee7f8ab4ab4
KUBECONFORM=ghcr.io/yannh/kubeconform:v0.8.0@sha256:faffaf43f95aa6425306e1ab8d6fcad72acb9049158f38e574c085ea1ec0f64e
# Schemas of the custom resources the chart renders (HTTPRoute, TCPRoute, ServiceMonitor,
# Certificate), from the community catalog at a pinned commit. A kind without a schema fails.
CRD_SCHEMAS_REF=89c736bdde38b5fecd0f8436ec5f3aa3679a3b86
# The oldest Kubernetes version Chart.yaml declares (kubeVersion), and a current one.
KUBERNETES_VERSIONS=(1.27.0 1.37.0)

kyverno() {
  docker run --rm -v "$chart/policies/kyverno:/policies:ro" -v "$chart/tests/policy:/tests:ro" \
    -v "$(realpath "$rendered"):/rendered:ro" "$KYVERNO" "$@"
}

status=0
for file in "$rendered"/*.yaml; do
  name="$(basename "$file")"
  if kyverno apply /policies --resource "/rendered/$name" >/dev/null 2>&1; then
    echo "policies: pass  $name"
  else
    echo "policies: FAIL  $name" >&2
    kyverno apply /policies --resource "/rendered/$name" >&2 || true
    status=1
  fi
done

if kyverno apply /policies --resource /tests/insecure-workload.yaml >/dev/null 2>&1; then
  echo "policies: FAIL  insecure-workload.yaml was accepted; the policies have stopped working" >&2
  status=1
else
  echo "policies: pass  insecure-workload.yaml is still rejected"
fi

mapfile -t files < <(basename -a "$rendered"/*.yaml)
for version in "${KUBERNETES_VERSIONS[@]}"; do
  echo "schemas: Kubernetes $version"
  docker run --rm -v "$(realpath "$rendered"):/rendered:ro" "$KUBECONFORM" \
    -strict -summary -kubernetes-version "$version" \
    -schema-location default \
    -schema-location "https://raw.githubusercontent.com/datreeio/CRDs-catalog/$CRD_SCHEMAS_REF/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json" \
    "${files[@]/#//rendered/}" || status=1
done

exit "$status"
