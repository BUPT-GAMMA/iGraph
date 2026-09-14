#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$SCRIPT_DIR"
EXPECTED_CONDA_ENV="${BENCHMARK_CONDA_ENV:-vlm}"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PYTHON="$PYTHON_BIN"
  if [[ ! -x "$PYTHON" ]]; then
    echo "benchmark error: PYTHON_BIN is not executable: $PYTHON" >&2
    exit 3
  fi
  export IGRAPH_BENCHMARK_LAUNCHER="python-bin-override"
  export PYTHONUNBUFFERED=1
  exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
fi

if [[ -n "${CONDA_PREFIX:-}" ]]; then
  ACTIVE_CONDA_ENV="${CONDA_DEFAULT_ENV:-$(basename -- "$CONDA_PREFIX")}"
  if [[ "$ACTIVE_CONDA_ENV" == */* ]]; then
    ACTIVE_CONDA_ENV="$(basename -- "$ACTIVE_CONDA_ENV")"
  fi
  if [[ "$ACTIVE_CONDA_ENV" == "$EXPECTED_CONDA_ENV" ]]; then
    PYTHON="$CONDA_PREFIX/bin/python"
    if [[ ! -x "$PYTHON" ]]; then
      echo "benchmark error: Python is not executable: $PYTHON" >&2
      exit 3
    fi
    export IGRAPH_BENCHMARK_LAUNCHER="active-conda-$EXPECTED_CONDA_ENV"
    export PYTHONUNBUFFERED=1
    exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
  fi
fi

CONDA_COMMAND=""
USER_HOME="${HOME:-}"
for CANDIDATE in \
  "${CONDA_EXE:-}" \
  "$(command -v conda 2>/dev/null || true)" \
  "$USER_HOME/miniconda3/bin/conda" \
  "$USER_HOME/anaconda3/bin/conda" \
  "/opt/conda/bin/conda"; do
  if [[ -n "$CANDIDATE" && -x "$CANDIDATE" ]]; then
    CONDA_COMMAND="$CANDIDATE"
    break
  fi
done

if [[ -z "$CONDA_COMMAND" ]]; then
  echo "benchmark error: cannot find conda; activate the '$EXPECTED_CONDA_ENV' environment first" >&2
  exit 3
fi

if ! CONDA_BASE="$("$CONDA_COMMAND" info --base 2>/dev/null)"; then
  echo "benchmark error: unable to locate the Conda base directory" >&2
  exit 3
fi
CONDA_SH="$CONDA_BASE/etc/profile.d/conda.sh"
if [[ ! -r "$CONDA_SH" ]]; then
  echo "benchmark error: Conda activation script is not readable: $CONDA_SH" >&2
  exit 3
fi

# Activating in this shell and then exec'ing Python preserves the runner's exact
# exit code. `conda run` collapses application failures to exit code 1.
# A caller may leave partial Conda markers behind after clearing CONDA_PREFIX;
# normalize that inconsistent state before sourcing Conda's shell integration.
if [[ -z "${CONDA_PREFIX:-}" ]]; then
  unset CONDA_DEFAULT_ENV CONDA_PROMPT_MODIFIER CONDA_SHLVL
fi
if ! source "$CONDA_SH"; then
  echo "benchmark error: unable to initialize Conda from $CONDA_SH" >&2
  exit 3
fi
if ! conda activate "$EXPECTED_CONDA_ENV"; then
  echo "benchmark error: unable to activate Conda environment '$EXPECTED_CONDA_ENV'" >&2
  exit 3
fi

PYTHON="$CONDA_PREFIX/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "benchmark error: Python is not executable after activation: $PYTHON" >&2
  exit 3
fi
export IGRAPH_BENCHMARK_LAUNCHER="conda-activate-$EXPECTED_CONDA_ENV"
export PYTHONUNBUFFERED=1
exec "$PYTHON" "$SCRIPT_DIR/main.py" "$@"
