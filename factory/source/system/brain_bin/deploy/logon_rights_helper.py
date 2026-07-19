#!/usr/bin/env python3
"""Host LSA logon-rights helper for the brain-factory residency stage.

Implements the contract residency.py:_logon_rights_helper() expects
($BRAIN_LOGON_RIGHTS_HELPER -> a module exposing):

    holds(brain) -> bool
    grant(brain) -> (ok: bool, detail: str)

Grants SeBatchLogonRight ("Log on as a batch job") to a brain's account via the
surgical, ADDITIVE Win32 LSA API (LsaAddAccountRights) — NOT a secedit policy
reimport (which would rewrite unrelated user-rights on the box). This is the exact
mechanism DEPLOYMENT.md / TROUBLESHOOTING.md §B document as the baseline grant.

Windows-only. Pure ctypes; no third-party deps.

CLI:
    python logon_rights_helper.py holds <brain>   # exit 0 if held, 1 if not
    python logon_rights_helper.py grant <brain>   # grants; exit 0 on success
"""
import sys
import ctypes
from ctypes import wintypes

advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

_RIGHT = "SeBatchLogonRight"
POLICY_ALL_ACCESS = 0x00000FFF
STATUS_SUCCESS = 0
# LsaEnumerateAccountRights returns this NTSTATUS when the account holds no rights.
STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034


class LSA_UNICODE_STRING(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    ]


class LSA_OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("RootDirectory", wintypes.HANDLE),
        ("ObjectName", ctypes.c_void_p),
        ("Attributes", wintypes.ULONG),
        ("SecurityDescriptor", ctypes.c_void_p),
        ("SecurityQualityOfService", ctypes.c_void_p),
    ]


advapi32.LsaOpenPolicy.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(LSA_OBJECT_ATTRIBUTES),
    wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
advapi32.LsaAddAccountRights.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(LSA_UNICODE_STRING), wintypes.ULONG]
advapi32.LsaEnumerateAccountRights.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(ctypes.POINTER(LSA_UNICODE_STRING)), ctypes.POINTER(wintypes.ULONG)]
advapi32.LsaClose.argtypes = [wintypes.HANDLE]
advapi32.LsaFreeMemory.argtypes = [ctypes.c_void_p]
advapi32.LsaNtStatusToWinError.argtypes = [wintypes.ULONG]
advapi32.LsaNtStatusToWinError.restype = wintypes.ULONG
# NTSTATUS is unsigned; without an explicit restype ctypes returns a SIGNED int, so
# comparisons like `st == STATUS_OBJECT_NAME_NOT_FOUND` (0xC0000034) silently fail.
advapi32.LsaOpenPolicy.restype = wintypes.ULONG
advapi32.LsaAddAccountRights.restype = wintypes.ULONG
advapi32.LsaEnumerateAccountRights.restype = wintypes.ULONG


def _sid_bytes(brain):
    """Resolve <brain> -> a binary SID buffer via LookupAccountName."""
    LookupAccountName = advapi32.LookupAccountNameW
    sid = ctypes.create_string_buffer(0)
    cb_sid = wintypes.DWORD(0)
    dom = ctypes.create_unicode_buffer(0)
    cch_dom = wintypes.DWORD(0)
    use = wintypes.DWORD(0)
    # First call sizes the buffers.
    LookupAccountName(None, brain, sid, ctypes.byref(cb_sid),
                      dom, ctypes.byref(cch_dom), ctypes.byref(use))
    sid = ctypes.create_string_buffer(cb_sid.value)
    dom = ctypes.create_unicode_buffer(cch_dom.value)
    if not LookupAccountName(None, brain, sid, ctypes.byref(cb_sid),
                             dom, ctypes.byref(cch_dom), ctypes.byref(use)):
        raise OSError(ctypes.get_last_error(), f"LookupAccountName failed for {brain!r}")
    return sid


def _open_policy():
    oa = LSA_OBJECT_ATTRIBUTES()
    oa.Length = ctypes.sizeof(LSA_OBJECT_ATTRIBUTES)
    handle = wintypes.HANDLE()
    st = advapi32.LsaOpenPolicy(None, ctypes.byref(oa), POLICY_ALL_ACCESS, ctypes.byref(handle))
    if st != STATUS_SUCCESS:
        raise OSError(advapi32.LsaNtStatusToWinError(st), "LsaOpenPolicy failed")
    return handle


def holds(brain):
    sid = _sid_bytes(brain)
    handle = _open_policy()
    try:
        rights = ctypes.POINTER(LSA_UNICODE_STRING)()
        count = wintypes.ULONG(0)
        st = advapi32.LsaEnumerateAccountRights(
            handle, sid, ctypes.byref(rights), ctypes.byref(count))
        if st == STATUS_OBJECT_NAME_NOT_FOUND:
            return False
        if st != STATUS_SUCCESS:
            raise OSError(advapi32.LsaNtStatusToWinError(st), "LsaEnumerateAccountRights failed")
        try:
            for i in range(count.value):
                if rights[i].Buffer == _RIGHT:
                    return True
            return False
        finally:
            advapi32.LsaFreeMemory(rights)
    finally:
        advapi32.LsaClose(handle)


def grant(brain):
    try:
        sid = _sid_bytes(brain)
        handle = _open_policy()
        try:
            r = LSA_UNICODE_STRING()
            r.Buffer = _RIGHT
            r.Length = len(_RIGHT) * 2
            r.MaximumLength = (len(_RIGHT) + 1) * 2
            arr = (LSA_UNICODE_STRING * 1)(r)
            st = advapi32.LsaAddAccountRights(handle, sid, arr, 1)
            if st != STATUS_SUCCESS:
                return False, f"LsaAddAccountRights NTSTATUS 0x{st:08X} (win err {advapi32.LsaNtStatusToWinError(st)})"
            return True, f"{_RIGHT} granted to {brain}"
        finally:
            advapi32.LsaClose(handle)
    except Exception as e:  # noqa: BLE001 — report, never raise, per helper contract
        return False, str(e)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("holds", "grant"):
        print(__doc__)
        sys.exit(2)
    action, who = sys.argv[1], sys.argv[2]
    if action == "holds":
        h = holds(who)
        print(f"{_RIGHT} held by {who}: {h}")
        sys.exit(0 if h else 1)
    ok, detail = grant(who)
    print(detail)
    sys.exit(0 if ok else 1)
