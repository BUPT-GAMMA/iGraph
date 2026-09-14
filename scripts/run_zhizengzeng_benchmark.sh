#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RELEASE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
ORIGINAL_ARGUMENTS=("$@")

usage() {
  cat <<'EOF'
Usage:
  run_zhizengzeng_benchmark.sh [options] -- DATASET [DATASET ...]

Required options:
  --output-dir PATH
  --cpuset-cpus LIST
  --questions-per-type N
  --skills-dir PATH

Common options:
  --evaluate                    Enable TSA and SR evaluation.
  --replay-file PATH            Reuse saved attempts without invoking the agent.
  --recover-cleanup-failures    Recover only strictly validated cleanup-only failures.
  --reuse-successful-judge-results
                                Reuse successful Judge scores and retry only the rest.
  --agent-model NAME            Default: glm-5-turbo
  --judge-model NAME            Default: gpt-5-mini
  --model NAME                  Legacy fallback for both model roles; role-specific
                                options take precedence
  --categories CSV             Default: theory,retrieval,ml,insight,prediction
  --task-parallelism N          Default: 4
  --intra-task-parallelism N    Default: 1
  --seed N                      Default: 2223
  --container-cpus N            Optional Docker CFS quota (default: omitted)
  --container-memory LIMIT      Default: 24g
  --rootless-netns-pid PID      Enter this rootless dockerd user/net namespace
  --questions-root PATH         Or set IGRAPH_QUESTIONS_ROOT.
  --dataset-root PATH           Or set IGRAPH_DATASET_ROOT.
  --harness-root PATH           Or set IGRAPH_CODEX_HARNESS_ROOT.
  --provider-profile PATH       Or set IGRAPH_PROVIDER_PROFILE.
EOF
}

