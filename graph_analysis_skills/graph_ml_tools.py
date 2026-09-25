"""Deterministic virtual graph-ML train/infer actions for benchmark agents."""

from __future__ import annotations

import inspect
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

import numpy as np


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmark_platform.virtual_ml import (  # noqa: E402
    GRAPH_CLASSIFICATION,
    LINK_PREDICTION,
    NODE_CLASSIFICATION,
    VIRTUAL_ML_BROKER_ENDPOINT_ENV,
    VIRTUAL_ML_BROKER_TOKEN_ENV,
    VIRTUAL_ML_BROKER_TOKEN_HEADER,
    virtual_label_for,
)


BROKER_TIMEOUT_SECONDS = 60.0
BROKER_MAX_RESPONSE_BYTES = 64 * 1024 * 1024


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


# Never pass the bearer token through a configured HTTP proxy and never follow
# a redirect away from the validated loopback endpoint.
_BROKER_OPENER = build_opener(ProxyHandler({}), _NoRedirectHandler())


class VirtualMLBrokerClientError(RuntimeError):
    def __init__(self, error_type: str, message: str, *, exit_code: int = 2) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.exit_code = 1 if exit_code == 1 else 2


def structural_node_embedding_classification_train(
    node_features, edge_index, labels, hidden_channels=16, epochs=100
):
    if not isinstance(node_features, np.ndarray) or not isinstance(edge_index, np.ndarray):
        raise TypeError("node_features and edge_index must be numpy arrays.")
    if len(node_features.shape) != 2:
        raise ValueError(
            f"node_features must be 2D (N x D). Current shape: {node_features.shape}"
        )

    num_nodes, in_channels = node_features.shape
    if len(edge_index.shape) != 2 or edge_index.shape[0] != 2:
        raise ValueError(
            f"edge_index must be in 2 x E format. Current shape: {edge_index.shape}"
        )

    labels = np.array(labels)
    if len(labels.shape) == 1:
        if len(labels) != num_nodes:
            raise ValueError(
                f"1D label length ({len(labels)}) must match the number of nodes ({num_nodes})."
            )
        num_classes = len(np.unique(labels))
    elif len(labels.shape) == 2:
        if labels.shape[0] != num_nodes:
            raise ValueError(
                "The number of samples in 2D labels "
                f"({labels.shape[0]}) must match the number of nodes ({num_nodes})."
            )
        num_classes = labels.shape[1]
    else:
        raise ValueError(
            "Unsupported label dimension! Must be 1D (class indices) or 2D "
            "(One-hot/mask matrix)."
        )

    checksum = in_channels * 1000 + hidden_channels * 10 + num_classes
    return {
        "task": "node_classification",
        "status": "trained",
        "in_channels": in_channels,
        "hidden_channels": hidden_channels,
        "out_channels": num_classes,
        "weights_checksum": checksum,
    }


def structural_node_embedding_classification_infer(
    model_state, node_features, edge_index
):
    if not isinstance(model_state, dict) or model_state.get("status") != "trained":
        raise ValueError("Inference failed: A trained model state must be provided.")
    if model_state.get("task") != "node_classification":
        raise TypeError(
            "Inference failed: Model task mismatch. Expected 'node_classification'."
        )

    in_channels = node_features.shape[1]
    if in_channels != model_state["in_channels"]:
        raise ValueError(
            "Feature dimension mismatch! Model expects "
            f"{model_state['in_channels']} dimensions, but got {in_channels}."
        )

    num_nodes = node_features.shape[0]
    out_channels = model_state["out_channels"]
    seed = num_nodes + in_channels + model_state["weights_checksum"]
    logits = np.random.RandomState(abs(seed) % (2**32)).uniform(
        size=(num_nodes, out_channels)
    )
    predictions = np.argmax(logits, axis=1)
    injected_label = virtual_label_for(NODE_CLASSIFICATION)
    if injected_label is not None:
        predictions[:] = injected_label
    return {"status": "success", "predictions": predictions.tolist()}


