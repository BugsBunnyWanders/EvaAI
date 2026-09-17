#!/usr/bin/env bash
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID is required}"
: "${REGION:?REGION is required}"

# Read JSON so annotation keys containing dots and slashes are parsed literally.
service="$(gcloud run services describe eva-action-executor \
  --project="${PROJECT_ID}" --region="${REGION}" --format=json)"
ingress="$(jq -r '.metadata.annotations["run.googleapis.com/ingress"] // ""' <<<"${service}")"
if [[ "${ingress}" != "internal" ]]; then
  echo "Action executor ingress is not private: ${ingress}" >&2
  exit 1
fi

policy="$(gcloud run services get-iam-policy eva-action-executor \
  --project="${PROJECT_ID}" --region="${REGION}" --format=json)"
if jq -e '.bindings[].members[]? | select(. == "allUsers")' <<<"${policy}" >/dev/null; then
  echo "Action executor must not grant allUsers invocation" >&2
  exit 1
fi
