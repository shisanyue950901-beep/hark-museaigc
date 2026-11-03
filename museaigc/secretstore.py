"""本机保存 API Key。

Windows：DPAPI（CryptProtectData，当前用户范围），文件拷到别的电脑或别的用户下解不开。
其他系统：优先 keyring（若已安装）；否则 0600 权限的文件（仅供开发调试）。
Key 永远不写进工程文件、日志或返回给页面。
"""
from __future__ import annotations

import os
import sys

ENTROPY = b"museaigc-v1"


def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes):
        buf = ctypes.create_string_buffer(b, len(b))
        return BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _k1 = blob(data)
    ent, _k2 = blob(ENTROPY)
    out = BLOB()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI 失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


class KeyStore:
    def __init__(self, path: str):
        self.path = path
        if sys.platform == "win32":
            self.kind = "dpapi"
        else:
            try:
                import keyring  # noqa: F401
                self.kind = "keyring"
            except Exception:
                self.kind = "file"

    def save(self, key: str):
        if not key:
            return
        if self.kind == "dpapi":
            with open(self.path, "wb") as f:
                f.write(_dpapi(key.encode(), True))
        elif self.kind == "keyring":
            import keyring
            keyring.set_password("museaigc", "muse2api", key)
        else:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)

    def load(self) -> str:
        try:
            if self.kind == "dpapi":
                with open(self.path, "rb") as f:
                    return _dpapi(f.read(), False).decode()
            if self.kind == "keyring":
                import keyring
                return keyring.get_password("museaigc", "muse2api") or ""
            with open(self.path) as f:
                return f.read().strip()
        except (FileNotFoundError, OSError):
            return ""
