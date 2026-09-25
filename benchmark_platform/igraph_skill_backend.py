"""Headless benchmark tool backend for the project's own iGraph Agent.

The product Agent normally discovers tools from Graph Data's MCP service.  The
benchmark corpus, however, defines its public tool names in the standalone
``graph_analysis_skills`` directory.  This module provides the small subset of
the :class:`mcp_service.raw_client.RawMCPClient` protocol used by ``IGraph`` and
executes those *actual* skill scripts without a shell.

The backend is deliberately attempt scoped.  It knows only the selected
dataset, the attempt workspace, and the skill directory supplied by the
runner.  It never receives a question file or ground truth.  In particular,
the six graph-ML actions inherit the runner's opaque loopback broker variables;
the benchmark runner therefore remains the only holder of the expected label
and the authoritative recorder of ML calls.
"""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping, Sequence

from .tool_names import CANONICAL_TOOL_GROUPS
from .virtual_ml import (
    VIRTUAL_ML_BROKER_ENDPOINT_ENV,
    VIRTUAL_ML_BROKER_TOKEN_ENV,
)


_SCRIPT_BY_GROUP = {
    "retrieval": "graph_retrieval_tools.py",
    "io": "graph_io_tools.py",
    "theory": "graph_theory_tools.py",
    "insight": "graph_insight_tools.py",
    "ml": "graph_ml_tools.py",
    "solver": "graph_task_solver.py",
}
_GRAPH_FUNCTION_GROUPS = frozenset({"theory", "insight"})
_ML_ACTION_FAMILIES = {
    "structural_node_embedding_classification": "node_classification",
    "structural_node_embedding_link_prediction": "link_prediction",
    "structural_graph_embedding_classification": "graph_classification",
}
_DATASET_ARGUMENTS = frozenset(
    {"filepath", "file_path", "dataset", "dataset_path", "graph_file", "path"}
)
_RUNTIME_ENVIRONMENT_NAMES = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "LD_LIBRARY_PATH",
        "LIBRARY_PATH",
        "VIRTUAL_ENV",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "CUDA_VISIBLE_DEVICES",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        VIRTUAL_ML_BROKER_ENDPOINT_ENV,
        VIRTUAL_ML_BROKER_TOKEN_ENV,
    }
)
_ERROR_MARKERS = (
    "error:",
    "unknown action:",
    "traceback (most recent call last)",
    "indentationerror:",
    '"ok": false',
    '"success": false',
)
_SAFE_ARGUMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_DATASET_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_FILE_SUFFIXES = frozenset({".pt", ".pth", ".pkl", ".gpickle", ".gml", ".graphml", ".npy", ".json"})
_RESERVED_WORKSPACE_FILES = frozenset(
    {
        "task.json",
        "result.json",
        "questions.json",
        "ground_truth.json",
        "virtual_ml_context.json",
    }
)


class BenchmarkSkillError(RuntimeError):
    """Raised for an invalid backend setup, never for a normal tool failure."""


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _strict_json_value(value: Any) -> Any:
    """Return a detached JSON value and reject NaN/Infinity early."""

    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    return json.loads(encoded, parse_constant=lambda raw: (_ for _ in ()).throw(ValueError(raw)))


def _annotation_text(node: ast.expr | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node).lower()
    except Exception:
        return ""


def _property_schema(name: str, annotation: ast.expr | None) -> dict[str, Any]:
    annotation_text = _annotation_text(annotation)
    lowered = name.lower()
    schema: dict[str, Any] = {}
    if "bool" in annotation_text or lowered.startswith(("is_", "has_", "include_", "exclude_")):
        schema["type"] = "boolean"
    elif "float" in annotation_text or any(token in lowered for token in ("percent", "ratio", "weight", "threshold")):
        schema["type"] = "number"
    elif "int" in annotation_text or lowered.endswith(("_id", "_idx", "_num", "_count", "_depth", "_radius", "_degree")):
        schema["type"] = "integer"
    elif any(token in annotation_text for token in ("list", "set", "tuple", "sequence")) or lowered.endswith(("_list", "_nodes", "_edges")):
        schema["type"] = "array"
        schema["items"] = {}
    elif any(token in annotation_text for token in ("dict", "mapping")) or lowered.endswith(("_filters", "_config")):
        schema["type"] = "object"
    elif lowered in _DATASET_ARGUMENTS or lowered.endswith(("_file", "_path")):
        schema["type"] = "string"
    else:
        # Many skill functions accept either a scalar node id or a JSON list.
        # Omitting ``type`` preserves that real Python interface instead of
        # lying to the model with an over-constrained schema.
        schema = {}
    schema["description"] = f"Argument `{name}` passed to the canonical skill action."
    return schema


