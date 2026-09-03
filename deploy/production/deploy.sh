#!/usr/bin/env bash

set -Eeuo pipefail

image_tag="${1:?usage: deploy.sh <40-character git sha>}"
deploy_root="${DEPLOY_ROOT:-/home/keyuyang/apps/igraph}"
release_dir="${deploy_root}/releases/${image_tag}"
env_file="${deploy_root}/.env"
state_file="${deploy_root}/.deployed-image-tag"
current_link="${deploy_root}/current"
lock_file="${deploy_root}/.deploy.lock"

if [[ ! "${image_tag}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "IMAGE_TAG must be a 40-character lowercase git SHA: ${image_tag}" >&2
  exit 2
fi

for required_file in \
  "${env_file}" \
  "${release_dir}/compose.yaml" \
  "${release_dir}/compose.production.yaml"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Missing required deployment file: ${required_file}" >&2
    exit 2
  fi
done

export DOCKER_HOST="${DOCKER_HOST:-unix:///run/user/$(id -u)/docker.sock}"
export IMAGE_TAG="${image_tag}"
# Production is reachable only through the ngrok agent on this host. Keep the
# application off the LAN and keep this endpoint aligned with ngrok-igraph.service.
export FRONTEND_BIND_ADDRESS="127.0.0.1"
export FRONTEND_PORT="22108"

# Reconnecting CI clients may safely invoke this script again. Serialize those
# invocations so a previous SSH session that survived a network interruption
# cannot race a replacement session.
exec 9>"${lock_file}"
if ! flock --wait 900 9; then
  echo "Timed out waiting for another deployment to finish." >&2
  exit 1
fi

ln -sfn "${env_file}" "${release_dir}/.env"

compose=(
  docker compose
  --project-name igraph
  --env-file "${env_file}"
  -f "${release_dir}/compose.yaml"
  -f "${release_dir}/compose.production.yaml"
)
services=(frontend backend agent-runtime graph-engine-api graph-engine-mcp)
max_pull_attempts=6

previous_tag=""
previous_release=""
if [[ -f "${state_file}" ]]; then
  previous_tag="$(tr -d '[:space:]' < "${state_file}")"
fi
if [[ -L "${current_link}" ]]; then
  previous_release="$(readlink -f "${current_link}")"
fi

rollback() {
  local status=$?
  trap - ERR

  echo "Deployment of ${image_tag} failed." >&2
  if [[ "${previous_tag}" =~ ^[0-9a-f]{40}$ \
    && -n "${previous_release}" \
    && -f "${previous_release}/compose.yaml" \
    && -f "${previous_release}/compose.production.yaml" ]]; then
    echo "Rolling back to ${previous_tag}." >&2
    export IMAGE_TAG="${previous_tag}"
    local previous_compose=(
      docker compose
      --project-name igraph
      --env-file "${env_file}"
      -f "${previous_release}/compose.yaml"
      -f "${previous_release}/compose.production.yaml"
    )
    "${previous_compose[@]}" up -d --no-build --remove-orphans --wait --wait-timeout 600 || true
  else
    echo "No previously successful deployment is available for rollback." >&2
  fi
  exit "${status}"
}
trap rollback ERR

"${compose[@]}" config -q

pull_service() {
  local service="$1"
  local attempt
  local delay

  for ((attempt = 1; attempt <= max_pull_attempts; attempt++)); do
    echo "Pulling ${service} (attempt ${attempt}/${max_pull_attempts})."
    if "${compose[@]}" pull --quiet "${service}"; then
      return 0
    fi

    if ((attempt == max_pull_attempts)); then
      echo "Unable to pull ${service} after ${max_pull_attempts} attempts." >&2
      return 1
    fi

    delay=$((attempt * 10))
    echo "Pull of ${service} failed; retrying in ${delay}s." >&2
    sleep "${delay}"
  done
}

# Pull one service at a time so a constrained egress link is not shared by five
# concurrent downloads. Completed layers survive retries, and quiet mode avoids
# tens of thousands of duplicated progress lines in the GitHub Actions log.
for service in "${services[@]}"; do
  pull_service "${service}"
done

# The temporary public nginx is removed only after config validation and all
# immutable application images have been pulled successfully.
docker rm -f igraph-ngrok-check >/dev/null 2>&1 || true

"${compose[@]}" up -d --no-build --remove-orphans --wait --wait-timeout 600
curl --fail --silent --show-error \
  --retry 20 --retry-delay 3 --retry-all-errors \
  "http://${FRONTEND_BIND_ADDRESS}:${FRONTEND_PORT}/health" >/dev/null

printf '%s\n' "${image_tag}" > "${state_file}.tmp"
mv "${state_file}.tmp" "${state_file}"
ln -sfn "${release_dir}" "${current_link}"

trap - ERR
echo "Deployment ${image_tag} is healthy."
