"""Current-user Windows DPAPI; secrets never fall back to plaintext storage."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import ctypes
import os
import uuid
from urllib.parse import urlparse

from paperpilot.services.project_errors import ProjectServiceError


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_uint32), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _transform(data: bytes, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise ProjectServiceError(503,'credential_protection_unavailable','当前系统没有可用的受保护凭据存储；未保存密钥。请选择本机免密钥服务，或本次操作的临时密钥模式。')
    try:
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    except OSError as exc:
        raise ProjectServiceError(503,'credential_protection_unavailable','当前账户的系统凭据保护不可用；没有保存明文。请选择本机免密钥服务或本次操作临时密钥。') from exc
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_uint32,ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    kernel.LocalFree.restype=ctypes.c_void_p
    buffer=ctypes.create_string_buffer(data)
    source=Blob(len(data),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte)))
    output=Blob()
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; user scope, never machine-wide protection.
        if not function(ctypes.byref(source),None,None,None,None,1,ctypes.byref(output)):
            code='credential_unavailable' if decrypt else 'credential_protection_unavailable'
            raise ProjectServiceError(503,code,'无法使用当前系统账户保护或读取凭据；没有保存明文。请在当前账户重新输入密钥。')
        return ctypes.string_at(output.data,output.size)
    finally:
        ctypes.memset(buffer,0,len(data))
        if output.data:
            ctypes.memset(output.data,0,output.size)
            kernel.LocalFree(ctypes.cast(output.data,ctypes.c_void_p))


def protect(secret: str) -> bytes:
    return _transform(secret.encode('utf-8'))


def unprotect(value: bytes) -> str:
    try:
        return _transform(value,True).decode('utf-8')
    except (ValueError,UnicodeError) as exc:
        raise ProjectServiceError(503,'credential_unavailable','受保护凭据不可读取，请重新输入；不会尝试其他提供方密钥。') from exc


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS ai_provider_credentials(
      id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES ai_provider_profiles(id),
      protected_value BLOB NOT NULL,protection TEXT NOT NULL,created_at TEXT NOT NULL)''')
    if 'credential_ref' not in {r['name'] for r in conn.execute('PRAGMA table_info(ai_provider_profiles)')}:
        conn.execute("ALTER TABLE ai_provider_profiles ADD COLUMN credential_ref TEXT NOT NULL DEFAULT ''")
    # Older digests cannot be recovered into secrets, and are never reported usable.
    conn.execute("UPDATE ai_provider_profiles SET credential_state='missing' WHERE auth_mode='api_key' AND credential_ref=''")


def save(conn, profile_id, secret, timestamp):
    protected=protect(secret)
    ref=uuid.uuid4().hex
    conn.execute('INSERT INTO ai_provider_credentials VALUES(?,?,?,?,?)',(ref,profile_id,protected,'windows_dpapi_current_user',timestamp))
    return ref


def load(conn, profile_id, reference):
    row=conn.execute('SELECT * FROM ai_provider_credentials WHERE id=? AND profile_id=?',(reference,profile_id)).fetchone()
    if not row or row['protection']!='windows_dpapi_current_user':
        raise ProjectServiceError(409,'credential_unavailable','此提供方的受保护密钥不可用，请重新输入并测试；不会使用其他配置的密钥。')
    return unprotect(bytes(row['protected_value']))


@dataclass(frozen=True)
class RuntimeCredential:
    base_url: str
    auth_mode: str
    key: str = field(repr=False)
    profile_id: str = ''
    config_revision: int = 0
    model_limits: dict = field(default_factory=dict)


_current: ContextVar[RuntimeCredential | None]=ContextVar('paperpilot_provider_credential',default=None)


@contextmanager
def scoped(credential):
    token=_current.set(credential)
    try:
        yield
    finally:
        _current.reset(token)


def current(base_url):
    value=_current.get()
    if value and value.base_url.rstrip('/')!=base_url.rstrip('/'):
        raise ProjectServiceError(409,'credential_endpoint_mismatch','请求接收方与凭据所属配置不一致；未发送请求。')
    return value