OUTPUT_DIR=""
CPUSET_CPUS=""
QUESTIONS_PER_TYPE=""
SKILLS_DIR=""
QUESTIONS_ROOT="${IGRAPH_QUESTIONS_ROOT:-}"
DATASET_ROOT="${IGRAPH_DATASET_ROOT:-}"
HARNESS_ROOT="${IGRAPH_CODEX_HARNESS_ROOT:-}"
PROVIDER_PROFILE="${IGRAPH_PROVIDER_PROFILE:-}"
MODEL=""
AGENT_MODEL=""
JUDGE_MODEL=""
CATEGORIES=(theory retrieval ml insight prediction)
IMAGE="sandbox_codex:latest"
TASK_PARALLELISM=4
INTRA_TASK_PARALLELISM=1
# Seed 2223 preserves every balanced cell while avoiding known multi-megabyte
# cycle-enumeration answers that are unsuitable for an LLM judge context.
SEED=2223
CONTAINER_CPUS=""
CONTAINER_MEMORY="24g"
ROOTLESS_NETNS_PID=""
AGENT_TIMEOUT=900
RUNNER_TIMEOUT=1200
JUDGE_TIMEOUT=120
RUNNER_JUDGE_TIMEOUT=600
EVALUATE=0
REPLAY_FILE=""
RECOVER_CLEANUP_FAILURES=0
REUSE_SUCCESSFUL_JUDGE_RESULTS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) OUTPUT_DIR="${2:?missing value for --output-dir}"; shift 2 ;;
    --cpuset-cpus) CPUSET_CPUS="${2:?missing value for --cpuset-cpus}"; shift 2 ;;
    --questions-per-type) QUESTIONS_PER_TYPE="${2:?missing value for --questions-per-type}"; shift 2 ;;
    --skills-dir) SKILLS_DIR="${2:?missing value for --skills-dir}"; shift 2 ;;
    --questions-root) QUESTIONS_ROOT="${2:?missing value for --questions-root}"; shift 2 ;;
    --dataset-root) DATASET_ROOT="${2:?missing value for --dataset-root}"; shift 2 ;;
    --harness-root) HARNESS_ROOT="${2:?missing value for --harness-root}"; shift 2 ;;
    --provider-profile) PROVIDER_PROFILE="${2:?missing value for --provider-profile}"; shift 2 ;;
    --model) MODEL="${2:?missing value for --model}"; shift 2 ;;
    --agent-model) AGENT_MODEL="${2:?missing value for --agent-model}"; shift 2 ;;
    --judge-model) JUDGE_MODEL="${2:?missing value for --judge-model}"; shift 2 ;;
    --categories)
      IFS=',' read -r -a CATEGORIES <<< "${2:?missing value for --categories}"
      shift 2
      ;;
    --image) IMAGE="${2:?missing value for --image}"; shift 2 ;;
    --task-parallelism) TASK_PARALLELISM="${2:?missing value for --task-parallelism}"; shift 2 ;;
    --intra-task-parallelism) INTRA_TASK_PARALLELISM="${2:?missing value for --intra-task-parallelism}"; shift 2 ;;
    --seed) SEED="${2:?missing value for --seed}"; shift 2 ;;
    --container-cpus) CONTAINER_CPUS="${2:?missing value for --container-cpus}"; shift 2 ;;
    --container-memory) CONTAINER_MEMORY="${2:?missing value for --container-memory}"; shift 2 ;;
    --rootless-netns-pid) ROOTLESS_NETNS_PID="${2:?missing value for --rootless-netns-pid}"; shift 2 ;;
    --agent-timeout-seconds) AGENT_TIMEOUT="${2:?missing value for --agent-timeout-seconds}"; shift 2 ;;
    --runner-timeout-seconds) RUNNER_TIMEOUT="${2:?missing value for --runner-timeout-seconds}"; shift 2 ;;
    --judge-timeout-seconds) JUDGE_TIMEOUT="${2:?missing value for --judge-timeout-seconds}"; shift 2 ;;
    --runner-judge-timeout-seconds) RUNNER_JUDGE_TIMEOUT="${2:?missing value for --runner-judge-timeout-seconds}"; shift 2 ;;
    --evaluate) EVALUATE=1; shift ;;
    --replay-file) REPLAY_FILE="${2:?missing value for --replay-file}"; shift 2 ;;
    --recover-cleanup-failures) RECOVER_CLEANUP_FAILURES=1; shift ;;
    --reuse-successful-judge-results) REUSE_SUCCESSFUL_JUDGE_RESULTS=1; shift ;;
    --help|-h) usage; exit 0 ;;
    --) shift; break ;;
    *) echo "benchmark launch error: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

DATASETS=("$@")
# ``--model`` is retained for existing launchers.  Explicit role-specific
# options win regardless of their order on the command line.
AGENT_MODEL="${AGENT_MODEL:-${MODEL:-glm-5-turbo}}"
JUDGE_MODEL="${JUDGE_MODEL:-${MODEL:-gpt-5-mini}}"

if [[ -n "$ROOTLESS_NETNS_PID" && ! "$ROOTLESS_NETNS_PID" =~ ^[1-9][0-9]*$ ]]; then
  echo "benchmark launch error: invalid --rootless-netns-pid value" >&2
  exit 2
