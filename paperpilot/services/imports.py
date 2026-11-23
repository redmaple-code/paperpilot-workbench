from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import threading
import time
import html
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urljoin, quote

from pypdf import PdfReader


MAX_FETCH_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT = 20.0


class ImportError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_url(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return text


def _pdf_text(content: bytes) -> tuple[str, list[str]]:
    try:
        reader = PdfReader(io.BytesIO(content))
        pages: list[str] = []
        for page in reader.pages:
            pages.append((page.extract_text() or "").strip())
        text = "\n\n".join(p for p in pages if p)
        return text, pages
    except Exception as exc:  # parser libraries expose several exception types
        raise ImportError("parse_failed", "PDF 无法解析，请检查公开文件是否完整", retryable=False) from exc


def _html_text(content: bytes) -> tuple[str, list[str]]:
    parser = _TextParser()
    try:
        parser.feed(content.decode("utf-8", errors="replace"))
    except Exception as exc:
        raise ImportError("parse_failed", "HTML 无法解析", retryable=False) from exc
    text = "\n".join(parser.parts)
    return text, [text]


def extract_preview(content: bytes, content_type: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if content.startswith(b"%PDF-") or "pdf" in content_type.lower():
        _, pages = _pdf_text(content)
        return preview_from_pages(pages, "pdf", metadata)
    _, pages = _html_text(content)
    return preview_from_pages(pages, "html", metadata)


def preview_from_pages(pages: list[str], content_kind: str, metadata: dict[str, Any]) -> dict[str, Any]:
    is_pdf = content_kind == "pdf"
    text = "\n\n".join(pages)
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        raise ImportError("parse_partial", "公开文件未提取到可用文本", retryable=False)
    full_fragments: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        page_text = re.sub(r"\s+", " ", page).strip()
        if page_text:
            full_fragments.append({"location": f"page:{index + 1}" if is_pdf else "document", "text": page_text})
    warnings = []
    if len(compact) < 200:
        warnings.append("可提取文本较少，请人工核对原文")
    if is_pdf and len(full_fragments) < len(pages):
        warnings.append("部分 PDF 页无可提取文本，可能需要 OCR")
    return {
        "content_kind": content_kind,
        "metadata": metadata,
        "abstract": metadata.get("abstract") or compact[:2000],
        "fragments": [piece | {"text": piece["text"][:1200]} for piece in full_fragments[:8]],
        "full_fragments": full_fragments,
        "page_count": len(pages) if is_pdf else None,
        "text_page_count": len(full_fragments) if is_pdf else None,
        "text_preview": compact[:6000],
        "extraction_quality": "partial" if warnings else "ok",
        "parser_version": "oa-import-v2",
        "warnings": warnings,
    }


_DNS_SLOTS = threading.BoundedSemaphore(4)
_MAX_REDIRECTS = 5
_METADATA_LIMIT = 2 * 1024 * 1024


def _public_target(url: str, deadline: float):
    if not isinstance(url,str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ImportError('invalid_url','OA 链接无效。')
    try:
        parsed=urlparse(url)
        if parsed.scheme.lower() not in {'http','https'} or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError('invalid URL')
        host=parsed.hostname.encode('idna').decode('ascii')
        if '%' in host or any(ch in host for ch in ('/', '\\', ' ', '\t')):raise ValueError('invalid host')
        port=parsed.port if parsed.port is not None else (443 if parsed.scheme.lower()=='https' else 80)
        if not 1<=port<=65535:raise ValueError('invalid port')
    except (ValueError,UnicodeError) as exc:
        raise ImportError('invalid_url','OA 链接无效，请使用不含凭据的公开 HTTP(S) 地址。') from exc
    remaining=deadline-time.monotonic()
    if remaining<=0:raise ImportError('fetch_timeout','解析或下载超时，请稍后重试。',retryable=True)
    if not _DNS_SLOTS.acquire(timeout=min(remaining,1.0)):
        raise ImportError('dns_busy','域名解析繁忙，请稍后重试。',retryable=True)
    done=threading.Event();result=[]
    def resolve():
        try:result.append(socket.getaddrinfo(host,port,type=socket.SOCK_STREAM,proto=socket.IPPROTO_TCP))
        except OSError as exc:result.append(exc)
        finally:_DNS_SLOTS.release();done.set()
    try:threading.Thread(target=resolve,daemon=True).start()
    except RuntimeError as exc:
        _DNS_SLOTS.release()
        raise ImportError('dns_busy','域名解析资源不足，请稍后重试。',retryable=True) from exc
    if not done.wait(max(0,deadline-time.monotonic())):
        raise ImportError('fetch_timeout','域名解析超时，请稍后重试。',retryable=True)
    if not result or isinstance(result[0],Exception):
        raise ImportError('dns_unavailable','无法解析公开来源，请检查网络后重试。',retryable=True)
    addresses=[]
    for family,kind,proto,_canonical,address in result[0]:
        if family not in (socket.AF_INET,socket.AF_INET6) or kind!=socket.SOCK_STREAM:continue
        try:
            ip=ipaddress.ip_address(address[0]);mapped=getattr(ip,'ipv4_mapped',None)
            if (not ip.is_global or ip.is_multicast or ip.is_reserved or (mapped is not None and (not mapped.is_global or mapped.is_multicast or mapped.is_reserved))
                or (family==socket.AF_INET6 and len(address)>3 and address[3])):
                raise ImportError('non_public_address','来源或跳转指向非公网地址，已阻止抓取。')
        except ValueError as exc:raise ImportError('invalid_url','来源地址无法验证。') from exc
        item=(family,kind,proto,address)
        if item not in addresses:addresses.append(item)
    if not addresses:raise ImportError('dns_unavailable','公开来源没有可用地址，请稍后重试。',retryable=True)
    return parsed,host,port,addresses


def _local_proxy(scheme: str):
    """Use only an explicitly configured, unauthenticated local HTTP proxy."""
    mode=os.environ.get('PAPERPILOT_OA_PROXY_MODE','auto').strip().lower()
    if mode=='direct':return None
    if mode!='auto':raise ImportError('proxy_configuration_unsupported','抓取代理模式无效；请设为 auto 或 direct。')
    name='https_proxy' if scheme=='https' else 'http_proxy'
    value=os.environ.get(name) or os.environ.get(name.upper())
    if not value:return None
    try:
        proxy=urlparse(value)
        if (proxy.scheme.lower()!='http' or proxy.hostname not in {'localhost','127.0.0.1','::1'}
            or proxy.username is not None or proxy.password is not None
            or proxy.path not in {'','/'} or proxy.params or proxy.query or proxy.fragment):raise ValueError('unsupported proxy')
        port=proxy.port if proxy.port is not None else 80
        if not 1<=port<=65535:raise ValueError('invalid proxy port')
    except (ValueError,UnicodeError) as exc:
        raise ImportError('proxy_configuration_unsupported','当前抓取仅支持无凭据的本地 HTTP 代理；请调整代理，或将 PAPERPILOT_OA_PROXY_MODE 设为 direct 后重试。') from exc
    return ('::1' if proxy.hostname=='::1' else '127.0.0.1',port)


class _PinnedConnection(http.client.HTTPConnection):
    def __init__(self,host,port,address,timeout,secure):
        super().__init__(host,port,timeout=timeout)
        self._address=address;self._secure=secure
    def connect(self):
        family,kind,proto,address=self._address
        sock=socket.socket(family,kind,proto)
        try:
            self.sock=sock;sock.settimeout(self.timeout);sock.connect(address)
            if self._secure:sock=ssl.create_default_context().wrap_socket(sock,server_hostname=self.host)
            self.sock=sock
        except BaseException:
            sock.close();raise


class _LocalProxyConnection(http.client.HTTPConnection):
    """CONNECT to the verified IP, then verify TLS against the origin hostname."""
    def __init__(self,host,port,address,timeout,secure,proxy):
        super().__init__(host,port,timeout=timeout)
        self._address=address;self._secure=secure;self._proxy=proxy
        ip=address[3][0]
        self.set_tunnel(f'[{ip}]' if address[0]==socket.AF_INET6 else ip,port)
    def connect(self):
        sock=socket.create_connection(self._proxy,self.timeout)
        try:
            self.sock=sock
            # The proxy receives a numeric, already-validated destination, not a hostname.
            self._tunnel()
            if self._secure:sock=ssl.create_default_context().wrap_socket(sock,server_hostname=self.host)
            self.sock=sock
        except BaseException:
            sock.close();self.sock=None;raise


def _public_request(url: str, *, timeout: float, max_bytes: int, accept: str):
    deadline=time.monotonic()+max(0.01,float(timeout));current=url
    for redirect in range(_MAX_REDIRECTS+1):
        parsed,host,port,addresses=_public_target(current,deadline)
        proxy=_local_proxy(parsed.scheme.lower());route='本地代理' if proxy else '直连网络'
        response=None;connection=None;last_error=None
        for address in addresses:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise ImportError('fetch_timeout',f'公开来源连接超时；请检查{route}后重试。',retryable=True)
            connection=(_LocalProxyConnection(host,port,address,remaining,parsed.scheme.lower()=='https',proxy) if proxy else _PinnedConnection(host,port,address,remaining,parsed.scheme.lower()=='https'))
            expired=threading.Event();holder={}
            def abort(conn=connection, holder=holder, expired=expired):
                expired.set()
                sock=holder.get('sock') or conn.sock
                if sock:
                    try:sock.shutdown(socket.SHUT_RDWR)
                    except OSError:pass
                    try:sock.close()
                    except OSError:pass
            timer=threading.Timer(remaining,abort);timer.daemon=True;timer.start()
            try:
                target=quote(parsed.path or '/',safe="/%:@!$&'()*+,;=-._~")
                if parsed.query:target+='?'+quote(parsed.query,safe="/%?:@!$&'()*+,;=-._~")
                host_header=f'[{host}]' if ':' in host else host
                if port!=(443 if parsed.scheme.lower()=='https' else 80):host_header+=f':{port}'
                connection.request('GET',target,headers={'Host':host_header,'Accept':accept,'Accept-Encoding':'identity','User-Agent':'PaperPilot/1.0'})
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TimeoutError('deadline')
                if connection.sock:connection.sock.settimeout(remaining)
                response=connection.getresponse()
                if response.fp and getattr(response.fp,'raw',None):holder['sock']=getattr(response.fp.raw,'_sock',None)
                break
            except (OSError,ssl.SSLError,http.client.HTTPException,ValueError) as exc:
                timer.cancel();last_error=exc;connection.close();connection=None
        if response is None:
            raise ImportError('fetch_timeout',f'公开来源暂时无法连接；请检查{route}后重试。',retryable=True) from last_error
        try:
            if response.status in {301,302,303,307,308}:
                location=response.getheader('Location')
                if not location or redirect>=_MAX_REDIRECTS:raise ImportError('redirect_invalid','来源跳转无效或次数过多，请核对公开链接。')
                target=urljoin(current,location)
                if parsed.scheme.lower()=='https' and urlparse(target).scheme.lower()!='https':
                    raise ImportError('redirect_insecure','来源跳转到不安全的 HTTP 地址，已停止抓取。')
                current=target;continue
            if response.status in {401,403}:raise ImportError('fetch_denied','该链接当前不可公开访问，已按非 OA 处理。')
            if response.status==404:raise ImportError('not_found','公开来源不存在或已失效。')
            if response.status>=500:raise ImportError('provider_unavailable','公开来源暂时不可用，请稍后重试。',retryable=True)
            if response.status>=400 or response.status<200 or response.status>=300:raise ImportError('fetch_failed','公开来源抓取失败。')
            if (response.getheader('Content-Encoding') or 'identity').strip().lower()!='identity':
                raise ImportError('unsupported_encoding','来源使用不支持的压缩方式，请尝试其他公开链接。')
            raw_length=response.getheader('Content-Length');length=None
            if raw_length is not None:
                try:
                    length=int(raw_length)
                    if length<0:raise ValueError('negative length')
                except ValueError as exc:raise ImportError('invalid_file','来源文件长度无效。') from exc
                if length>max_bytes:raise ImportError('file_too_large','公开文件超过大小限制。')
            chunks=[];total=0
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise ImportError('fetch_timeout',f'公开来源下载超时；请检查{route}后重试。',retryable=True)
                if connection.sock:connection.sock.settimeout(remaining)
                chunk=response.read1(min(65536,max_bytes-total+1))
                if expired.is_set() or time.monotonic()>=deadline:
                    raise ImportError('fetch_timeout',f'公开来源下载超时；请检查{route}后重试。',retryable=True)
                if not chunk:break
                total+=len(chunk)
                if total>max_bytes:raise ImportError('file_too_large','公开文件超过大小限制。')
                chunks.append(chunk)
            if length is not None and total!=length:raise ImportError('fetch_incomplete',f'公开文件传输不完整；请检查{route}后重试。',retryable=True)
            return b''.join(chunks),response.getheader('Content-Type') or ''
        except (OSError,http.client.HTTPException,ValueError) as exc:
            if expired.is_set():raise ImportError('fetch_timeout',f'公开来源下载超时；请检查{route}后重试。',retryable=True) from exc
            raise ImportError('fetch_incomplete',f'公开来源传输中断；请检查{route}后重试。',retryable=True) from exc
        finally:
            timer.cancel();response.close();connection.close()
    raise ImportError('redirect_invalid','来源跳转次数过多。')


def fetch_public(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[bytes, str, str]:
    content,content_type=_public_request(url,timeout=timeout,max_bytes=MAX_FETCH_BYTES,accept='application/pdf,text/html;q=0.9,*/*;q=0.1')
    if not (content.startswith(b'%PDF-') or 'pdf' in content_type.lower() or 'html' in content_type.lower() or 'text/' in content_type.lower()):
        raise ImportError('invalid_file','OA 响应不是可解析的 PDF 或 HTML')
    return content,content_type,hashlib.sha256(content).hexdigest()


def resolve_openalex(identifier: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    value=str(identifier or '').strip()
    doi=re.sub(r'^https?://(dx\.)?doi\.org/','',value.lower().removeprefix('doi:').strip()).rstrip('.')
    if not re.fullmatch(r'10\.\d{4,9}/[^\s?#]+',doi):
        raise ImportError('invalid_input','请输入有效 DOI 或 DOI 链接。')
    try:
        content,_=_public_request('https://api.openalex.org/works/https://doi.org/'+quote(doi,safe='/'),timeout=timeout,max_bytes=_METADATA_LIMIT,accept='application/json')
    except ImportError as exc:
        if exc.code=='not_found':raise ImportError('metadata_not_found','未找到该 DOI 的论文元数据。') from exc
        raise
    try:raw=json.loads(content)
    except (ValueError,UnicodeError) as exc:raise ImportError('metadata_invalid','元数据服务返回格式无效。',retryable=True) from exc
    if not isinstance(raw,dict):raise ImportError('metadata_invalid','元数据服务返回格式无效。',retryable=True)
    return raw


def metadata_from_raw(raw: dict[str, Any]) -> dict[str, Any]:
    source = (raw.get("primary_location") or {}).get("source") or {}
    oa = raw.get("open_access") or {}
    location = raw.get("best_oa_location") or raw.get("primary_location") or {}
    oa_url = normalize_url(location.get("pdf_url") or location.get("landing_page_url") or "")
    doi = str(raw.get("doi") or "").strip()
    authors = ", ".join(str((item.get("author") or {}).get("display_name") or "").strip() for item in (raw.get("authorships") or []) if (item.get("author") or {}).get("display_name"))
    return {
        "title": " ".join(str(raw.get("title") or "").split()),
        "authors": authors,
        "year": str(raw.get("publication_year") or ""),
        "venue": str(source.get("display_name") or "").strip(),
        "doi": doi,
        "canonical_url": normalize_url(raw.get("id") or (raw.get("primary_location") or {}).get("landing_page_url")),
        "abstract": str(raw.get("abstract") or "").strip(),
        "access_status": "open_access" if oa.get("is_oa") and oa_url else "metadata_only",
        "oa_url": oa_url,
        "provider": "openalex",
    }
