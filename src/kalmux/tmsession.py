"""tmsession: which macOS login session a process belongs to, and whether the ui server is a left-over.

A WindowServer crash logs the user out and back in without a reboot. Processes outside the GUI session
survive it: the tmux server (a LaunchAgent, or a child of one) and a `kalmux ui serve` started from a tmux
pane. They keep the audit session of the login that is gone, and from there an Apple Event to the new
iTerm2 fails ("Can't get application "iTerm2" (-1728)"). `it2` fetches its API cookie through exactly
such an event, so every call fails and the toolbelt can neither jump to a tab nor open one. Measured on
2026-11-01: the old session was 100019, the new one 108882; the clipboard and the Keychain still worked.

iTerm2's AutoLaunch runs `kalmux ui start` in the live session, so that is where a left-over server is
replaced. It is replaced only when this process can talk to iTerm2 itself: from ssh, or from a pane of a
tmux server that outlived the logout, iTerm2 does not answer either, and a new server would be no better.
"""
from __future__ import annotations

import ctypes
import sys
import time

LIBSYSTEM = "/usr/lib/libSystem.B.dylib"


class _Mask(ctypes.Structure):          # au_mask_t
    _fields_ = [("success", ctypes.c_uint32), ("failure", ctypes.c_uint32)]


class _TermId(ctypes.Structure):        # au_tid_addr_t (dev_t is 32-bit on macOS)
    _fields_ = [("port", ctypes.c_int32), ("type", ctypes.c_uint32), ("addr", ctypes.c_uint32 * 4)]


class _AuditInfo(ctypes.Structure):     # struct auditinfo_addr, <bsm/audit.h>
    _fields_ = [("auid", ctypes.c_uint32), ("mask", _Mask), ("termid", _TermId), ("asid", ctypes.c_int32),
                ("flags", ctypes.c_uint64)]


# <bsm/audit_session.h>: a console login has graphic access; ssh creates a remote session without it
FLAG_GRAPHIC_ACCESS = 0x0010
FLAG_IS_REMOTE = 0x1000


def audit_info() -> _AuditInfo | None:
    """This process's auditinfo_addr (getaudit_addr), None away from macOS or when the call fails."""
    if sys.platform != "darwin":
        return None
    try:
        libc = ctypes.CDLL(LIBSYSTEM, use_errno=True)
        info = _AuditInfo()
        if libc.getaudit_addr(ctypes.byref(info), ctypes.sizeof(info)) != 0:
            return None
    except (OSError, AttributeError):
        return None
    return info


def audit_session() -> int | None:
    """The audit session id of this process, None away from macOS."""
    info = audit_info()
    return int(info.asid) if info is not None else None


def graphic_session(flags: int | None) -> bool:
    """A local login with graphic access: the only kind of session that may take the server over."""
    return isinstance(flags, int) and bool(flags & FLAG_GRAPHIC_ACCESS) and not flags & FLAG_IS_REMOTE


def it2_answers(it2, tries: int = 1, pause: float = 1.0, sleep=time.sleep) -> bool:
    """Can this process talk to iTerm2? One `it2 session list` (about 150 ms) per try, up to `tries` times:
    AutoLaunch runs while iTerm2 is still starting, and one early miss must not keep a dead server."""
    if not it2.available():
        return False
    for attempt in range(max(1, tries)):
        if attempt:
            sleep(pause)
        if it2.ping():
            return True
    return False


def left_over(health: dict, mine: int | None, reaches_iterm2, graphic: bool = True) -> bool:
    """True when the server answering /healthz runs in another login session, this process sits in a local
    graphic session, AND it reaches iTerm2: then this process is in the live login and the server is the one
    left behind. `reaches_iterm2` is only called when all the rest already holds."""
    theirs = health.get("asid")
    if not (isinstance(theirs, int) and isinstance(mine, int)) or theirs == mine or not graphic:
        return False
    return bool(reaches_iterm2())


def caller_left_over(health: dict, it2_factory, tries: int = 1) -> bool:
    """left_over() for this process: its own session, its own flags, a fresh It2 probe."""
    info = audit_info()
    if info is None:
        return False
    return left_over(health, int(info.asid), lambda: it2_answers(it2_factory(), tries=tries),
                     graphic=graphic_session(int(info.flags)))
