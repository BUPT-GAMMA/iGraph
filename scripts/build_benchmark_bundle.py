#!/usr/bin/env python3
"""Build a standalone benchmark bundle from an explicit path allowlist."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "benchmark-package.txt"
FORBIDDEN_TOP_LEVEL = frozenset(
    {
        ".env.example",
        "agent_server.py",
        "agent_runtime",
        "backend",
        "compose.yaml",
        "deploy",
        "frontend",
        "iGraph-feature-agentos-react-web",
        "mcp_service",
    }
)
FORBIDDEN_PATH_NAMES = frozenset(
    {
        *FORBIDDEN_TOP_LEVEL,
        ".env",
        "auth",
        "authentication",
        "login",
        "session",
        "sessions",
    }
)
IGNORED_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)


def _copy_ignore(_directory: str, names: list[str]) -> list[str]:
    return [
        name
        for name in names
        if name in IGNORED_DIRECTORY_NAMES or name.endswith((".pyc", ".pyo"))
    ]


def _forbidden_component(path: PurePosixPath) -> str | None:
    """Return the forbidden path component, including a file stem, if any."""

    for part in path.parts:
        folded = part.casefold()
        stem = PurePosixPath(part).stem.casefold()
        if folded in FORBIDDEN_PATH_NAMES or stem in FORBIDDEN_PATH_NAMES:
            return part
    return None


def _validate_allowlisted_source(relative: PurePosixPath, source: Path) -> None:
    """Reject symlinks and forbidden content anywhere in a recursive entry."""

    candidates = [source]
    if source.is_dir():
        candidates.extend(source.rglob("*"))
    for candidate in candidates:
        nested = relative
        if candidate != source:
            nested = relative.joinpath(
                *candidate.relative_to(source).parts
            )
        if candidate.is_symlink():
            raise ValueError(
                f"symbolic links are not allowed in benchmark bundle: {nested.as_posix()}"
            )
        forbidden = _forbidden_component(nested)
        if forbidden is not None:
            raise ValueError(
                "forbidden benchmark path component "
                f"{forbidden!r}: {nested.as_posix()}"
            )


def _validate_built_tree(root: Path) -> None:
    """Post-copy defense in depth for the final standalone tree."""

    for candidate in root.rglob("*"):
        relative = PurePosixPath(candidate.relative_to(root).as_posix())
        if candidate.is_symlink():
            raise ValueError(
                f"built benchmark bundle contains a symbolic link: {relative.as_posix()}"
            )
        forbidden = _forbidden_component(relative)
        if forbidden is not None:
            raise ValueError(
                "built benchmark bundle contains forbidden path component "
                f"{forbidden!r}: {relative.as_posix()}"
            )


def _manifest_entries() -> list[tuple[PurePosixPath, Path]]:
    entries: list[tuple[PurePosixPath, Path]] = []
    seen: set[PurePosixPath] = set()
    for line_number, raw_line in enumerate(
        MANIFEST_PATH.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        value = raw_line.strip()
        if not value or value.startswith("#"):
            continue
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"invalid manifest path on line {line_number}: {value!r}")
        if _forbidden_component(relative) is not None:
            raise ValueError(f"forbidden benchmark path on line {line_number}: {value!r}")
        if relative in seen:
            raise ValueError(f"duplicate manifest path on line {line_number}: {value!r}")

        declared_source = PROJECT_ROOT.joinpath(*relative.parts)
        if declared_source.is_symlink():
            raise ValueError(
                f"symbolic links are not allowed in benchmark manifest: {value!r}"
            )
        source = declared_source.resolve()
        if not source.is_relative_to(PROJECT_ROOT) or not source.exists():
            raise FileNotFoundError(f"manifest source does not exist: {relative.as_posix()}")
        _validate_allowlisted_source(relative, source)
        entries.append((relative, source))
        seen.add(relative)

    if not entries:
        raise ValueError("benchmark manifest is empty")
    return entries


def build_bundle(destination: Path) -> Path:
    """Copy the allowlisted benchmark into a new destination atomically."""

    destination = destination.expanduser().resolve()
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    entries = _manifest_entries()
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.building-",
            dir=destination.parent,
        )
    ).resolve()
    try:
        for relative, source in entries:
            if staging.is_relative_to(source):
                raise ValueError(
                    "destination cannot be nested inside an allowlisted source directory"
                )
            target = staging.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, target, ignore=_copy_ignore)
            else:
                shutil.copy2(source, target)
        launcher = staging / "run_benchmark.sh"
        launcher.chmod(
            launcher.stat().st_mode
            | stat.S_IXUSR
            | stat.S_IXGRP
            | stat.S_IXOTH
        )
        _validate_built_tree(staging)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return destination


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create a standalone iGraph benchmark directory containing only "
            "the allowlisted headless runtime files."
        )
    )
    parser.add_argument("destination", type=Path, help="new output directory")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        destination = build_bundle(args.destination)
    except Exception as exc:
        print(f"benchmark bundle error: {exc}", file=sys.stderr)
        return 2
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
