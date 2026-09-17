from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_portable_compute_stack_is_default_and_gpu_stack_is_available():
    cpu = (ROOT / "deploy/requirements/compute-cpu.txt").read_text(encoding="utf-8")
    gpu = (ROOT / "deploy/requirements/compute-gpu.txt").read_text(encoding="utf-8")
    assert "--extra-index-url https://download.pytorch.org/whl/cpu" in cpu
    assert "torch==2.12.1+cpu" in cpu
    assert "--extra-index-url https://download.pytorch.org/whl/cu130" in gpu
    assert "torch==2.14.0+cu130" in gpu
    assert "torchvision==0.29.0+cu130" in gpu
    assert "torch-geometric==2.8.0" in cpu and "torch-geometric==2.8.0" in gpu

    for dockerfile in ("agent-runtime.Dockerfile", "graph-data.Dockerfile"):
        text = (ROOT / "deploy/docker" / dockerfile).read_text(encoding="utf-8")
        assert "ARG COMPUTE_REQUIREMENTS=deploy/requirements/compute-cpu.txt" in text
        assert "COPY ${COMPUTE_REQUIREMENTS} /tmp/compute.txt" in text

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    gpu_compose = (ROOT / "compose.gpu.yaml").read_text(encoding="utf-8")
    assert "nvidia.com/gpu=all" not in compose
    assert gpu_compose.count("nvidia.com/gpu=all") == 2
    assert gpu_compose.count("deploy/requirements/compute-gpu.txt") == 2


def test_runtime_instructions_use_preinstalled_torch_and_probe_optional_cuda():
    text = (ROOT / "agent_runtime/instructions.py").read_text(encoding="utf-8")
    tool_text = (ROOT / "agent_runtime/workspace_tools.py").read_text(encoding="utf-8")
    for value in ("CUDA", "torch", "python Uploaded/script.py"):
        assert value in text
        assert value in tool_text