def _literal_default(node: ast.expr | None) -> tuple[bool, Any]:
    if node is None:
        return False, None
    try:
        value = ast.literal_eval(node)
        _strict_json_value(value)
        return True, value
    except Exception:
        return False, None


def _function_schema(script: Path, action: str, *, omit_first: bool) -> tuple[str, dict[str, Any]]:
    """Build an MCP schema from the checked-in server skill function itself."""

    try:
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
    except (OSError, UnicodeError, SyntaxError) as exc:
        raise BenchmarkSkillError(f"cannot inspect skill script {script.name}: {type(exc).__name__}") from exc

    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == action
        ),
        None,
    )
    if function is None:
        raise BenchmarkSkillError(f"canonical action {action!r} is missing from {script.name}")

    positional = [*function.args.posonlyargs, *function.args.args]
    positional_defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(function.args.defaults)
    ) + list(function.args.defaults)
    parameters: list[tuple[ast.arg, ast.expr | None]] = list(zip(positional, positional_defaults))
    parameters.extend(zip(function.args.kwonlyargs, function.args.kw_defaults))
    if omit_first and parameters:
        parameters = parameters[1:]

    properties: dict[str, Any] = {}
    required: list[str] = []
    for parameter, default_node in parameters:
        name = parameter.arg
        if not _SAFE_ARGUMENT_NAME.fullmatch(name):
            continue
        property_schema = _property_schema(name, parameter.annotation)
        has_default, default = _literal_default(default_node)
        if has_default:
            property_schema["default"] = default
        else:
            required.append(name)
        properties[name] = property_schema

    description = ast.get_docstring(function, clean=True) or (
        f"Execute the canonical graph benchmark action `{action}` using {script.name}."
    )
    description = " ".join(description.split())[:2000]
    return description, {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": function.args.kwarg is not None,
    }