def structural_node_embedding_link_prediction_train(
    node_features, edge_index, hidden_channels=32, epochs=100
):
    if not isinstance(node_features, np.ndarray) or not isinstance(edge_index, np.ndarray):
        raise TypeError("node_features and edge_index must be numpy arrays.")
    if len(node_features.shape) != 2:
        raise ValueError("node_features must be a 2D matrix (N x D).")
    if len(edge_index.shape) != 2 or edge_index.shape[0] != 2:
        raise ValueError("edge_index must be in 2 x E format.")

    _num_nodes, in_channels = node_features.shape
    checksum = in_channels * 1000 + hidden_channels
    return {
        "task": "link_prediction",
        "status": "trained",
        "in_channels": in_channels,
        "hidden_channels": hidden_channels,
        "weights_checksum": checksum,
    }


def structural_node_embedding_link_prediction_infer(
    model_state, node_features, edge_index, query_edge_index
):
    if not isinstance(model_state, dict) or model_state.get("task") != "link_prediction":
        raise TypeError("Inference failed: Expected model state for 'link_prediction'.")
    if (
        not isinstance(query_edge_index, np.ndarray)
        or len(query_edge_index.shape) != 2
        or query_edge_index.shape[0] != 2
    ):
        raise ValueError("query_edge_index must be a numpy array in 2 x E_query format.")

    in_channels = node_features.shape[1]
    if in_channels != model_state["in_channels"]:
        raise ValueError(
            "Feature dimension mismatch! Model expects "
            f"{model_state['in_channels']} dimensions, but got {in_channels}."
        )

    num_query_edges = query_edge_index.shape[1]
    seed = num_query_edges + in_channels + model_state["weights_checksum"]
    probabilities = np.random.RandomState(abs(seed) % (2**32)).uniform(
        0, 1, size=(num_query_edges,)
    )
    predictions = (probabilities > 0.5).astype(int)
    injected_label = virtual_label_for(LINK_PREDICTION)
    if injected_label is not None:
        predictions[:] = injected_label
        probabilities[:] = 0.9 if injected_label == 1 else 0.1
    return {
        "status": "success",
        "num_edges_predicted": num_query_edges,
        "probabilities": probabilities.tolist(),
        "predictions": predictions.tolist(),
    }


def structural_graph_embedding_classification_train(
    node_features,
    edge_index,
    graph_labels,
    batch=None,
    hidden_channels=32,
    epochs=100,
):
    if not isinstance(node_features, np.ndarray) or not isinstance(edge_index, np.ndarray):
        raise TypeError("node_features and edge_index must be numpy arrays.")

    num_nodes, in_channels = node_features.shape
    if batch is None:
        batch = np.zeros(num_nodes, dtype=int)
    elif len(batch) != num_nodes:
        raise ValueError(
            f"The length of the batch vector ({len(batch)}) must equal the total "
            f"number of nodes ({num_nodes})."
        )
    num_graphs = len(np.unique(batch))

    graph_labels = np.array(graph_labels)
    if len(graph_labels.shape) == 1:
        if len(graph_labels) != num_graphs:
            raise ValueError(
                f"Label count error! Calculated {num_graphs} graphs from batch, but "
                f"received {len(graph_labels)} 1D graph labels."
            )
        num_classes = len(np.unique(graph_labels))
    elif len(graph_labels.shape) == 2:
        if graph_labels.shape[0] != num_graphs:
            raise ValueError(
                f"Label sample count error! Calculated {num_graphs} graphs from "
                "batch, but the 2D labels provide "
                f"{graph_labels.shape[0]} samples."
            )
        num_classes = graph_labels.shape[1]
    else:
        raise ValueError(
            "Unsupported graph label dimension! Must be 1D indices or a 2D matrix."
        )

    checksum = in_channels * 1000 + hidden_channels * 10 + num_classes
    return {
        "task": "graph_classification",
        "status": "trained",
        "in_channels": in_channels,
        "out_channels": num_classes,
        "pool_method": "global_mean_pool",
        "weights_checksum": checksum,
    }


def structural_graph_embedding_classification_infer(
    model_state, node_features, edge_index, batch=None
):
    if not isinstance(model_state, dict) or model_state.get("task") != "graph_classification":
        raise TypeError(
            "Inference failed: Expected model state for 'graph_classification'."
        )

    num_nodes = node_features.shape[0]
    if batch is None:
        batch = np.zeros(num_nodes, dtype=int)
    elif len(batch) != num_nodes:
        raise ValueError(
            "Inference failed: The length of the batch vector must match the number "
            "of nodes in node_features."
        )
    num_graphs = len(np.unique(batch))
    in_channels = node_features.shape[1]
    if in_channels != model_state["in_channels"]:
        raise ValueError(
            "Feature dimension mismatch! Model expects "
            f"{model_state['in_channels']} dimensions, but got {in_channels}."
        )

    out_channels = model_state["out_channels"]
    seed = num_graphs + in_channels + model_state["weights_checksum"]
    logits = np.random.RandomState(abs(seed) % (2**32)).uniform(
        size=(num_graphs, out_channels)
    )
    predictions = np.argmax(logits, axis=1)
    injected_label = virtual_label_for(GRAPH_CLASSIFICATION)
    if injected_label is not None:
        predictions[:] = injected_label
    return {
        "status": "success",
        "num_graphs_processed": num_graphs,
        "predictions": predictions.tolist(),
    }


