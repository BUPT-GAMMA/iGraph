"""Keep sandbox descendants in the process group owned by the runtime."""

from __future__ import annotations

import ctypes
import errno
import tempfile
from contextlib import contextmanager
from functools import lru_cache


@lru_cache(maxsize=1)
def _process_group_filter() -> bytes:
    library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise OSError("Cannot initialize workspace seccomp filter")
    try:
        # The runtime creates the session before bwrap applies this filter.
        # Descendants may fork, but cannot escape its cancellation boundary.
        for name in (b"setsid", b"setpgid"):
            syscall = library.seccomp_syscall_resolve_name(name)
            if syscall < 0 or library.seccomp_rule_add(context, 0x00050000 | errno.EPERM, syscall, 0) < 0:
                raise OSError("Cannot restrict workspace process groups")
        with tempfile.TemporaryFile() as handle:
            if library.seccomp_export_bpf(context, handle.fileno()) < 0:
                raise OSError("Cannot export workspace seccomp filter")
            handle.seek(0)
            return handle.read()
    finally:
        library.seccomp_release(context)


@contextmanager
def process_group_filter():
    with tempfile.TemporaryFile() as handle:
        handle.write(_process_group_filter())
        handle.seek(0)
        yield handle.fileno()
