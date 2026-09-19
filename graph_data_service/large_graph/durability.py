"""Crash-durable publication helpers for immutable large-graph objects.

Writing a complete staging tree and renaming it makes publication atomic, but
it is not by itself durable across a machine crash.  The immutable files and
their directory entries must reach stable storage before the rename, and the
rename must then be persisted in its parent directory.

These helpers deliberately keep only one file descriptor open at a time and
never read file contents into Python memory.  Published object trees are small
in directory count (partition data lives in files), so the pending-directory
stack is bounded by the object layout rather than by graph size.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat
from pathlib import Path


_UNSUPPORTED_DIRECTORY_SYNC_ERRORS = {
    errno.EINVAL,
    # POSIX defines EBADF for fsync on a descriptor not opened for writing;
    # directories generally cannot be opened O_RDWR, so this is a directory-
    # sync capability result after the descriptor has been fstat-verified.
    errno.EBADF,
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
    getattr(errno, "ENOSYS", errno.EINVAL),
}

_UNSUPPORTED_NON_POSIX_DIRECTORY_OPEN_ERRORS = {
    errno.EACCES,
    errno.EPERM,
    *_UNSUPPORTED_DIRECTORY_SYNC_ERRORS,
}

_UNSUPPORTED_NON_POSIX_DIRECTORY_SYNC_ERRORS = {
    *_UNSUPPORTED_NON_POSIX_DIRECTORY_OPEN_ERRORS,
}


def _open_for_sync(path: Path, *, directory: bool) -> int:
    before = os.stat(path, follow_symlinks=False)
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    object_name = "目录" if directory else "普通文件"
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"发布树中禁止符号链接: {path}")
    if not expected_type(before.st_mode):
        raise ValueError(f"发布树中只允许{object_name}: {path}")

    # POSIX permits implementations to reject fsync on a regular descriptor
    # that was not opened for writing.  These files were just created by the
    # publisher, so use O_RDWR for portable file syncing while directories
    # necessarily remain O_RDONLY.
    flags = os.O_RDONLY if directory else os.O_RDWR
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not expected_type(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValueError(f"发布对象在同步期间被替换: {path}")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def fsync_directory(path: str | Path) -> None:
    """Persist directory entries when the platform/filesystem supports it.

    Linux and the other POSIX platforms used in production support opening a
    directory and calling ``fsync`` on it.  Some non-POSIX platforms and a few
    filesystems explicitly report that directory syncing is unsupported; only
    those capability errors are ignored.  Ordinary I/O failures still abort
    publication.
    """

    directory = Path(path)
    try:
        descriptor = _open_for_sync(directory, directory=True)
    except OSError as exc:
        if (
            exc.errno in _UNSUPPORTED_DIRECTORY_SYNC_ERRORS
            or (
                os.name != "posix"
                and exc.errno in _UNSUPPORTED_NON_POSIX_DIRECTORY_OPEN_ERRORS
            )
        ):
            return
        raise
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            unsupported = (
                _UNSUPPORTED_DIRECTORY_SYNC_ERRORS
                if os.name == "posix"
                else _UNSUPPORTED_NON_POSIX_DIRECTORY_SYNC_ERRORS
            )
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(descriptor)


def _fsync_regular_file(path: Path) -> None:
    descriptor = _open_for_sync(path, directory=False)
    try:
        # File syncing is mandatory.  Unlike directory syncing, an unsupported
        # or failed file fsync means the immutable object is not safe to expose.
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_publish_tree(root: str | Path) -> None:
    """Synchronise every immutable file and directory below ``root``.

    Directory entries are inspected with ``follow_symlinks=False`` and files
    are opened with ``O_NOFOLLOW`` where available.  One scan and one file
    descriptor are active at a time; graph-sized files are never read.
    """

    tree_root = Path(root)
    if tree_root.is_symlink() or not tree_root.is_dir():
        raise ValueError(f"发布 staging 必须是实际目录: {tree_root}")

    pending = [tree_root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink():
            raise ValueError(f"发布树中禁止符号链接目录: {directory}")
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_path = Path(entry.path)
                if entry.is_symlink():
                    raise ValueError(f"发布树中禁止符号链接: {entry_path}")
                if entry.is_file(follow_symlinks=False):
                    _fsync_regular_file(entry_path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry_path)
                else:
                    raise ValueError(f"发布树中只允许普通文件和目录: {entry_path}")
        # Persist this directory's current names.  Child contents are synced
        # when their directory is popped from ``pending``.
        fsync_directory(directory)


def fsync_directory_chain(path: str | Path, *, through: str | Path) -> None:
    """Persist directory entries from ``path`` through a managed ancestor.

    A staging tree can be nested below several directories created by the same
    job (lease scope, graph and version).  Syncing only the staging parent does
    not make those ancestor names durable.  The bottom-up walk ensures every
    link needed to reach the published object survives a crash.
    """

    directory = Path(path)
    boundary = Path(through)
    try:
        directory.relative_to(boundary)
    except ValueError as exc:
        raise ValueError("目录同步边界必须是发布目录的祖先") from exc

    current = directory
    while True:
        fsync_directory(current)
        if current == boundary:
            return
        current = current.parent


def durable_publish_tree(
    staging: str | Path,
    final: str | Path,
    *,
    durability_root: str | Path | None = None,
) -> None:
    """Atomically and crash-durably publish an immutable staging directory."""

    staging_path = Path(staging)
    final_path = Path(final)
    if staging_path.parent != final_path.parent:
        raise ValueError("原子发布要求 staging 与 final 位于同一目录")
    if os.path.lexists(final_path):
        raise FileExistsError(f"不可覆盖已发布的不可变对象: {final_path}")

    # First make every file and staging directory entry durable.  Syncing the
    # parent before rename also persists the staging name itself.  A managed
    # durability root additionally covers ancestor directories created by this
    # job, without needlessly syncing arbitrary filesystem ancestors.
    fsync_publish_tree(staging_path)
    if durability_root is None:
        fsync_directory(staging_path.parent)
    else:
        fsync_directory_chain(staging_path.parent, through=durability_root)
    os.replace(staging_path, final_path)
    # Persist removal of the private staging name and creation of final.
    fsync_directory(final_path.parent)


def remove_tree_and_fsync(path: str | Path) -> None:
    """Remove a private tree and persist its disappearance from the parent."""

    tree = Path(path)
    parent = tree.parent
    if os.path.lexists(tree):
        if tree.is_symlink() or not tree.is_dir():
            tree.unlink()
        else:
            shutil.rmtree(tree)
    if not parent.is_symlink() and parent.is_dir():
        fsync_directory(parent)