def _fixed_schema(action: str) -> tuple[str, dict[str, Any]]:
    if action in {"inspect_pt_with_pyg", "inspect_pt_without_pyg"}:
        properties: dict[str, Any] = {
            "filepath": {
                "type": "string",
                "description": "Optional attempt-local graph file; defaults to the selected benchmark dataset.",
            }
        }
        required: list[str] = []
    elif action in {
        "extract_key_with_pyg",
        "extract_key_without_pyg",
        "extract_key_to_numpy_with_pyg",
        "extract_key_to_numpy_without_pyg",
    }:
        properties = {
            "key_name": {"type": "string", "description": "Dataset key to extract."},
            "filepath": {
                "type": "string",
                "description": "Optional attempt-local graph file; defaults to the selected benchmark dataset.",
            },
        }
        required = ["key_name"]
    elif action == "extract_graph_by_id_with_pyg":
        properties = {
            "key_name": {"type": "string", "description": "Key containing the graph list."},
            "graph_id": {"type": "integer", "minimum": 0, "description": "Zero-based graph index."},
            "filepath": {
                "type": "string",
                "description": "Optional attempt-local graph file; defaults to the selected benchmark dataset.",
            },
        }
        required = ["key_name", "graph_id"]
    elif action == "graph_task_solver":
        properties = {
            "source": {"type": "integer", "description": "Source node id."},
            "target": {"type": "integer", "description": "Optional target node id."},
            "compare": {"type": "integer", "description": "Optional comparison node id."},
            "top_percent": {"type": "number", "default": 5.0},
            "radius": {"type": "integer", "default": 3, "minimum": 1},
            "pretty": {"type": "boolean", "default": False},
            "filepath": {
                "type": "string",
                "description": "Optional attempt-local graph file; defaults to the selected benchmark dataset.",
            },
        }
        required = ["source"]
    else:
        raise BenchmarkSkillError(f"no fixed schema for action {action!r}")
    return (
        f"Execute canonical benchmark action `{action}` against the selected attempt dataset.",
        {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    )


def _virtual_ml_schema(action: str) -> tuple[str, dict[str, Any]]:
    """Expose the corpus' virtual train/infer contract to the self Agent.

    The canonical implementation has a NumPy-level Python signature, which is
    appropriate for code-running agents but impossible to call through JSON
    with a 500 MB feature matrix.  This attempt-local MCP surface binds the
    selected dataset and supplies neutral, tiny shape-valid arrays internally;
    the authoritative runner broker still executes and records the real
    canonical action and remains the only process that can see the target
    label.
    """

    family = next(
        (prefix for prefix in _ML_ACTION_FAMILIES if action.startswith(prefix + "_")),
        None,
    )
    if family is None:
        raise BenchmarkSkillError(f"unknown canonical virtual-ML action: {action!r}")
    is_train = action.endswith("_train")
    if is_train:
        properties: dict[str, Any] = {
            "hidden_channels": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional virtual model width.",
            },
            "epochs": {
                "type": "integer",
                "minimum": 1,
                "description": "Optional virtual training epoch count.",
            },
        }
        description = (
            f"Train the canonical virtual {_ML_ACTION_FAMILIES[family]} model on the "
            "current attempt dataset. The dataset and array inputs are already bound; "
            "do not pass feature arrays or file paths. Call this train action before "
            "the matching infer action."
        )
    else:
        if family.endswith("link_prediction"):
            properties = {
                "source_node": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional source node identifier for result context.",
                },
                "target_node": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional target node identifier for result context.",
                },
            }
        elif family.endswith("node_embedding_classification"):
            properties = {
                "node_id": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional node identifier for result context.",
                }
            }
        else:
            properties = {
                "graph_idx": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional graph index for result context.",
                }
            }
        description = (
            f"Infer with the trained canonical virtual {_ML_ACTION_FAMILIES[family]} "
            "model for the current attempt. The dataset, arrays, and model state are "
            "bound automatically. Call the matching train action first."
        )
    return description, {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }


def _last_json_document(stdout: str) -> Any | None:
    """Extract a trailing JSON value from noisy legacy CLI output."""

    decoder = json.JSONDecoder(parse_constant=lambda raw: (_ for _ in ()).throw(ValueError(raw)))
    candidate: Any | None = None
    for index, character in enumerate(stdout):
        if character not in '[{"-0123456789tfn':
            continue
        try:
            value, end = decoder.raw_decode(stdout, index)
        except (json.JSONDecodeError, ValueError):
            continue
        if not stdout[end:].strip():
            candidate = value
    return candidate


