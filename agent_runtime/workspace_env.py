"""Server-side Python environment provisioning for session workspaces.

uv runs here, outside the bubblewrap sandbox, because the sandbox has no
network namespace and no CA bundle. The resulting virtual environment is
mounted read-only into the sandbox, so package downloads never happen from
LLM-authored shell commands.
"""

from __future__ import annotations

import re

PYPI_INDEX_URL = "https://pypi.org/simple"
MAX_REQUIREMENTS = 100

# Name plus optional PEP 440 version constraints. Deliberately narrow: no
# URLs, no local paths, no extras, no environment markers, no option flags.
_REQUIREMENT = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
    r"(?:(?:==|>=|<=|~=|!=|>|<)[A-Za-z0-9][A-Za-z0-9._*+!-]*)*$"
)


class InvalidRequirement(ValueError):
    """A requested package is not a plain PyPI name/version specifier."""


def validated_requirements(packages: list[str]) -> list[str]:
    """Normalise a requested package list, rejecting anything but PyPI names.

    Only bare names with optional PEP 440 version constraints pass. VCS refs,
    URLs, local paths, option flags and shell metacharacters are refused so
    that a requirement string can never redirect the download or reach the
    shell.
    """
    if not isinstance(packages, list) or not packages:
        raise InvalidRequirement("请提供至少一个依赖包名")
    if len(packages) > MAX_REQUIREMENTS:
        raise InvalidRequirement(f"依赖数量不能超过 {MAX_REQUIREMENTS} 个")
    seen: list[str] = []
    for item in packages:
        if not isinstance(item, str):
            raise InvalidRequirement("依赖包名必须是字符串")
        value = item.strip()
        if not value:
            raise InvalidRequirement("依赖包名不能为空")
        if not _REQUIREMENT.match(value):
            raise InvalidRequirement(
                f"依赖 {value!r} 无效：只支持 PyPI 包名与版本约束"
                "（如 torch==2.0.1），不支持 URL、git 源、本地路径或命令"
            )
        if value not in seen:
            seen.append(value)
    return seen
