#!/usr/bin/env bash
# Invoked by the forced SSH receiver, with its process lock already held.
set -Eeuo pipefail
umask 077
sha=${1:?commit SHA is required}
source_dir=${2:?source directory is required}
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || exit 2
active="$HOME/apps/igraph"
state="$HOME/apps/igraph-deploy"
project=igraph-kyy
export DOCKER_HOST="unix:///run/user/$(id -u)/docker.sock"
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
[[ -f "$active/.env" && -f "$active/host-config/compose.host.yaml" ]] || exit 2
mkdir -p "$state/backups"
backup=$(mktemp -d "$state/backups/${sha:0:12}.XXXXXXXX")
cp -p "$active/.env" "$backup/previous.env"
rsync -a --exclude=.env --exclude=host-config --exclude=.git "$active/" "$backup/source/"
mkdir -p "$source_dir/host-config"
# Reuse large, administrator-provisioned offline dependencies without copying
# logs/secrets into the Docker build context. Hard links stay on this filesystem.
for item in compose.host.yaml agent-runtime.offline.Dockerfile graph-data.offline.Dockerfile mcp.offline.Dockerfile wheelhouse dev-wheels runtime-fixes; do
    if [[ -e "$active/host-config/$item" ]]; then
        cp -al "$active/host-config/$item" "$source_dir/host-config/$item"
    fi
done
cp "$active/.env" "$source_dir/.env"
python3 - "$source_dir/.env" "$sha" <<'PY'
import sys
from pathlib import Path
path, sha = Path(sys.argv[1]), sys.argv[2]
images = {'IGRAPH_GRAPH_ENGINE_IMAGE':'graph-engine', 'IGRAPH_GRAPH_ENGINE_MCP_IMAGE':'mcp',
          'IGRAPH_AGENT_RUNTIME_IMAGE':'runtime', 'IGRAPH_BACKEND_IMAGE':'backend', 'IGRAPH_FRONTEND_IMAGE':'frontend'}
lines = [line for line in path.read_text().splitlines() if line.partition('=')[0] not in images]
lines += [f'{key}=igraph-kyy-{name}:main-{sha}' for key, name in images.items()]
path.write_text('\n'.join(lines) + '\n')
path.chmod(0o600)
PY
compose() {
    local directory=$1
    shift
    docker compose --project-directory "$directory" --env-file "$directory/.env" -p "$project" \
        -f "$directory/compose.yaml" -f "$directory/host-config/compose.host.yaml" "$@"
}
compose "$source_dir" config --quiet
printf 'Building main commit %s; existing containers remain running.\n' "$sha"
compose "$source_dir" build
# Back up the database before the new version can perform migrations. Application
# files and graph datasets remain in their existing named volumes throughout.
compose "$active" exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$backup/postgres.sql.gz"
changed=0
rollback() {
    result=$?
    trap - ERR
    if (( changed )); then
        echo 'Deployment failed; restoring the previous application images and source.' >&2
        rsync -a --delete --exclude=.env --exclude=host-config --exclude=.git "$backup/source/" "$active/"
        cp "$backup/previous.env" "$active/.env"
        compose "$active" up -d --no-build --pull never --wait --wait-timeout 300 || true
    fi
    exit "$result"
}
trap rollback ERR
changed=1
compose "$source_dir" up -d --no-build --pull never --wait --wait-timeout 300
# Test static assets as well as the health route (the latter alone misses 403s).
compose "$source_dir" exec -T frontend wget -q -O /dev/null http://127.0.0.1/
compose "$source_dir" exec -T backend python - <<'PY'
import urllib.request
for url in ('http://127.0.0.1:21109/health', 'http://agent-runtime:21112/internal/health',
            'http://graph-engine-api:21111/health'):
    with urllib.request.urlopen(url, timeout=10) as r:
        assert r.status == 200, url
PY
# Keep the established systemd WorkingDirectory valid after a reboot.
rsync -a --delete --exclude=.env --exclude=host-config --exclude=.git "$source_dir/" "$active/"
cp "$source_dir/.env" "$active/.env"
printf '%s\n' "$sha" > "$active/host-config/deployed-main-commit.txt"
printf '%s\n' "$sha" > "$state/current-commit.txt"
trap - ERR
compose "$source_dir" ps --format '{{.Service}} {{.State}} {{.Health}}'
printf 'Successfully deployed main commit %s\n' "$sha"
