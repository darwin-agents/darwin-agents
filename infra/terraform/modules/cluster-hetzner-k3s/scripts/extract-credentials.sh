#!/usr/bin/env bash
# Extract API server credentials from a kubeconfig as a JSON map.
#
# Called by Terraform's `data "external"` source. Reads the kubeconfig
# path + context name from stdin (JSON), prints the credentials as JSON
# on stdout, and exits 0. Any error message goes to stderr + non-zero
# exit; Terraform surfaces it.
#
# Output shape (all values are strings, base64-encoded for cert/key):
#   {
#     "host": "https://127.0.0.1:NNNNN",
#     "cluster_ca_certificate": "...",
#     "client_certificate": "...",
#     "client_key": "..."
#   }

set -euo pipefail

# Read query JSON from stdin.
input=$(cat)

kubeconfig=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin)["kubeconfig"])')
context=$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin)["context"])')

if [[ ! -f "$kubeconfig" ]]; then
    echo "kubeconfig not found at: $kubeconfig" >&2
    exit 1
fi

# Use kubectl --minify to drop everything but the current context's
# cluster + user, then read fields off [0]. This avoids fragile jsonpath
# selectors with quoted names that vary across kind versions.
host=$(kubectl --kubeconfig "$kubeconfig" --context "$context" \
    config view --raw --minify \
    --output 'jsonpath={.clusters[0].cluster.server}')

ca=$(kubectl --kubeconfig "$kubeconfig" --context "$context" \
    config view --raw --minify \
    --output 'jsonpath={.clusters[0].cluster.certificate-authority-data}')

cert=$(kubectl --kubeconfig "$kubeconfig" --context "$context" \
    config view --raw --minify \
    --output 'jsonpath={.users[0].user.client-certificate-data}')

key=$(kubectl --kubeconfig "$kubeconfig" --context "$context" \
    config view --raw --minify \
    --output 'jsonpath={.users[0].user.client-key-data}')

# Decode the base64 the kubeconfig stores so the provider receives
# PEMs (which is what the inline provider config wants).
ca_pem=$(printf '%s' "$ca" | base64 -d)
cert_pem=$(printf '%s' "$cert" | base64 -d)
key_pem=$(printf '%s' "$key" | base64 -d)

python3 -c '
import json, sys
print(json.dumps({
    "host": sys.argv[1],
    "cluster_ca_certificate": sys.argv[2],
    "client_certificate": sys.argv[3],
    "client_key": sys.argv[4],
}))
' "$host" "$ca_pem" "$cert_pem" "$key_pem"