fi
if [[ -n "$ROOTLESS_NETNS_PID" && "${IGRAPH_ROOTLESS_NETNS_ENTERED:-}" != "1" ]]; then
  for NAMESPACE_NAME in user net; do
    NAMESPACE_PATH="/proc/$ROOTLESS_NETNS_PID/ns/$NAMESPACE_NAME"
    if [[ ! -L "$NAMESPACE_PATH" || ! -r "$NAMESPACE_PATH" ]]; then
      echo "benchmark launch error: unavailable rootless namespace: $NAMESPACE_PATH" >&2
      exit 3
    fi
  done
  NSENTER_COMMAND="$(command -v nsenter || true)"
  ENV_COMMAND="$(command -v env || true)"
  BASH_COMMAND_PATH="$(command -v bash || true)"
  for REQUIRED_COMMAND in "$NSENTER_COMMAND" "$ENV_COMMAND" "$BASH_COMMAND_PATH"; do
    if [[ "$REQUIRED_COMMAND" != /* || ! -x "$REQUIRED_COMMAND" ]]; then
      echo "benchmark launch error: nsenter re-exec dependency is unavailable" >&2
      exit 3
    fi
  done
  exec "$NSENTER_COMMAND" -t "$ROOTLESS_NETNS_PID" -U -n --preserve-credentials \
    "$ENV_COMMAND" IGRAPH_ROOTLESS_NETNS_ENTERED=1 \
    "$BASH_COMMAND_PATH" "$0" "${ORIGINAL_ARGUMENTS[@]}"
fi

if [[ -z "$OUTPUT_DIR" || -z "$CPUSET_CPUS" || -z "$QUESTIONS_PER_TYPE" || -z "$SKILLS_DIR" || -z "$QUESTIONS_ROOT" || -z "$DATASET_ROOT" || -z "$HARNESS_ROOT" || -z "$PROVIDER_PROFILE" ]]; then
  echo "benchmark launch error: required option is missing" >&2
  usage >&2
  exit 2
fi
if [[ ${#DATASETS[@]} -eq 0 ]]; then
  echo "benchmark launch error: at least one dataset is required after --" >&2
  exit 2
fi
if [[ ${#CATEGORIES[@]} -eq 0 ]]; then
  echo "benchmark launch error: at least one category is required" >&2
  exit 2
fi
if (( RECOVER_CLEANUP_FAILURES == 1 )) && [[ -z "$REPLAY_FILE" ]]; then
  echo "benchmark launch error: --recover-cleanup-failures requires --replay-file" >&2
  exit 2
fi
if (( REUSE_SUCCESSFUL_JUDGE_RESULTS == 1 )); then
  if [[ -z "$REPLAY_FILE" ]] || (( EVALUATE != 1 )); then
    echo "benchmark launch error: --reuse-successful-judge-results requires --replay-file and --evaluate" >&2
    exit 2
  fi
fi
if [[ -n "$REPLAY_FILE" && ! -f "$REPLAY_FILE" ]]; then
  echo "benchmark launch error: replay file is unavailable: $REPLAY_FILE" >&2
  exit 3
fi
for CATEGORY in "${CATEGORIES[@]}"; do
  case "$CATEGORY" in
    theory|retrieval|ml|insight|prediction) ;;
    *) echo "benchmark launch error: invalid category: $CATEGORY" >&2; exit 2 ;;
  esac
done
if [[ ! "$CPUSET_CPUS" =~ ^[0-9]+([,-][0-9]+)*$ ]]; then
  echo "benchmark launch error: invalid --cpuset-cpus value" >&2
  exit 2
fi
for VALUE in "$QUESTIONS_PER_TYPE" "$TASK_PARALLELISM" "$INTRA_TASK_PARALLELISM" "$AGENT_TIMEOUT" "$RUNNER_TIMEOUT" "$JUDGE_TIMEOUT" "$RUNNER_JUDGE_TIMEOUT"; do
  if [[ ! "$VALUE" =~ ^[1-9][0-9]*$ ]]; then
    echo "benchmark launch error: positive integer option expected" >&2
    exit 2
  fi
done
if (( RUNNER_TIMEOUT <= AGENT_TIMEOUT )); then
  echo "benchmark launch error: runner timeout must exceed agent timeout" >&2
  exit 2
fi
if (( RUNNER_JUDGE_TIMEOUT <= JUDGE_TIMEOUT * 3 + 130 )); then
  echo "benchmark launch error: runner judge timeout is too short for retries" >&2
  exit 2
fi

PYTHON_COMMAND="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_COMMAND" && -n "${CONDA_PREFIX:-}" ]]; then
  PYTHON_COMMAND="$CONDA_PREFIX/bin/python"
fi
if [[ -z "$PYTHON_COMMAND" ]]; then
  PYTHON_COMMAND="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_COMMAND" || ! -x "$PYTHON_COMMAND" ]]; then
  echo "benchmark launch error: activate the vlm environment first" >&2
  exit 3
fi

json_argv() {
  "$PYTHON_COMMAND" -c 'import json,sys; print(json.dumps(sys.argv[1:], separators=(",", ":")))' "$@"
}

AGENT_COMMAND=(
  "$PYTHON_COMMAND" "$SCRIPT_DIR/codex_harness_adapter.py"
  --harness-root "$HARNESS_ROOT"
  --skills-dir "$SKILLS_DIR"
  --provider-profile "$PROVIDER_PROFILE"
  --model "$AGENT_MODEL"
  --image "$IMAGE"
  --timeout-seconds "$AGENT_TIMEOUT"
  --container-cpuset-cpus "$CPUSET_CPUS"
  --container-memory "$CONTAINER_MEMORY"
)
if [[ -n "$CONTAINER_CPUS" ]]; then
  AGENT_COMMAND+=(--container-cpus "$CONTAINER_CPUS")
fi
JUDGE_COMMAND=(
  "$PYTHON_COMMAND" "$SCRIPT_DIR/zhizengzeng_judge_adapter.py"
  --provider-profile "$PROVIDER_PROFILE"
  --model "$JUDGE_MODEL"
  --timeout-seconds "$JUDGE_TIMEOUT"
)
AGENT_COMMAND_JSON="$(json_argv "${AGENT_COMMAND[@]}")"
JUDGE_COMMAND_JSON="$(json_argv "${JUDGE_COMMAND[@]}")"

export CUDA_VISIBLE_DEVICES=""
export NVIDIA_VISIBLE_DEVICES="void"
export OMP_NUM_THREADS=2
export MKL_NUM_THREADS=2
export OPENBLAS_NUM_THREADS=2
export NUMEXPR_NUM_THREADS=2
export TOKENIZERS_PARALLELISM=false

RUN_ARGUMENTS=(
  questions
  --questions-root "$QUESTIONS_ROOT"
  --dataset-root "$DATASET_ROOT"
  --categories "${CATEGORIES[@]}"
  --datasets "${DATASETS[@]}"
  --questions-per-type "$QUESTIONS_PER_TYPE"
  --shuffle
  --seed "$SEED"
  --task-parallelism "$TASK_PARALLELISM"
  --intra-task-parallelism "$INTRA_TASK_PARALLELISM"
  --attempts-per-question 1
  --timeout-seconds "$RUNNER_TIMEOUT"
  --judge-timeout-seconds "$RUNNER_JUDGE_TIMEOUT"
  --output-dir "$OUTPUT_DIR"
)
if [[ -n "$REPLAY_FILE" ]]; then
  RUN_ARGUMENTS+=(--replay-file "$REPLAY_FILE")
  if (( RECOVER_CLEANUP_FAILURES == 1 )); then
    RUN_ARGUMENTS+=(--recover-cleanup-failures)
  fi
else
  RUN_ARGUMENTS+=(--agent-command-json "$AGENT_COMMAND_JSON")
fi
if (( EVALUATE == 1 )); then
  RUN_ARGUMENTS+=(--evaluate --judge-command-json "$JUDGE_COMMAND_JSON")
fi
if (( REUSE_SUCCESSFUL_JUDGE_RESULTS == 1 )); then
  RUN_ARGUMENTS+=(--reuse-successful-judge-results)
fi
if [[ -z "$REPLAY_FILE" && -n "${DOCKER_HOST:-}" ]]; then
  # The benchmark runner starts agents with an explicit environment allowlist.
  # Forward only the variable name; its socket URI never appears in argv/logs.
  RUN_ARGUMENTS+=(--agent-env DOCKER_HOST)
fi

exec taskset -c "$CPUSET_CPUS" nice -n 10 \
  bash "$RELEASE_ROOT/run_benchmark.sh" "${RUN_ARGUMENTS[@]}"