ACTION_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "structural_node_embedding_classification_train": structural_node_embedding_classification_train,
    "structural_node_embedding_classification_infer": structural_node_embedding_classification_infer,
    "structural_node_embedding_link_prediction_train": structural_node_embedding_link_prediction_train,
    "structural_node_embedding_link_prediction_infer": structural_node_embedding_link_prediction_infer,
    "structural_graph_embedding_classification_train": structural_graph_embedding_classification_train,
    "structural_graph_embedding_classification_infer": structural_graph_embedding_classification_infer,
}

_ARRAY_ARGUMENTS = frozenset(
    {
        "node_features",
        "edge_index",
        "labels",
        "query_edge_index",
        "graph_labels",
        "batch",
    }
)


def _load_cli_value(raw_value: str) -> Any:
    if raw_value.endswith(".npy"):
        return np.load(raw_value)
    if raw_value.endswith(".json"):
        with open(raw_value, "r", encoding="utf-8") as handle:
            return json.load(handle)
    try:
        return json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return raw_value


def _parse_cli_arguments(arguments: list[str]) -> tuple[str, dict[str, Any]]:
    if not arguments:
        raise ValueError("an action is required")
    action = arguments[0]
    if action not in ACTION_HANDLERS:
        raise ValueError(f"unknown action: {action}")

    kwargs: dict[str, Any] = {}
    index = 1
    while index < len(arguments):
        option = arguments[index]
        if not option.startswith("--") or len(option) == 2:
            raise ValueError(f"invalid argument: {option}")
        if index + 1 >= len(arguments):
            raise ValueError(f"missing value for {option}")
        key = option[2:].replace("-", "_")
        if key in kwargs:
            raise ValueError(f"duplicate argument: {option}")
        value = _load_cli_value(arguments[index + 1])
        if key in _ARRAY_ARGUMENTS and isinstance(value, list):
            value = np.asarray(value)
        kwargs[key] = value
        index += 2

    try:
        inspect.signature(ACTION_HANDLERS[action]).bind(**kwargs)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    return action, kwargs


def _write_json(payload: Any, stream) -> None:
    stream.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    stream.write("\n")


def _broker_configuration(
    environment: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    values = os.environ if environment is None else environment
    endpoint = values.get(VIRTUAL_ML_BROKER_ENDPOINT_ENV, "").strip()
    token = values.get(VIRTUAL_ML_BROKER_TOKEN_ENV, "").strip()
    if not endpoint and not token:
        return None
    if not endpoint or not token:
        raise VirtualMLBrokerClientError(
            "BrokerConfigurationError",
            "both virtual-ML broker endpoint and token are required",
        )
    if len(token) > 256 or any(
        ord(character) < 33 or ord(character) > 126 for character in token
    ):
        raise VirtualMLBrokerClientError(
            "BrokerConfigurationError", "virtual-ML broker token is invalid"
        )

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise VirtualMLBrokerClientError(
            "BrokerConfigurationError", "virtual-ML broker endpoint is invalid"
        ) from exc
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/invoke"
        or port is None
    ):
        raise VirtualMLBrokerClientError(
            "BrokerConfigurationError", "virtual-ML broker endpoint is invalid"
        )
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        is_loopback = parsed.hostname == "localhost"
    if not is_loopback:
        raise VirtualMLBrokerClientError(
            "BrokerConfigurationError", "virtual-ML broker must use a loopback endpoint"
        )
    return endpoint, token


