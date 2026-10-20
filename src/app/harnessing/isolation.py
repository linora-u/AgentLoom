"""Mandatory inherited access restrictions for Harness children.

This is intentionally separate from optional Shell command validation. Unsupported
backends fail closed. The trusted native resource guardian stays outside policy.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import platform
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class IsolationUnavailable(RuntimeError):
    category = "unsupported_capability"


@dataclass(frozen=True)
class IsolationSpec:
    read_paths: tuple[Path, ...]
    write_paths: tuple[Path, ...]
    directory_paths: tuple[Path, ...] = ()

    def __post_init__(self):
        for name in ("read_paths", "write_paths", "directory_paths"):
            paths = tuple(Path(path).resolve(strict=True) for path in getattr(self, name))
            if any(path == Path("/") or (name == "read_paths" and path == Path.home()) for path in paths):
                raise ValueError("Isolation cannot grant an entire root or home directory")
            object.__setattr__(self, name, paths)

    def to_dict(self):
        return {"read_paths": [str(path) for path in self.read_paths], "write_paths": [str(path) for path in self.write_paths], "directory_paths": [str(path) for path in self.directory_paths]}


def preflight_isolation() -> dict:
    if sys.platform != "linux":
        raise IsolationUnavailable("Harness isolation requires a validated OS backend; this build has no validated macOS Seatbelt or Windows backend")
    if platform.machine() not in {"x86_64", "aarch64"}:
        raise IsolationUnavailable("Unsupported Landlock syscall architecture")
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 6:
        raise IsolationUnavailable("Harness isolation requires Landlock ABI 6+ for inherited filesystem, truncation and IPC/signal scopes")
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise IsolationUnavailable("Harness isolation requires libseccomp for metadata/namespace/credential escape denial")
    return {"backend": "landlock+seccomp", "abi": abi, "metadata_denial": True, "inherited": True}


class _Ruleset(ctypes.Structure):
    _fields_ = [("fs", ctypes.c_uint64), ("net", ctypes.c_uint64), ("scoped", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("access", ctypes.c_uint64), ("parent", ctypes.c_int32)]


class _Argument(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_uint), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]


def _checked(value, operation):
    if value < 0:
        raise IsolationUnavailable(f"{operation} failed: errno {ctypes.get_errno()}")
    return value


def _seccomp():
    library = ctypes.CDLL(ctypes.util.find_library("seccomp"), use_errno=True)
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    library.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_Argument)]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    policy = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not policy:
        raise IsolationUnavailable("Cannot allocate metadata syscall policy")
    try:
        # Landlock does not mediate these metadata operations. No Trial tool
        # needs to mutate ownership/permissions, ptrace its controller, acquire
        # namespaces or obtain pre-policy descriptors through another process.
        denied = ("chmod", "fchmod", "fchmodat", "fchmodat2", "chown", "fchown", "lchown", "fchownat",
                  "utime", "utimes", "futimesat", "utimensat", "setxattr", "lsetxattr", "fsetxattr",
                  "removexattr", "lremovexattr", "fremovexattr", "mount", "umount2", "pivot_root", "chroot",
                  "unshare", "setns", "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
                  "open_by_handle_at", "bpf", "io_uring_setup", "userfaultfd", "keyctl", "add_key", "request_key")
        for name in denied:
            number = library.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and library.seccomp_rule_add(policy, 0x50000 | errno.EPERM, number, 0) != 0:
                raise IsolationUnavailable("Cannot restrict syscall " + name)
        # libc falls back from clone3 only on ENOSYS; deny namespace flags on
        # clone while keeping ordinary Python/Node threads and child processes.
        clone3 = library.seccomp_syscall_resolve_name(b"clone3")
        if clone3 >= 0:
            _checked(library.seccomp_rule_add(policy, 0x50000 | errno.ENOSYS, clone3, 0), "clone3 policy")
        clone = library.seccomp_syscall_resolve_name(b"clone")
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000, 0x10000000, 0x20000000, 0x40000000):
            condition = _Argument(0, 7, flag, flag)  # SCMP_CMP_MASKED_EQ
            _checked(library.seccomp_rule_add_array(policy, 0x50000 | errno.EPERM, clone, 1, ctypes.byref(condition)), "namespace clone policy")
        # Path-based AF_UNIX sockets are not mediated until Landlock ABI 9.
        # New local-daemon connections could bypass hidden-file restrictions.
        # socketpair is a separate syscall and remains usable by event loops.
        socket_number = library.seccomp_syscall_resolve_name(b"socket")
        for family in range(0, 46):
            if family in (2, 10):  # AF_INET and AF_INET6 for approved providers/tools
                continue
            condition = _Argument(0, 4, family, 0)  # SCMP_CMP_EQ
            _checked(library.seccomp_rule_add_array(policy, 0x50000 | errno.EPERM, socket_number, 1, ctypes.byref(condition)), "socket capability policy")
        _checked(library.seccomp_load(policy), "metadata/escape policy")
    finally:
        library.seccomp_release(policy)


def enforce_isolation(spec: IsolationSpec) -> dict:
    """Irreversibly restrict this single-threaded bootstrap and future children.

    Call before importing any candidate/Host code. Descriptors opened before
    enforcement are outside path-open mediation: the caller must close all
    nonprotocol descriptors and must not pass private files to a Trial.
    """
    capability = preflight_isolation()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    # ABI6 supports all filesystem rights through IOCTL_DEV. WRITE/TRUNCATE
    # are always handled together, even for a resource allowed readonly.
    handled = (1 << 16) - 1
    ruleset = _checked(libc.syscall(444, ctypes.byref(_Ruleset(handled, 0, 3)), ctypes.sizeof(_Ruleset), 0), "Landlock ruleset")
    read = (1 << 0) | (1 << 2) | (1 << 3)
    try:
        for path, access in [(path, read) for path in spec.read_paths] + [(path, handled) for path in spec.write_paths] + [(path, 1 << 3) for path in spec.directory_paths]:
            if not path.is_dir():
                access &= (1 << 0) | (1 << 1) | (1 << 2) | (1 << 14) | (1 << 15)
                if not stat.S_ISREG(path.stat().st_mode):
                    access &= ~((1 << 0) | (1 << 14))
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(access, descriptor)
                _checked(libc.syscall(445, ruleset, 1, ctypes.byref(rule), 0), "Landlock resource rule")
            finally:
                os.close(descriptor)
        _checked(libc.prctl(38, 1, 0, 0, 0), "no-new-privileges policy")
        _seccomp()
        _checked(libc.syscall(446, ruleset, 0), "Landlock enforcement")
    finally:
        os.close(ruleset)
    return capability
