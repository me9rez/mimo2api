#!/usr/bin/env python3
"""mimo2api bridge — Xiaomi MiMo Desktop account → OpenAI-compatible API

Protocol (all Confirmed by live testing, 2026-09-11):
  POST https://mimo-server-cn.xiaomimimo.com/api/route/chat/completions
  Auth: passToken (plaintext in Desktop cookie db) → 5-step Xiaomi SSO → serviceToken cookie

Endpoints served:
  GET  /v1/models
  POST /v1/chat/completions   (stream + non-stream)

Usage:
  python3 mimo_bridge.py                 # listen on 127.0.0.1:4500
  PORT=4500 API_KEY=sk-your-key python3 mimo_bridge.py

passToken sources (first match wins):
  1. env MIMO_PASS_TOKEN
  2. mimo_pass_token.json next to this script  ({"passToken": "...", "userId": "...", "cUserId": "..."})
  3. MiMo Desktop cookie database (auto-read, Desktop locks it only while importing)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

API_BASE = "https://mimo-server-cn.xiaomimimo.com"
ACCOUNT_HOST = "account.xiaomi.com"
API_HOST = "mimo-server-cn.xiaomimimo.com"

PORT = int(os.environ.get("PORT", "4500"))
HOST = os.environ.get("HOST", "127.0.0.1")
API_KEY = os.environ.get("API_KEY", "sk-change-me")

PREVIEW_MODELS = ["mimo-x-pro-preview", "mimo-x-flash-preview"]  # legacy, kept until fully sunset
V26_MODELS = ["mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.6-pro-ultraspeed"]  # new catalog (2026-09-24 refresh)
TEXT_MODELS = V26_MODELS + PREVIEW_MODELS  # model-catalog.json: modelType=TEXT

# v2.6 defaults (validated live): work without thinking/temperature/top_p too,
# but preview models require them — apply defaults per family at request time.
DEFAULTS_PREVIEW = {"thinking": {"type": "enabled"}, "temperature": 1.0, "top_p": 0.95}
DEFAULTS_V26 = {}  # bare request works; keep pass-through

API_UA = (
    "miNative PC/Normal Windows_NT/10.0.19045 SDKV/1.0.0 "
    "DEVT/PC DEVS/Windows APP/miaccount_desktop APPV/0.1.0"
)
SSO_UA = "MiClaw/1.0"
COOKIE_TTL_S = 30 * 60  # serviceToken cache, matches Desktop behaviour

SSL_CTX_NOVERIFY = None  # set in main(); production path uses certifi/system CA

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------------------
# Upstream HTTP (urllib, no third-party deps)
# ----------------------------------------------------------------------------


class UpstreamError(Exception):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"upstream {status}: {body[:200]}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_opener: urllib.request.OpenerDirector | None = None


def _opener_init():
    global _opener
    if _opener is not None:
        return
    handlers: list = [NoRedirect()]
    try:
        import certifi

        import ssl

        ctx = ssl.create_default_context(cafile=certifi.where())
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    except ImportError:
        pass  # fall back to default CA handling
    _opener = urllib.request.build_opener(*handlers)


def http_req(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    cookie: str | None = None,
    timeout: float = 120.0,
) -> tuple[int, "email.message.Message", bytes]:
    _opener_init()
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        res = _opener.open(req, timeout=timeout)
        return res.status, res.headers, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def http_stream(
    url: str,
    *,
    headers: dict,
    body: bytes,
    timeout: float = 180.0,
):
    """Yield raw SSE lines. Caller must consume; errors raise UpstreamError."""
    _opener_init()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        res = _opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise UpstreamError(e.code, e.read().decode("utf-8", "replace")) from None

    if res.status >= 400:
        raise UpstreamError(res.status, res.read().decode("utf-8", "replace"))

    for raw in res:
        yield raw.decode("utf-8", "replace").rstrip("\r\n")


# ----------------------------------------------------------------------------
# Credential sources
# ----------------------------------------------------------------------------


def read_desktop_cookies() -> dict | None:
    """passToken/userId/cUserId from MiMo Desktop Chromium cookie db (plaintext)."""
    home = Path.home()
    candidates = [
        home / "Library/Application Support/Xiaomi MiMo/Partitions/xiaomi-account/Cookies",
        home / "Library/Application Support/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies",
        home / ".config/Xiaomi MiMo/Partitions/xiaomi-account/Network/Cookies",
    ]
    # Windows: %APPDATA%\Xiaomi MiMo\...（与 macOS 同样的分区结构；cookie 同样为明文 value）
    appdata = os.environ.get("APPDATA")
    if appdata:
        win_base = Path(appdata) / "Xiaomi MiMo" / "Partitions" / "xiaomi-account"
        candidates += [win_base / "Network" / "Cookies", win_base / "Cookies"]
    src = next((p for p in candidates if p.exists()), None)
    if src is None:
        return None
    fd, tmp = tempfile.mkstemp(prefix="mimo2api-ck-", suffix=".db")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        shutil.copy2(src, tmp_path)
        conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT name, value FROM cookies WHERE host_key = ?",
                ("." + ACCOUNT_HOST,),
            ).fetchall()
        finally:
            conn.close()
        jar = {n: v for n, v in rows}
        if not jar.get("passToken"):
            return None
        return {
            "passToken": jar["passToken"],
            "userId": jar.get("userId"),
            "cUserId": jar.get("cUserId"),
        }
    except PermissionError:
        # Windows: MiMo Desktop 运行时对 Cookies 库加独占锁（ERROR_SHARING_VIOLATION），无法拷贝
        print(
            f"[warn] cookie 库被占用，读不到 passToken：{src}\n"
            "       请完全退出 MiMo Desktop 后重启本服务（Windows 上托盘退出即可）。",
            file=sys.stderr,
        )
        return None
    except Exception:
        return None
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


CRED_SOURCE = "unset"


def load_credentials() -> dict:
    global CRED_SOURCE
    env = os.environ.get("MIMO_PASS_TOKEN")
    if env:
        CRED_SOURCE = "env (MIMO_PASS_TOKEN)"
        return {"passToken": env, "userId": os.environ.get("MIMO_USER_ID"), "cUserId": None}
    cred_file = HERE / "mimo_pass_token.json"
    if cred_file.exists():
        try:
            data = json.loads(cred_file.read_text())
            if data.get("passToken"):
                CRED_SOURCE = f"file ({cred_file})"
                return data
        except Exception:
            pass
    auto = read_desktop_cookies()
    if auto:
        CRED_SOURCE = "desktop cookie db (auto)"
        return auto
    raise SystemExit(
        "no passToken found. Login to MiMo Desktop once, or set MIMO_PASS_TOKEN, "
        f"or write {cred_file} with {{\"passToken\": \"...\"}}\n"
        "  Windows: MiMo Desktop 运行时对 cookie 库加独占锁，先退出 Desktop 再启动；"
        "或用 dump_windows_token.py 导出 mimo_pass_token.json（之后 Desktop 可保持运行）。"
    )


# ----------------------------------------------------------------------------
# SSO: passToken → serviceToken cookie (5 steps, mirrors Desktop)
# ----------------------------------------------------------------------------

_session_lock = threading.Lock()
_session: dict = {"key": None, "cookie": None, "at": 0.0}


def _absorb(jar: dict, headers) -> None:
    for v in headers.get_all("Set-Cookie") or []:
        c = v.strip().split(";", 1)[0]
        if "=" in c:
            k, val = c.split("=", 1)
            if val.strip():
                jar[k.strip()] = val.strip()


def _client_sign(nonce: str, ssecurity: str | None) -> str:
    payload = f"nonce={nonce}"
    if ssecurity and ssecurity.strip():
        payload += f"&{ssecurity}"
    digest = hashlib.sha1(payload.encode()).digest()
    b64 = base64.b64encode(digest).decode()
    return b64.replace("+", "%2B").replace("/", "%2F").replace("=", "%3D")


def _json_body(raw: bytes) -> dict:
    t = raw.decode("utf-8", "replace").strip()
    while t.startswith("&"):
        t = t[1:]
    if t.startswith("START&&&"):
        t = t[8:]
    return json.loads(t)


def _normalize_gateway(url: str) -> str:
    """Xiaomi grey-release bug (observed 2026-09-12): /api/user/xiaomi/me sometimes 302s
    to the preview gateway (preview-cn-server.xiaomimimo.mioffice.cn) whose sts callback
    is not whitelisted by account.xiaomi.com (code 10025). Pin everything back to the
    production gateway; sign/followup params stay valid across hosts."""
    u = urllib.parse.urlparse(url)
    if u.netloc != API_HOST:
        return urllib.parse.urlunparse(u._replace(netloc=API_HOST, scheme="https"))
    return url


def _sso(cred: dict) -> dict:
    """Full SSO chain → {serviceToken, mimopc_ph, mimopc_slh, userId}."""
    jar = {k: v for k, v in cred.items() if k in ("passToken", "userId", "cUserId") and v}
    ck = lambda: "; ".join(f"{k}={v}" for k, v in jar.items() if v)  # noqa: E731

    # 1. unauthenticated API → 302 to serviceLogin, Location query carries sts callback
    st, hd, _ = http_req(
        f"{API_BASE}/api/user/xiaomi/me", headers={"User-Agent": API_UA}, cookie=ck()
    )
    loc = hd.get("Location", "")
    if st != 302 or "serviceLogin" not in loc:
        raise UpstreamError(st, f"SSO step1 failed: {loc[:120]}")
    sts_callback = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("callback", [None])[0]
    if not sts_callback:
        raise UpstreamError(500, "SSO step1: no callback in 302")
    sts_callback = _normalize_gateway(sts_callback)
    if not sts_callback:
        raise UpstreamError(500, "SSO step1: no callback in 302")

    # 2. passportapi SSO phase1 → nonce/ssecurity
    st, hd, body = http_req(
        f"https://{ACCOUNT_HOST}/pass/serviceLogin?sid=passportapi&_json=true",
        headers={"User-Agent": SSO_UA, "Accept": "application/json"},
        cookie=ck(),
    )
    j1 = _json_body(body)
    if st != 200 or not j1.get("nonce"):
        raise UpstreamError(st, f"SSO step2 failed: {body[:120]}")

    # 3. phase2 (+clientSign) → account-level serviceToken
    sep = "&" if "?" in j1["location"] else "?"
    st, hd, _ = http_req(
        f"{j1['location']}{sep}clientSign={_client_sign(j1['nonce'], j1.get('ssecurity'))}",
        headers={"User-Agent": SSO_UA},
        cookie=ck(),
    )
    _absorb(jar, hd)

    # 4. mimopc SSO → sts ticket
    st, hd, body = http_req(
        f"https://{ACCOUNT_HOST}/pass/serviceLogin?sid=mimopc"
        f"&callback={urllib.parse.quote(sts_callback, safe='')}&_json=true",
        headers={"User-Agent": SSO_UA, "Accept": "application/json"},
        cookie=ck(),
    )
    j3 = _json_body(body)
    loc3 = j3.get("location", "")
    if "/api/sts" not in loc3:
        raise UpstreamError(st, f"SSO step4 failed: {loc3[:120]}")
    loc3 = _normalize_gateway(loc3)

    # 5. sts → Set-Cookie: serviceToken
    st, hd, _ = http_req(loc3, headers={"User-Agent": API_UA}, cookie=ck())
    _absorb(jar, hd)

    if not jar.get("serviceToken"):
        raise UpstreamError(500, "SSO step5: no serviceToken issued (passToken expired?)")
    return {k: jar[k] for k in ("serviceToken", "mimopc_ph", "mimopc_slh", "userId") if jar.get(k)}


def get_service_cookie(cred: dict, force: bool = False) -> str:
    key = hashlib.sha256(cred["passToken"].encode()).hexdigest()
    with _session_lock:
        hit = (
            not force
            and _session["cookie"]
            and _session["key"] == key
            and time.time() - _session["at"] < COOKIE_TTL_S
        )
        if hit:
            return _session["cookie"]
        sess = _sso(cred)
        cookie = "; ".join(f"{k}={v}" for k, v in sess.items())
        _session.update(key=key, cookie=cookie, at=time.time())
        return cookie


# ----------------------------------------------------------------------------
# Model name mapping + request prep
# ----------------------------------------------------------------------------


def bare(model: str) -> str:
    return model.split("/", 1)[1] if "/" in model else model


def upstream_model(model: str) -> str:
    b = bare(model)
    return f"xiaomi/{b}" if b in PREVIEW_MODELS else b


def prepare_body(body: dict) -> dict:
    out = dict(body)
    req_model = bare(out.pop("model", V26_MODELS[0]))
    if req_model not in TEXT_MODELS:
        raise UpstreamError(400, f"model {req_model} not available; text models: {TEXT_MODELS}")
    out["model"] = f"xiaomi/{req_model}"
    # Family-specific defaults (Confirmed 2026-09-13):
    # - preview models require thinking/temperature/top_p
    # - v2.6 models work bare; pass-through untouched
    if req_model in PREVIEW_MODELS:
        for k, v in DEFAULTS_PREVIEW.items():
            out.setdefault(k, v)
    return out


# ----------------------------------------------------------------------------
# Bridge server
# ----------------------------------------------------------------------------


def sse_error(status: int, message: str) -> str:
    payload = {"error": {"message": message, "type": "upstream_error", "code": status}}
    return f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cred: dict = {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.address_string()} {fmt % args}\n")

    # -- helpers ------------------------------------------------------------

    def _authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if API_KEY == "none":
            return True
        return auth in (f"Bearer {API_KEY}", f"Basic {base64.b64encode(f'x:{API_KEY}'.encode()).decode()}")

    def _json(self, status: int, payload: dict):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # -- routes ---------------------------------------------------------------

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/v1/models":
            if not self._authed():
                return self._json(401, {"error": {"message": "invalid api key"}})
            now = int(time.time())
            return self._json(200, {
                "object": "list",
                "data": [
                    {"id": m, "object": "model", "created": now, "owned_by": "xiaomi"}
                    for m in TEXT_MODELS
                ],
            })
        if path == "/health":
            return self._json(200, {"ok": True, "upstream": API_BASE})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path != "/v1/chat/completions":
            return self._json(404, {"error": {"message": "not found"}})
        if not self._authed():
            return self._json(401, {"error": {"message": "invalid api key"}})

        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": {"message": "invalid json"}})

        try:
            payload = prepare_body(body)
        except UpstreamError as e:
            return self._json(e.status, {"error": {"message": e.body}})

        stream = bool(payload.get("stream"))
        url = f"{API_BASE}/api/route/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": API_UA,
        }
        raw = json.dumps(payload).encode()

        force = False
        for attempt in (1, 2):  # 401 → refresh serviceToken once
            try:
                cookie = get_service_cookie(self.cred, force=force)
            except UpstreamError as e:
                return self._json(502, {"error": {"message": f"session unavailable: {e.body}"}})

            if not stream:
                st, _, resp = http_req(url, method="POST", headers=headers, body=raw, cookie=cookie)
                if st == 401 and attempt == 1:
                    force = True
                    continue
                if st >= 400:
                    return self._json(st, {"error": {"message": resp.decode("utf-8", "replace")[:500]}})
                # pass-through (upstream is already OpenAI-shaped)
                return self._raw(200, "application/json", resp)

            # stream: merge headers + retry-on-401 requires buffering upstream status first
            try:
                return self._pipe_stream(url, headers, raw, cookie, force_refresh=attempt == 1)
            except UpstreamError as e:
                if e.status == 401 and attempt == 1:
                    force = True
                    continue
                return self._sse_err(e.status, e.body)
            return

    # -- streaming passthrough ------------------------------------------------

    def _pipe_stream(self, url, headers, raw, cookie, force_refresh: bool):
        """Stream upstream SSE → client, filtering keep-alives. 401 inside stream is
        handled by pre-checking the first event before committing headers."""
        headers = dict(headers, Cookie=cookie)
        it = http_stream(url, headers=headers, body=raw)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        first = True
        for line in it:
            if not line.strip():
                continue
            if first:
                first = False
                if line.startswith("data:") and '"error"' in line[:200] and "401" in line[:200]:
                    raise UpstreamError(401, line)
            self.wfile.write((line + "\n\n").encode())
            self.wfile.flush()

    def _sse_err(self, status: int, message: str):
        try:
            self.wfile.write(sse_error(status, message).encode())
        except Exception:
            pass

    def _raw(self, status: int, ctype: str, data: bytes):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    Handler.cred = load_credentials()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mimo2api bridge listening on http://{HOST}:{PORT}")
    print(f"  models: {', '.join(TEXT_MODELS)}")
    print(f"  api key: {API_KEY}")
    print(f"  credential: {CRED_SOURCE}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