def _absolutize_broker_file_arguments(
    arguments: list[str], *, cwd: str | Path | None = None
) -> list[str]:
    """Resolve CLI file values before crossing into the runner process."""

    base = Path.cwd() if cwd is None else Path(cwd)
    base = base.resolve(strict=False)
    resolved = list(arguments)
    # CLI values occupy even positions after ``action, --option``.  Scanning
    # only value positions avoids mistaking action/option names for paths while
    # still leaving malformed argv for the authoritative broker to reject.
    for index in range(2, len(resolved), 2):
        value = resolved[index]
        if value.endswith((".npy", ".json")):
            candidate = Path(value)
            if not candidate.is_absolute():
                resolved[index] = str((base / candidate).resolve(strict=False))
    return resolved


def _read_broker_body(stream) -> bytes:
    body = stream.read(BROKER_MAX_RESPONSE_BYTES + 1)
    if len(body) > BROKER_MAX_RESPONSE_BYTES:
        raise VirtualMLBrokerClientError(
            "BrokerResponseTooLarge", "virtual-ML broker response exceeds size limit"
        )
    return body


def _decode_broker_response(body: bytes, *, http_status: int) -> Any:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VirtualMLBrokerClientError(
            "BrokerProtocolError", "virtual-ML broker returned invalid JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        raise VirtualMLBrokerClientError(
            "BrokerProtocolError", "virtual-ML broker returned an invalid envelope"
        )
    if payload["ok"] is True:
        if http_status != 200 or "result" not in payload:
            raise VirtualMLBrokerClientError(
                "BrokerProtocolError", "virtual-ML broker returned an invalid success envelope"
            )
        return payload["result"]

    error = payload.get("error")
    if not isinstance(error, dict):
        raise VirtualMLBrokerClientError(
            "BrokerProtocolError", "virtual-ML broker returned an invalid error envelope"
        )
    error_type = error.get("type", "VirtualMLBrokerError")
    message = error.get("message", "virtual-ML broker rejected the request")
    if not isinstance(error_type, str) or not isinstance(message, str):
        raise VirtualMLBrokerClientError(
            "BrokerProtocolError", "virtual-ML broker returned an invalid error envelope"
        )
    exit_code = payload.get("exit_code", 2)
    raise VirtualMLBrokerClientError(
        error_type[:128], message[:2048], exit_code=exit_code
    )


def _invoke_virtual_ml_broker(
    arguments: list[str], endpoint: str, token: str
) -> Any:
    forwarded_arguments = _absolutize_broker_file_arguments(arguments)
    try:
        body = json.dumps(
            {"argv": forwarded_arguments},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise VirtualMLBrokerClientError(
            "BrokerRequestError", "virtual-ML arguments are not strict JSON"
        ) from exc
    request = Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/json",
            VIRTUAL_ML_BROKER_TOKEN_HEADER: token,
            "Cache-Control": "no-store",
            "Connection": "close",
        },
        method="POST",
    )
    try:
        with _BROKER_OPENER.open(request, timeout=BROKER_TIMEOUT_SECONDS) as response:
            response_body = _read_broker_body(response)
            status = response.status
    except HTTPError as exc:
        try:
            response_body = _read_broker_body(exc)
            status = exc.code
        finally:
            exc.close()
    except (URLError, TimeoutError, OSError) as exc:
        raise VirtualMLBrokerClientError(
            "BrokerConnectionError", "virtual-ML broker is unavailable"
        ) from exc
    return _decode_broker_response(response_body, http_status=status)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    try:
        broker_configuration = _broker_configuration()
    except VirtualMLBrokerClientError as exc:
        _write_json(
            {
                "status": "error",
                "error": {"type": exc.error_type, "message": str(exc)},
            },
            sys.stderr,
        )
        return exc.exit_code

    if broker_configuration is not None:
        endpoint, token = broker_configuration
        try:
            result = _invoke_virtual_ml_broker(arguments, endpoint, token)
            _write_json(result, sys.stdout)
            return 0
        except VirtualMLBrokerClientError as exc:
            _write_json(
                {
                    "status": "error",
                    "error": {"type": exc.error_type, "message": str(exc)},
                },
                sys.stderr,
            )
            return exc.exit_code

    try:
        action, kwargs = _parse_cli_arguments(arguments)
    except Exception as exc:
        _write_json(
            {"status": "error", "error": {"type": type(exc).__name__, "message": str(exc)}},
            sys.stderr,
        )
        return 2

    try:
        result = ACTION_HANDLERS[action](**kwargs)
        _write_json(result, sys.stdout)
        return 0
    except Exception as exc:
        _write_json(
            {"status": "error", "error": {"type": type(exc).__name__, "message": str(exc)}},
            sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
