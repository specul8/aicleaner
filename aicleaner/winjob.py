"""Windows job objects: which processes belong to a session, and (when asked) ending them together.

Windows never ends a child when its parent ends, and `kill()` is TerminateProcess on one process. A session is a
tree - broker, Claude Code, its MCP servers through cmd and npx, every tool it runs - so killing or losing any
process in the middle leaves the rest running with nobody to answer for them (spec
2026-10-24-session-lifecycle-instrumentation.md). A job object is the kernel's own grouping: a process put in a job
before it starts children takes every descendant with it, and the job reports their ids, counts and peak memory.

With `kill_on_close` the kernel ends every process in the job when the last handle to it closes - including the
handle of a process that crashed or was killed, which the kernel closes for it. Without it the job only counts.

Everything here is a no-op returning empty values off Windows, so callers need no platform checks.
"""
from __future__ import annotations

import sys
from typing import Any

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # Information classes and limit flags (winnt.h).
    _BASIC_ACCOUNTING = 1
    _BASIC_PROCESS_ID_LIST = 3
    _EXTENDED_LIMIT = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _ACTIVE_PROCESS = 0x0008
    _JOB_MEMORY = 0x0200

    class _IoCounters(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOps", "WriteOps", "OtherOps",
                                                      "ReadBytes", "WriteBytes", "OtherBytes")]

    class _BasicLimit(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [("Basic", _BasicLimit), ("Io", _IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _Accounting(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001


def _flag(info: Any, bit: int, on: bool) -> None:
    if on:
        info.Basic.LimitFlags |= bit
    else:
        info.Basic.LimitFlags &= ~bit


class Job:
    """One job object. `handle` is None off Windows or when the job could not be made."""

    def __init__(self, kill_on_close: bool = False) -> None:
        self.handle: Any = None
        self.kill_on_close = False
        self.max_processes = 0
        self.max_memory_mb = 0
        self.error = ""
        if not WINDOWS:
            return
        handle = _k32.CreateJobObjectW(None, None)
        if not handle:
            self.error = f"CreateJobObject failed ({ctypes.get_last_error()})"
            return
        self.handle = handle
        if kill_on_close:
            self.set_kill_on_close(True)

    def set_kill_on_close(self, on: bool) -> bool:
        """Turn kernel cleanup on or off. Can change while processes are in the job."""
        if self._change(lambda info: _flag(info, _KILL_ON_JOB_CLOSE, on)):
            self.kill_on_close = on
            return True
        return False

    def set_limits(self, max_processes: int = 0, max_memory_mb: int = 0) -> bool:
        """Cap the job: processes alive at once, and memory committed by all of them together (0 = no cap).
        Past a cap the kernel refuses - a process start fails, an allocation fails - inside this job only, so a
        runaway in one session cannot take the machine with it (D208)."""
        def apply(info: Any) -> None:
            _flag(info, _ACTIVE_PROCESS, max_processes > 0)
            info.Basic.ActiveProcessLimit = max(0, int(max_processes))
            _flag(info, _JOB_MEMORY, max_memory_mb > 0)
            info.JobMemoryLimit = max(0, int(max_memory_mb)) * 1048576
        if self._change(apply):
            self.max_processes, self.max_memory_mb = max(0, int(max_processes)), max(0, int(max_memory_mb))
            return True
        return False

    def _change(self, edit: Any) -> bool:
        """Read the job's limits, let `edit` change them, write them back - so one setting never clears another."""
        if self.handle is None:
            return False
        info = _ExtendedLimit()
        size = ctypes.sizeof(info)
        _k32.QueryInformationJobObject(self.handle, _EXTENDED_LIMIT, ctypes.byref(info), size, None)
        edit(info)
        if not _k32.SetInformationJobObject(self.handle, _EXTENDED_LIMIT, ctypes.byref(info), size):
            self.error = f"SetInformationJobObject failed ({ctypes.get_last_error()})"
            return False
        return True

    def add_self(self) -> bool:
        """Put this process in the job. Done before starting children, every descendant joins too - no gap
        between a child starting and being assigned, in which it could start a grandchild outside."""
        if self.handle is None:
            return False
        if not _k32.AssignProcessToJobObject(self.handle, _k32.GetCurrentProcess()):
            self.error = f"AssignProcessToJobObject failed ({ctypes.get_last_error()})"
            return False
        return True

    def add(self, pid: int) -> bool:
        """Put another process in the job (its children started from now on follow it; earlier ones do not)."""
        if self.handle is None:
            return False
        process = _k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not process:
            self.error = f"OpenProcess({pid}) failed ({ctypes.get_last_error()})"
            return False
        try:
            if not _k32.AssignProcessToJobObject(self.handle, process):
                self.error = f"AssignProcessToJobObject({pid}) failed ({ctypes.get_last_error()})"
                return False
            return True
        finally:
            _k32.CloseHandle(process)

    def pids(self) -> list[int]:
        """The ids of every process in the job now."""
        if self.handle is None:
            return []
        # JOBOBJECT_BASIC_PROCESS_ID_LIST: two DWORD counts, then ULONG_PTR ids.
        for room in (64, 1024, 16384):
            class _List(ctypes.Structure):
                _fields_ = [("assigned", wintypes.DWORD), ("listed", wintypes.DWORD),
                            ("ids", ctypes.c_size_t * room)]
            listing = _List()
            if _k32.QueryInformationJobObject(self.handle, _BASIC_PROCESS_ID_LIST, ctypes.byref(listing),
                                              ctypes.sizeof(listing), None):
                return [int(listing.ids[i]) for i in range(min(int(listing.listed), room))]
        return []

    def stats(self) -> dict[str, Any]:
        """Counts and peak memory, for the lifecycle log and the Processes panel."""
        if self.handle is None:
            return {}
        accounting = _Accounting()
        limits = _ExtendedLimit()
        _k32.QueryInformationJobObject(self.handle, _BASIC_ACCOUNTING, ctypes.byref(accounting),
                                       ctypes.sizeof(accounting), None)
        _k32.QueryInformationJobObject(self.handle, _EXTENDED_LIMIT, ctypes.byref(limits),
                                       ctypes.sizeof(limits), None)
        return {"active": int(accounting.ActiveProcesses), "total": int(accounting.TotalProcesses),
                "terminated": int(accounting.TotalTerminatedProcesses),
                "peak_job_mb": round(limits.PeakJobMemoryUsed / 1048576, 1),
                "peak_process_mb": round(limits.PeakProcessMemoryUsed / 1048576, 1),
                "kill_on_close": self.kill_on_close, "max_processes": self.max_processes,
                "max_memory_mb": self.max_memory_mb, "pids": self.pids()}

    def terminate(self, code: int = 1) -> bool:
        """End every process in the job now (this one too, if it is in it)."""
        return bool(self.handle is not None and _k32.TerminateJobObject(self.handle, code))

    def close(self) -> None:
        """Let go of the handle. With kill_on_close and no other handle open, the kernel ends the job's processes."""
        if self.handle is not None:
            _k32.CloseHandle(self.handle)
            self.handle = None
