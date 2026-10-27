"""Running with a standard-user token when the start-up task gave aicleaner an administrator's (feature 107).

A task set to run whether or not anyone is signed in gets the account's full token: Windows applies UAC's filtering to
interactive sign-ins, not to batch logons, so "Run with highest privileges" off did not keep an administrator account
limited (the 2026-10-25 live test: /healthz said elevated). aicleaner - and every session and Claude Code it starts -
must not run as an administrator because of how it was started.

So, started elevated by the task, aicleaner starts itself again with a standard-user token and waits for that copy:
the SAFER "normal user" level (what `runas /trustlevel:0x20000` uses - the Administrators group deny-only, admin
privileges removed) at medium integrity, as a signed-in session would run it. The copy inherits this process's
console and standard handles, so its log goes where the launcher sent this one's; its exit code is this process's.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Sequence

DROPPED = "AICLEANER_TOKEN_DROPPED"         # set in the copy, so it never tries again
# What says "this process is the start-up task's aicleaner" - never passed on to a session: anything started inside
# one (a test server, `python -m aicleaner ...`) would take itself for the task's copy (D213).
SERVICE_ONLY = ("AICLEANER_SERVICE", DROPPED, "AICLEANER_KEEP_ADMIN_TOKEN")


def session_environment(base: dict | None = None) -> dict:
    """An environment for a session's processes: this one's, without what marks the start-up task's copy."""
    env = dict(os.environ if base is None else base)
    for name in SERVICE_ONLY:
        env.pop(name, None)
    return env
KEEP = "AICLEANER_KEEP_ADMIN_TOKEN"         # set to 1 to run with whatever token it was given

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class _StartupInfo(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class _ProcessInformation(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE), ("dwProcessId", wintypes.DWORD),
                    ("dwThreadId", wintypes.DWORD)]

    _advapi.SaferCreateLevel.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    _advapi.SaferComputeTokenFromLevel.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE),
                                                   wintypes.DWORD, ctypes.c_void_p]
    _advapi.SaferCloseLevel.argtypes = [wintypes.HANDLE]
    _advapi.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    _advapi.SetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _advapi.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                             ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                                             wintypes.LPCWSTR, ctypes.POINTER(_StartupInfo),
                                             ctypes.POINTER(_ProcessInformation)]
    _k32.GetStdHandle.restype = wintypes.HANDLE
    _k32.GetStdHandle.argtypes = [wintypes.DWORD]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]

    _SAFER_SCOPEID_USER = 2
    _SAFER_LEVELID_NORMALUSER = 0x20000
    _SAFER_LEVEL_OPEN = 1
    _TOKEN_INTEGRITY_LEVEL = 25
    _SE_GROUP_INTEGRITY = 0x20
    _MEDIUM_INTEGRITY = "S-1-16-8192"
    _STARTF_USESTDHANDLES = 0x100
    _CREATE_UNICODE_ENVIRONMENT = 0x400
    _INFINITE = 0xFFFFFFFF


class TokenError(OSError):
    """A standard-user token could not be made, or a process started with it."""


def _check(ok: object, what: str) -> None:
    if not ok:
        raise TokenError(f"{what} failed (Windows error {ctypes.get_last_error()})")


def standard_user_token() -> "wintypes.HANDLE":
    """This process's token at the SAFER normal-user level, at medium integrity. The caller closes it."""
    level = wintypes.HANDLE()
    _check(_advapi.SaferCreateLevel(_SAFER_SCOPEID_USER, _SAFER_LEVELID_NORMALUSER, _SAFER_LEVEL_OPEN,
                                    ctypes.byref(level), None), "SaferCreateLevel")
    token = wintypes.HANDLE()
    try:
        _check(_advapi.SaferComputeTokenFromLevel(level, None, ctypes.byref(token), 0, None),
               "SaferComputeTokenFromLevel")
    finally:
        _advapi.SaferCloseLevel(level)
    sid = ctypes.c_void_p()
    _check(_advapi.ConvertStringSidToSidW(_MEDIUM_INTEGRITY, ctypes.byref(sid)), "ConvertStringSidToSid")
    try:
        label = _SidAndAttributes(sid, _SE_GROUP_INTEGRITY)
        _check(_advapi.SetTokenInformation(token, _TOKEN_INTEGRITY_LEVEL, ctypes.byref(label),
                                           ctypes.sizeof(label) + ctypes.sizeof(ctypes.c_void_p)),
               "SetTokenInformation(integrity)")
    finally:
        _k32.LocalFree(sid)
    return token


def run_with_standard_token(argv: Sequence[str]) -> int:
    """Run argv with a standard-user token, sharing this process's console and standard handles; its exit code."""
    token = standard_user_token()
    try:
        startup = _StartupInfo()
        startup.cb = ctypes.sizeof(startup)
        startup.dwFlags = _STARTF_USESTDHANDLES
        startup.hStdInput = _k32.GetStdHandle(-10 & 0xFFFFFFFF)
        startup.hStdOutput = _k32.GetStdHandle(-11 & 0xFFFFFFFF)
        startup.hStdError = _k32.GetStdHandle(-12 & 0xFFFFFFFF)
        info = _ProcessInformation()
        command = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(argv)))
        _check(_advapi.CreateProcessAsUserW(token, None, command, None, None, True, _CREATE_UNICODE_ENVIRONMENT,
                                            None, os.getcwd(), ctypes.byref(startup), ctypes.byref(info)),
               "CreateProcessAsUser")
    finally:
        _k32.CloseHandle(token)
    try:
        _k32.WaitForSingleObject(info.hProcess, _INFINITE)
        code = wintypes.DWORD()
        _k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return int(code.value)
    finally:
        _k32.CloseHandle(info.hThread)
        _k32.CloseHandle(info.hProcess)


def should_drop(elevated: bool | None) -> bool:
    """Started elevated by the start-up task, not already the dropped copy, and not told to keep the token."""
    return (sys.platform == "win32" and elevated is True and bool(os.environ.get("AICLEANER_SERVICE", "").strip())
            and not os.environ.get(DROPPED) and os.environ.get(KEEP, "") != "1")


def drop_if_elevated(elevated: bool | None, argv: Sequence[str] | None = None) -> int | None:
    """The exit code of a standard-user copy of this command when one was run; None when this process should go on
    itself (not elevated, not the task's, or the copy could not be started - said in the log by the caller)."""
    if not should_drop(elevated):
        return None
    # This process only waits now. A sign-out must not end it (the task's copy outlives sign-outs, and the launcher
    # would start another beside the running copy); a shutdown ends it, as it ends the copy.
    from . import shutdown
    shutdown.install(lambda kind: None)
    os.environ[DROPPED] = "1"                  # inherited by the copy (the environment is this process's)
    command = list(argv) if argv is not None else [sys.executable, *sys.orig_argv[1:]]
    return run_with_standard_token(command)
