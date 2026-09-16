from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cuda_compute_stack_is_shared_by_runtime_images():
    requirements = (ROOT / "deploy/requirements/compute-gpu.txt").read_text(encoding="utf-8")
    assert "--extra-index-url https://download.pytorch.org/whl/cu130" in requirements
    assert "torch==2.14.0+cu130" in requirements
    assert "torchvision==0.29.0+cu130" in requirements
    assert "torch-geometric==2.8.0" in requirements

    for dockerfile in ("agent-runtime.Dockerfile", "graph-data.Dockerfile"):
        text = (ROOT / "deploy/docker" / dockerfile).read_text(encoding="utf-8")
        assert "deploy/requirements/compute-gpu.txt" in text
        assert "/tmp/compute-gpu.txt" in text


def test_runtime_instructions_use_preinstalled_cuda_torch():
    text = (ROOT / "agent_runtime/instructions.py").read_text(encoding="utf-8")
    tool_text = (ROOT / "agent_runtime/workspace_tools.py").read_text(encoding="utf-8")
    for value in ("CUDA", "torch", "python Uploaded/script.py"):
        assert value in text
        assert value in tool_text
