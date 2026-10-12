"""Send CTRL_BREAK to every process on another process's console.

hostcron starts jobs with CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, so a
console break can only reach one from a process attached to its console. This
helper frees its own console, attaches to the target's, ignores the event
itself, sends it to the whole console and detaches.

Usage: console_break.py <pid>
Exit codes: 0 sent, 2 could not attach to the target's console, 3 send failed.
"""
from __future__ import annotations

import sys
import time

CTRL_BREAK_EVENT = 1


def main(pid: int) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    Handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.AttachConsole.restype = wintypes.BOOL
    kernel32.FreeConsole.restype = wintypes.BOOL
    kernel32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL

    def _swallow(_ctrl_type: int) -> bool:
        return True

    swallow = Handler(_swallow)

    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid):
        return 2
    kernel32.SetConsoleCtrlHandler(None, True)
    kernel32.SetConsoleCtrlHandler(ctypes.cast(swallow, ctypes.c_void_p), True)
    ok = kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0)
    time.sleep(0.3)
    kernel32.FreeConsole()
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1])))
