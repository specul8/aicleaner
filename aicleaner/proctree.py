"""The machine's processes and memory, cheaply, for the lifecycle log and the Processes panel.

`snapshot()` is one Toolhelp32 pass (id, parent, name) plus, for each process this account can open, its path,
creation time and private bytes. `orphans()` picks out the processes a session leaves behind: the kinds a session
starts, whose chain of live parents does not reach anything that owns them (spec
2026-10-24-session-lifecycle-instrumentation.md). Windows only; elsewhere both return empty.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

WINDOWS = sys.platform == "win32"

# What a session starts, directly or through Claude Code: these are the names an orphan can have.
SESSION_KINDS = frozenset({"claude.exe", "node.exe", "cmd.exe", "python.exe", "pythonw.exe", "pwsh.exe",
                           "powershell.exe", "bash.exe", "conhost.exe"})
# What owns a session's processes legitimately when they are not under aicleaner: a person's own terminal or editor.
OWNER_KINDS = frozenset({"code.exe", "windowsterminal.exe", "explorer.exe", "openconsole.exe", "claude.exe",
                         "svchost.exe", "services.exe", "wininit.exe", "sshd.exe", "taskhostw.exe",
                         "devenv.exe", "rider64.exe", "idea64.exe", "pycharm64.exe"})

if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _psapi = ctypes.WinDLL("psapi", use_last_error=True)

    class _Entry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    class _MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t)]

    class _Performance(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("CommitTotal", ctypes.c_size_t), ("CommitLimit", ctypes.c_size_t),
                    ("CommitPeak", ctypes.c_size_t), ("PhysicalTotal", ctypes.c_size_t),
                    ("PhysicalAvailable", ctypes.c_size_t), ("SystemCache", ctypes.c_size_t),
                    ("KernelTotal", ctypes.c_size_t), ("KernelPaged", ctypes.c_size_t),
                    ("KernelNonpaged", ctypes.c_size_t), ("PageSize", ctypes.c_size_t),
                    ("HandleCount", wintypes.DWORD), ("ProcessCount", wintypes.DWORD),
                    ("ThreadCount", wintypes.DWORD)]

    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Entry)]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    _psapi.GetPerformanceInfo.argtypes = [ctypes.c_void_p, wintypes.DWORD]

    _SNAPPROCESS = 0x2
    _QUERY_LIMITED = 0x1000
    _VM_READ = 0x0010
    _INVALID = wintypes.HANDLE(-1).value
    _EPOCH_AS_FILETIME = 116444736000000000


@dataclass
class Proc:
    pid: int
    ppid: int
    name: str
    path: str = ""
    created: float = 0.0          # Unix time; 0 when it could not be read
    private_mb: float = 0.0


@dataclass
class Snapshot:
    taken: float = field(default_factory=time.time)
    procs: dict[int, Proc] = field(default_factory=dict)
    memory: dict[str, Any] = field(default_factory=dict)


def _details(proc: Proc) -> None:
    handle = _k32.OpenProcess(_QUERY_LIMITED | _VM_READ, False, proc.pid) or \
        _k32.OpenProcess(_QUERY_LIMITED, False, proc.pid)
    if not handle:
        return
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(1024)
        if _k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            proc.path = buffer.value
        times = [wintypes.FILETIME() for _ in range(4)]
        if _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            proc.created = (ticks - _EPOCH_AS_FILETIME) / 1e7
        counters = _MemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            proc.private_mb = round(counters.PrivateUsage / 1048576, 1)
    finally:
        _k32.CloseHandle(handle)


def token_facts() -> dict[str, Any]:
    """What this process's token can actually do: whether the Administrators group is enabled in it, its integrity
    level, and Windows' own elevation flag. A process can always read its own token - unlike another logon session's,
    which a normal window may not open (feature 107's live test: the start-up task's proxy is in session 0).

    The flag alone misleads: a token lowered with SAFER from an elevated one keeps TokenElevation set although the
    Administrators group is deny-only (D211's live check, 2026-10-26). A UAC-limited sign-in has the group deny-only
    at medium integrity; that is the bar."""
    if not WINDOWS:
        return {}
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.CheckTokenMembership.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL)]
    advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    advapi.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    facts: dict[str, Any] = {}
    admins = ctypes.c_void_p()
    if advapi.ConvertStringSidToSidW("S-1-5-32-544", ctypes.byref(admins)):
        member = wintypes.BOOL()
        if advapi.CheckTokenMembership(None, admins, ctypes.byref(member)):
            facts["admin_group_enabled"] = bool(member.value)
        ctypes.windll.kernel32.LocalFree(admins)
    token = wintypes.HANDLE()
    if advapi.OpenProcessToken(_k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        try:
            value, size = wintypes.DWORD(), wintypes.DWORD()
            if advapi.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
                facts["elevation_flag"] = bool(value.value)
            buffer = ctypes.create_string_buffer(64)
            if advapi.GetTokenInformation(token, 25, buffer, 64, ctypes.byref(size)):
                sid = ctypes.c_void_p.from_buffer(buffer).value          # TOKEN_MANDATORY_LABEL.Label.Sid
                count = advapi.GetSidSubAuthorityCount(sid).contents.value
                rid = advapi.GetSidSubAuthority(sid, count - 1).contents.value
                facts["integrity"] = {0x1000: "low", 0x2000: "medium", 0x2100: "medium-plus", 0x3000: "high",
                                      0x4000: "system"}.get(rid, hex(rid))
        finally:
            _k32.CloseHandle(token)
    return facts


def elevated() -> bool | None:
    """Whether this process can act as an administrator: the Administrators group enabled, or high integrity or
    above. None when it cannot be told."""
    if not WINDOWS:
        return None
    facts = token_facts()
    if "admin_group_enabled" in facts or "integrity" in facts:
        return bool(facts.get("admin_group_enabled")) or facts.get("integrity") in ("high", "system")
    return _elevation_flag()


def _elevation_flag() -> bool | None:
    if not WINDOWS:
        return None
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(_k32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        return None
    try:
        value, size = wintypes.DWORD(), wintypes.DWORD()
        if not advapi.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size)):
            return None
        return bool(value.value)
    finally:
        _k32.CloseHandle(token)


def memory() -> dict[str, Any]:
    """Commit used and limit, physical memory available, handles and processes - the numbers the 10:40 OOM was."""
    if not WINDOWS:
        return {}
    info = _Performance()
    info.cb = ctypes.sizeof(info)
    if not _psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return {}
    page = info.PageSize
    return {"commit_mb": round(info.CommitTotal * page / 1048576), "commit_limit_mb": round(info.CommitLimit * page / 1048576),
            "commit_peak_mb": round(info.CommitPeak * page / 1048576),
            "available_mb": round(info.PhysicalAvailable * page / 1048576),
            "handles": int(info.HandleCount), "processes": int(info.ProcessCount), "threads": int(info.ThreadCount)}


def snapshot(details_for: Iterable[str] | None = None) -> Snapshot:
    """Every process, with path, start time and private bytes read for the names in `details_for` (all when None)."""
    snap = Snapshot()
    if not WINDOWS:
        return snap
    wanted = None if details_for is None else {n.lower() for n in details_for}
    handle = _k32.CreateToolhelp32Snapshot(_SNAPPROCESS, 0)
    if handle == _INVALID or not handle:
        return snap
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            proc = Proc(int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile)
            snap.procs[proc.pid] = proc
            ok = _k32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(handle)
    for proc in snap.procs.values():
        if proc.pid and (wanted is None or proc.name.lower() in wanted or proc.name.lower() in OWNER_KINDS):
            _details(proc)
    snap.memory = memory()
    return snap


def _parent(snap: Snapshot, proc: Proc) -> Proc | None:
    """The live parent, or None when it has gone - including when its id now belongs to a newer process."""
    parent = snap.procs.get(proc.ppid)
    if parent is None or parent.pid == proc.pid:
        return None
    if parent.created and proc.created and parent.created > proc.created + 1:
        return None                                   # the id was reused by a process started after the child
    return parent


def orphans(snap: Snapshot, owners: Iterable[int], venv: str = "") -> list[Proc]:
    """Session-kind processes whose live ancestry reaches neither an owner pid (the proxy, the brokers) nor an
    owner kind (a terminal, an editor, Claude Desktop). Only the top of each orphaned tree is listed, and an
    aicleaner python is counted only when it runs from `venv` (another program's Python is not ours to report)."""
    owner_pids = set(owners)
    # The venv's python.exe is a launcher that waits on the real interpreter, so a broker or the proxy sits below a
    # launcher whose own parent may be long gone. Anything above an owner is held by it, not left behind.
    for pid in list(owner_pids):
        current = snap.procs.get(pid)
        hops = 0
        while current is not None and hops < 64:
            owner_pids.add(current.pid)
            current = _parent(snap, current)
            hops += 1
    venv_low = venv.lower()
    found: list[Proc] = []
    for proc in snap.procs.values():
        name = proc.name.lower()
        if name not in SESSION_KINDS or proc.pid in owner_pids:
            continue
        if name in ("python.exe", "pythonw.exe") and venv_low and not proc.path.lower().startswith(venv_low):
            continue
        parent = _parent(snap, proc)
        if parent is not None and parent.name.lower() in SESSION_KINDS and parent.pid not in owner_pids:
            continue                                  # not the top of its tree; the top is judged instead
        chain, current, owned = 0, proc, False
        while current is not None and chain < 64:
            if current.pid in owner_pids or (current is not proc and current.name.lower() in OWNER_KINDS):
                owned = True
                break
            current = _parent(snap, current)
            chain += 1
        if not owned and parent is None:
            found.append(proc)
    return found


def descendants(snap: Snapshot, pid: int) -> list[Proc]:
    """Every live process below `pid`."""
    children: dict[int, list[Proc]] = {}
    for proc in snap.procs.values():
        parent = _parent(snap, proc)
        if parent is not None:
            children.setdefault(parent.pid, []).append(proc)
    out, stack = [], list(children.get(pid, []))
    while stack:
        proc = stack.pop()
        out.append(proc)
        stack.extend(children.get(proc.pid, []))
    return out
