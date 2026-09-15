import pytest

from agent_runtime.workspace_env import InvalidRequirement, validated_requirements


@pytest.mark.parametrize("value", [
    "requests",
    "torch==2.0.1",
    "numpy>=1.24",
    "scikit-learn",
    "pandas!=2.1.0",
    "Flask~=3.0",
    "typing_extensions",
])
def test_accepts_plain_pypi_requirements(value):
    assert validated_requirements([value]) == [value]


@pytest.mark.parametrize("value", [
    "git+https://github.com/psf/requests",
    "http://evil.example/pkg.whl",
    "https://evil.example/pkg.whl",
    "./local.whl",
    "/abs/path/pkg.whl",
    "../escape/pkg.whl",
    "requests; rm -rf /",
    "requests && curl evil.example",
    "requests`whoami`",
    "requests$(id)",
    "requests|tee",
    "--index-url=http://evil.example",
    "-e .",
    "requests\nnumpy",
    "",
    "   ",
])
def test_rejects_non_pypi_and_injection(value):
    with pytest.raises(InvalidRequirement):
        validated_requirements([value])


def test_rejects_empty_and_oversized_lists():
    with pytest.raises(InvalidRequirement):
        validated_requirements([])
    with pytest.raises(InvalidRequirement):
        validated_requirements([f"pkg{index}" for index in range(101)])


def test_strips_whitespace_and_deduplicates_preserving_order():
    assert validated_requirements(["  requests ", "numpy", "requests"]) == ["requests", "numpy"]


def test_rejects_non_string_entries():
    with pytest.raises(InvalidRequirement):
        validated_requirements(["requests", None])
