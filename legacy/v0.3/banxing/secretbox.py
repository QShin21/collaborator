"""Keeping the API key at rest.

On Windows the key is encrypted with DPAPI (CryptProtectData), which ties it to
the signed-in Windows user: copying settings.json to another account or machine
does not reveal it. If DPAPI fails, `protect` raises: on Windows a secret is never
written in a readable form. Elsewhere (development on Linux/macOS) there is no such
facility in the standard library, so the value is only encoded and the settings
file is made private to the user.

Stored form:  "dpapi:<base64>"  or  "plain:<base64>".
"""
from __future__ import annotations

import base64
import sys

_ENTROPY = b"Banxing/api-key"


class SecretError(Exception):
    pass


def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source, keep_source = blob(data)
    entropy, keep_entropy = blob(_ENTROPY)
    result = Blob()
    ok = function(ctypes.byref(source), None, ctypes.byref(entropy), None, None,
                  0x1, ctypes.byref(result))            # CRYPTPROTECT_UI_FORBIDDEN
    if not ok:
        raise SecretError(f"DPAPI failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def _on_windows():
    return sys.platform == "win32"


def protect(secret):
    """Encode a secret for a file on disk.

    On Windows this either encrypts or raises SecretError: when DPAPI fails, nothing
    readable is written in its place. The caller keeps the secret out of the file
    and tells the user."""
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if _on_windows():
        try:
            return "dpapi:" + base64.b64encode(_dpapi(raw, True)).decode("ascii")
        except SecretError:
            raise
        except Exception as error:      # missing DLL, bad signature, anything: fail closed
            raise SecretError(f"DPAPI unavailable ({type(error).__name__})") from error
    return "plain:" + base64.b64encode(raw).decode("ascii")


def reveal(stored):
    """Decode a stored secret. Returns "" when it cannot be read (e.g. another
    Windows account): the user is then asked for the key again. On Windows a value
    that is not encrypted is not accepted either — it is asked for again and then
    stored encrypted."""
    if not stored or ":" not in stored:
        return ""
    scheme, payload = stored.split(":", 1)
    try:
        raw = base64.b64decode(payload.encode("ascii"), validate=True)
        if scheme == "plain" and not _on_windows():
            return raw.decode("utf-8")
        if scheme == "dpapi" and _on_windows():
            return _dpapi(raw, False).decode("utf-8")
    except Exception:
        return ""
    return ""


def is_encrypted(stored):
    return bool(stored) and stored.startswith("dpapi:")