class BenchmarkSkillClient:
    """Async, attempt-local client implementing IGraph's MCP client surface."""

    def __init__(
        self,
        skills_dir: Path,
        dataset_path: Path,
        environment: Mapping[str, str] | None = None,
        *,
        work_dir: Path | None = None,
        python_executable: str | Path | None = None,
        timeout_seconds: float = 300.0,
        max_output_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.skills_dir = Path(skills_dir).expanduser().resolve()
        self.dataset_path = Path(dataset_path).expanduser().resolve()
        self.work_dir = Path(work_dir or Path.cwd()).expanduser().resolve()
        self.python_executable = str(python_executable or sys.executable)
        self.timeout_seconds = float(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        if not self.skills_dir.is_dir():
            raise FileNotFoundError("graph analysis skill directory is unavailable")
        if not self.dataset_path.is_file():
            raise FileNotFoundError("benchmark dataset path is unavailable")
        if not self.work_dir.is_dir():
            raise FileNotFoundError("benchmark attempt workspace is unavailable")
        if self.timeout_seconds <= 0 or self.max_output_bytes <= 0:
            raise ValueError("skill timeout and output limit must be positive")

        supplied_environment = dict(os.environ if environment is None else environment)
        self._environment = {
            name: str(value)
            for name, value in supplied_environment.items()
            if name in _RUNTIME_ENVIRONMENT_NAMES and value is not None
        }
        # The broker-aware graph_ml_tools.py imports benchmark_platform in its
        # own subprocess.  The adapter's sys.path changes are process-local, so
        # explicitly make this release root importable while preserving any
        # product-runtime PYTHONPATH needed to unpickle dataset classes.
        release_root = str(Path(__file__).resolve().parents[1])
        python_path_parts = [release_root]
        existing_python_path = self._environment.get("PYTHONPATH", "")
        if existing_python_path:
            python_path_parts.extend(
                part
                for part in existing_python_path.split(os.pathsep)
                if part and part != release_root
            )
        self._environment["PYTHONPATH"] = os.pathsep.join(python_path_parts)
        # Force deterministic, unbuffered text output without forwarding any
        # LLM provider credential to tool subprocesses.
        self._environment["PYTHONUNBUFFERED"] = "1"
        self._scripts: dict[str, Path] = {}
        self._groups: dict[str, str] = {}
        self._ml_model_states: dict[str, dict[str, Any]] = {}
        self._ml_lock = asyncio.Lock()
        self._tool_specs = self._build_specs()
        self._verify_virtual_ml_transport()
        self._opened = False

    def _script(self, filename: str) -> Path:
        candidates = (self.skills_dir / filename, self.skills_dir / "scripts" / filename)
        script = next((path.resolve() for path in candidates if path.is_file()), None)
        if script is None or not _inside(script, self.skills_dir):
            raise FileNotFoundError(f"canonical skill script is missing: {filename}")
        return script

    def _build_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for group, actions in CANONICAL_TOOL_GROUPS.items():
            filename = _SCRIPT_BY_GROUP.get(group)
            if filename is None:
                continue
            script = self._script(filename)
            for action in actions:
                action_script = script
                if group in _GRAPH_FUNCTION_GROUPS:
                    try:
                        description, input_schema = _function_schema(
                            action_script,
                            action,
                            omit_first=group in _GRAPH_FUNCTION_GROUPS,
                        )
                    except BenchmarkSkillError:
                        if group != "insight" or action != "simulate_edge_perturbation":
                            raise
                        action_script = (
                            Path(__file__).resolve().parents[1]
                            / "graph_analysis_skills"
                            / "graph_insight_extensions.py"
                        ).resolve()
                        if not action_script.is_file():
                            raise FileNotFoundError(
                                "bundled simulate_edge_perturbation skill is unavailable"
                            )
                        description, input_schema = _function_schema(
                            action_script,
                            action,
                            omit_first=True,
                        )
                    if group in _GRAPH_FUNCTION_GROUPS:
                        input_schema["properties"]["graph_file"] = {
                            "type": "string",
                            "description": "Optional attempt-local graph file; defaults to the selected benchmark dataset.",
                        }
                elif group == "ml":
                    # Still parse the real source so a missing/renamed
                    # canonical handler fails at startup, then expose the
                    # attempt-bound virtual JSON contract used by this Agent.
                    _function_schema(action_script, action, omit_first=False)
                    description, input_schema = _virtual_ml_schema(action)
                else:
                    description, input_schema = _fixed_schema(action)
                self._scripts[action] = action_script
                self._groups[action] = group
                specs.append(
                    {
                        "name": action,
                        "description": description,
                        "inputSchema": input_schema,
                    }
                )
        return specs

    def _verify_virtual_ml_transport(self) -> None:
        """Refuse an old label-injection script when the broker is active.

        A historic copy of ``graph_ml_tools.py`` read expected labels from
        process environment variables.  Passing broker variables to that file
        would look successful while completely bypassing the runner's
        authoritative trace.  A formal benchmark must fail closed instead.
        """

        endpoint = self._environment.get(VIRTUAL_ML_BROKER_ENDPOINT_ENV, "")
        token = self._environment.get(VIRTUAL_ML_BROKER_TOKEN_ENV, "")
        if bool(endpoint) != bool(token):
            raise BenchmarkSkillError(
                "virtual-ML broker endpoint and token must be configured together"
            )
        if not endpoint:
            return
        script = self._scripts.get(CANONICAL_TOOL_GROUPS["ml"][0])
        if script is None:
            raise BenchmarkSkillError("canonical graph-ML script is unavailable")
        try:
            source = script.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise BenchmarkSkillError("cannot verify graph-ML broker support") from exc
        broker_markers = (
            "VIRTUAL_ML_BROKER_ENDPOINT_ENV",
            "VIRTUAL_ML_BROKER_TOKEN_ENV",
            "_invoke_virtual_ml_broker",
        )
        if not all(marker in source for marker in broker_markers):
            raise BenchmarkSkillError(
                "graph_ml_tools.py does not implement the attempt-scoped virtual-ML broker"
            )

    async def open(self) -> None:
        self._opened = True

    async def close(self) -> None:
        self._opened = False
        self._ml_model_states.clear()

    async def ensure_initialized(self) -> None:
        await self.open()

    async def list_tools(self) -> list[dict[str, Any]]:
        if not self._opened:
            await self.open()
        return copy.deepcopy(self._tool_specs)

    def _resolve_attempt_file(self, raw: Any) -> Path:
        if raw is None or str(raw).strip() == "":
            return self.dataset_path
        candidate = Path(str(raw)).expanduser()
        if not candidate.is_absolute():
            candidate = self.work_dir / candidate
        resolved = candidate.resolve()
        if resolved == self.dataset_path:
            return resolved
        if not _inside(resolved, self.work_dir):
            raise ValueError("tool file arguments must stay inside the attempt workspace")
        if resolved.name.casefold() in _RESERVED_WORKSPACE_FILES:
            raise ValueError("benchmark metadata files are not valid tool inputs")
        if resolved.suffix.lower() not in _FILE_SUFFIXES:
            raise ValueError("tool file argument has an unsupported suffix")
        return resolved

    def _validate_arguments(self, action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            raise TypeError("tool arguments must be a JSON object")
        clean = _strict_json_value(dict(arguments))
        if not isinstance(clean, dict):
            raise TypeError("tool arguments must be a JSON object")
        if any(not _SAFE_ARGUMENT_NAME.fullmatch(str(name)) for name in clean):
            raise ValueError("tool argument name is invalid")
        spec = next(item for item in self._tool_specs if item["name"] == action)
        schema = spec["inputSchema"]
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            unknown = set(clean).difference(properties)
            if unknown:
                raise ValueError(f"unexpected tool arguments: {sorted(unknown)}")
        missing = [name for name in schema.get("required", []) if name not in clean]
        if missing:
            raise ValueError(f"missing required tool arguments: {missing}")
        return clean

    def _graph_command(self, action: str, arguments: dict[str, Any]) -> list[str]:
        graph_file = self._resolve_attempt_file(
            arguments.pop("graph_file", arguments.pop("filepath", None))
        )
        return [
            self.python_executable,
            str(self._scripts[action]),
            action,
            str(graph_file),
            json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        ]

    def _retrieval_or_io_command(self, action: str, arguments: dict[str, Any]) -> list[str]:
        filepath = self._resolve_attempt_file(arguments.pop("filepath", None))
        command = [self.python_executable, str(self._scripts[action]), action, str(filepath)]
        if "key_name" in arguments:
            key_name = str(arguments.pop("key_name"))
            if not _SAFE_DATASET_KEY.fullmatch(key_name):
                raise ValueError("key_name must be a safe dataset field name")
            command.append(key_name)
        if "graph_id" in arguments:
            graph_id = arguments.pop("graph_id")
            if isinstance(graph_id, bool) or not isinstance(graph_id, int) or graph_id < 0:
                raise ValueError("graph_id must be a non-negative integer")
            command.append(str(graph_id))
        if arguments:
            raise ValueError(f"unexpected positional tool arguments: {sorted(arguments)}")
        return command

    def _solver_command(self, action: str, arguments: dict[str, Any]) -> list[str]:
        filepath = self._resolve_attempt_file(arguments.pop("filepath", None))
        command = [self.python_executable, str(self._scripts[action]), str(filepath)]
        option_names = {
            "source": "--source",
            "target": "--target",
            "compare": "--compare",
            "top_percent": "--top-percent",
            "radius": "--radius",
        }
        for name, option in option_names.items():
            if name in arguments:
                command.extend((option, str(arguments.pop(name))))
        if bool(arguments.pop("pretty", False)):
            command.append("--pretty")
        if arguments:
            raise ValueError(f"unexpected solver arguments: {sorted(arguments)}")
        return command

    @staticmethod
    def _ml_family(action: str) -> tuple[str, str]:
        for prefix, family in _ML_ACTION_FAMILIES.items():
            if action == prefix + "_train":
                return prefix, family
            if action == prefix + "_infer":
                return prefix, family
        raise ValueError("unknown virtual-ML action")

    @staticmethod
    def _append_cli_argument(command: list[str], name: str, value: Any) -> None:
        command.extend(
            (
                "--" + name,
                value
                if isinstance(value, str)
                else json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
            )
        )

    def _ml_command(self, action: str, arguments: dict[str, Any]) -> list[str]:
        prefix, family = self._ml_family(action)
        endpoint = self._environment.get(VIRTUAL_ML_BROKER_ENDPOINT_ENV, "")
        token = self._environment.get(VIRTUAL_ML_BROKER_TOKEN_ENV, "")
        if not endpoint or not token:
            raise ValueError("attempt-scoped virtual-ML broker is unavailable")
        arrays: dict[str, Any] = {
            "node_features": [[0.0], [0.0]],
            "edge_index": [[0, 1], [1, 0]],
            "node_labels": [0, 1],
            "query_edge_index": [[0], [1]],
            "graph_labels": [0],
            "batch": [0, 0],
        }
        command = [self.python_executable, str(self._scripts[action]), action]
        self._append_cli_argument(command, "node_features", arrays["node_features"])
        self._append_cli_argument(command, "edge_index", arrays["edge_index"])
        if action.endswith("_train"):
            # A failed retrain must not leave a prior usable model behind.
            self._ml_model_states.pop(prefix, None)
            if family == "node_classification":
                self._append_cli_argument(command, "labels", arrays["node_labels"])
            elif family == "graph_classification":
                self._append_cli_argument(command, "graph_labels", arrays["graph_labels"])
                self._append_cli_argument(command, "batch", arrays["batch"])
            for name in ("hidden_channels", "epochs"):
                if name in arguments:
                    self._append_cli_argument(command, name, arguments[name])
        else:
            model_state = self._ml_model_states.get(prefix)
            if model_state is None:
                raise ValueError("matching virtual-ML train action must succeed before infer")
            self._append_cli_argument(command, "model_state", model_state)
            if family == "link_prediction":
                self._append_cli_argument(
                    command, "query_edge_index", arrays["query_edge_index"]
                )
            elif family == "graph_classification":
                self._append_cli_argument(command, "batch", arrays["batch"])
        return command

    def _remember_virtual_ml_state(self, action: str, result: Mapping[str, Any]) -> None:
        if not action.endswith("_train") or not result.get("success"):
            return
        payload = result.get("result")
        prefix, family = self._ml_family(action)
        if not isinstance(payload, Mapping):
            return
        if payload.get("status") != "trained" or payload.get("task") != family:
            return
        self._ml_model_states[prefix] = _strict_json_value(dict(payload))

    def _command(self, action: str, arguments: dict[str, Any]) -> list[str]:
        group = self._groups[action]
        if group in _GRAPH_FUNCTION_GROUPS:
            return self._graph_command(action, arguments)
        if group in {"retrieval", "io"}:
            return self._retrieval_or_io_command(action, arguments)
        if group == "solver":
            return self._solver_command(action, arguments)
        if group == "ml":
            return self._ml_command(action, arguments)
        raise BenchmarkSkillError(f"unsupported canonical tool group: {group}")

    def _run(self, command: Sequence[str]) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                list(command),
                cwd=self.work_dir,
                env=self._environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                check=False,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": {"type": "TimeoutExpired", "message": "canonical skill action timed out"},
            }
        except OSError as exc:
            return {
                "success": False,
                "error": {"type": type(exc).__name__, "message": "canonical skill process could not start"},
            }

        stdout_bytes = completed.stdout or b""
        stderr_bytes = completed.stderr or b""
        truncated = len(stdout_bytes) > self.max_output_bytes or len(stderr_bytes) > self.max_output_bytes
        stdout = stdout_bytes[-self.max_output_bytes :].decode("utf-8", errors="replace")
        stderr = stderr_bytes[-self.max_output_bytes :].decode("utf-8", errors="replace")
        # These opaque values are never useful to the model or report.  Keep a
        # trusted skill's accidental environment diagnostic from persisting
        # broker credentials in the Agent trace.
        for name in (VIRTUAL_ML_BROKER_ENDPOINT_ENV, VIRTUAL_ML_BROKER_TOKEN_ENV):
            secret = self._environment.get(name, "")
            if secret:
                stdout = stdout.replace(secret, "[REDACTED]")
                stderr = stderr.replace(secret, "[REDACTED]")
        lowered = (stdout + "\n" + stderr).lower()
        failed = completed.returncode != 0 or any(marker in lowered for marker in _ERROR_MARKERS)
        parsed = _last_json_document(stdout)
        if failed:
            return {
                "success": False,
                "exit_code": int(completed.returncode),
                "error": {
                    "type": "SkillActionFailed",
                    "message": (stderr.strip() or stdout.strip() or "canonical skill action failed")[-4000:],
                },
                "output_truncated": truncated,
            }
        result: dict[str, Any] = {
            "success": True,
            "exit_code": int(completed.returncode),
            "result": parsed if parsed is not None else stdout.strip(),
        }
        if truncated:
            result["output_truncated"] = True
        return _strict_json_value(result)

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not self._opened:
            await self.open()
        action = str(name or "").strip()
        if action not in self._scripts:
            return {
                "success": False,
                "error": {"type": "UnknownTool", "message": "unknown canonical benchmark tool"},
            }
        if self._groups[action] == "ml":
            # Serialize the pair so a single model response containing train
            # then infer cannot race the cached model state.
            async with self._ml_lock:
                try:
                    clean = self._validate_arguments(action, arguments or {})
                    command = self._command(action, clean)
                except (TypeError, ValueError, OSError) as exc:
                    return {
                        "success": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
                    }
                result = await asyncio.to_thread(self._run, command)
                self._remember_virtual_ml_state(action, result)
                return result
        try:
            clean = self._validate_arguments(action, arguments or {})
            command = self._command(action, clean)
        except (TypeError, ValueError, OSError) as exc:
            return {
                "success": False,
                "error": {"type": type(exc).__name__, "message": str(exc)[:1000]},
            }
        return await asyncio.to_thread(self._run, command)


def install_benchmark_skill_client(
    runtime: Any,
    *,
    skills_dir: Path,
    dataset_path: Path,
    environment: Mapping[str, str] | None = None,
    work_dir: Path | None = None,
    timeout_seconds: float = 300.0,
) -> BenchmarkSkillClient:
    """Install an attempt-local canonical backend before the first Agent turn."""

    agent = getattr(runtime, "_agent", runtime)
    if getattr(agent, "_mcp_connected", False):
        raise RuntimeError("benchmark skill client must be installed before MCP initialization")
    client = BenchmarkSkillClient(
        skills_dir=skills_dir,
        dataset_path=dataset_path,
        environment=environment,
        work_dir=work_dir,
        timeout_seconds=timeout_seconds,
    )
    agent._mcp_client = client
    agent._mcp_connected = False
    agent._mcp_loop = None
    agent._tool_specs = []
    agent._visible_tool_specs = []
    agent._mcp_visible_tool_specs = []
    agent._tool_map = {}
    agent._tool_selector = None
    return client


__all__ = [
    "BenchmarkSkillClient",
    "BenchmarkSkillError",
    "install_benchmark_skill_client",
]
