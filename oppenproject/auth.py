from __future__ import annotations

import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import quote, urlsplit, urlunsplit

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from .config import APP_NAME, Settings

SCOPE = "governance:read"
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 86400
COOKIE = "oppen_authorize"
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
    return salt + ":" + key.hex()


class Store:
    def __init__(self, settings: Settings):
        settings.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.state_dir.chmod(0o700)
        if os.name == "nt":
            from .windows_fs import make_private

            make_private(settings.state_dir, directory=True)
        self.path = settings.state_dir / "oauth.sqlite3"
        with self.connection() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS state (kind TEXT, key TEXT, value TEXT, expires REAL, "
                "PRIMARY KEY (kind, key))"
            )
        self.path.chmod(0o600)
        if os.name == "nt":
            make_private(self.path)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def write(db, kind, key, value, expires=0):
        db.execute(
            "INSERT OR REPLACE INTO state VALUES (?, ?, ?, ?)", (kind, key, json.dumps(value), expires)
        )

    def put(self, kind, key, value, expires=0):
        with self.connection() as db:
            db.execute("DELETE FROM state WHERE expires > 0 AND expires < ?", (time.time(),))
            self.write(db, kind, key, value, expires)

    def get(self, kind, key):
        with self.connection() as db:
            row = db.execute(
                "SELECT value FROM state WHERE kind=? AND key=? AND (expires=0 OR expires>?)",
                (kind, key, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def take(self, kind, key):
        with self.connection() as db:
            row = db.execute(
                "DELETE FROM state WHERE kind=? AND key=? AND (expires=0 OR expires>?) RETURNING value",
                (kind, key, time.time()),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def revoke_all(self):
        with self.connection() as db:
            db.execute("DELETE FROM state WHERE kind != 'meta'")


class OAuthProvider:
    """Single-owner OAuth provider; the official MCP SDK handles protocol validation."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.store = Store(settings)
        self.password_hash = self.store.get("meta", "owner_password")
        if not self.password_hash:
            raise ValueError("Owner login not configured; run run.py configure first")
        issuer = self.store.get("meta", "issuer")
        if issuer != settings.public_url:
            self.store.revoke_all()
            self.store.put("meta", "issuer", settings.public_url)
        if self.store.get("meta", "access_scope") != SCOPE:
            # Force fresh consent when upgrading from broad project-file access.
            self.store.revoke_all()
            self.store.put("meta", "access_scope", SCOPE)

    def redirect_allowed(self, uri):
        uri = str(uri)
        # The host/path constraints apply before SDK exact-match registration checks.
        return bool(
            uri in self.settings.extra_redirect_uris
            or uri == "https://chatgpt.com/connector_platform_oauth_redirect"
            or re.fullmatch(r"https://chatgpt\.com/connector/oauth/[A-Za-z0-9_-]{1,200}", uri)
        )

    async def get_client(self, client_id):
        data = self.store.get("client", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if not client_info.redirect_uris or not all(
            self.redirect_allowed(u) for u in client_info.redirect_uris
        ):
            raise RegistrationError(
                "invalid_redirect_uri", "Only ChatGPT callbacks or locally configured exact URIs"
            )
        if client_info.token_endpoint_auth_method not in {
            "none",
            "client_secret_post",
            "client_secret_basic",
        }:
            raise RegistrationError("invalid_client_metadata", "Unsupported token authentication method")
        with self.store.connection() as db:
            count = db.execute("SELECT count(*) FROM state WHERE kind='client'").fetchone()[0]
            if count >= 1000:
                raise RegistrationError("invalid_client_metadata", "Registration capacity reached")
            self.store.write(db, "client", client_info.client_id, client_info.model_dump(mode="json"))

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource != self.settings.resource:
            raise AuthorizeError("invalid_request", "resource must equal the advertised MCP resource")
        if params.scopes != [SCOPE]:
            raise AuthorizeError("invalid_scope", f"Only {SCOPE} is supported")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "Invalid S256 PKCE challenge")
        if not self.redirect_allowed(params.redirect_uri):
            raise AuthorizeError("invalid_request", "Redirect is no longer allowed")
        transaction = secrets.token_urlsafe(32)
        self.store.put(
            "pending",
            digest(transaction),
            {
                "client_id": client.client_id,
                "client_name": (client.client_name or "MCP client")[:200],
                "params": params.model_dump(mode="json"),
            },
            time.time() + 600,
        )
        return self.settings.public_url + "/consent?transaction=" + transaction

    def password_valid(self, password):
        if not isinstance(password, str) or len(password) > 256:
            return False
        expected = self.store.get("meta", "owner_password")
        return secrets.compare_digest(hash_password(password, expected.split(":")[0]), expected)

    async def consent(self, request: Request):
        if request.method == "GET":
            transaction = request.query_params.get("transaction", "")
            data = self.store.get("pending", digest(transaction))
            if not data:
                return JSONResponse(
                    {"error": "Authorization expired; reconnect from ChatGPT"}, status_code=400
                )
            browser_secret = secrets.token_urlsafe(32)
            csrf = secrets.token_urlsafe(32)
            data.update(browser=digest(browser_secret), csrf=digest(csrf))
            self.store.put("pending", digest(transaction), data, time.time() + 600)
            response = self.consent_response(data, transaction, csrf)
            response.set_cookie(
                COOKIE,
                browser_secret,
                max_age=600,
                httponly=True,
                secure=self.settings.secure_cookie,
                samesite="lax",
                path="/consent",
            )
            return response
        form = await request.form()
        transaction = str(form.get("transaction", ""))
        data = self.store.get("pending", digest(transaction))
        origin = request.headers.get("origin")
        if (
            not data
            or origin != self.settings.public_url
            or not secrets.compare_digest(data.get("browser", "!"), digest(request.cookies.get(COOKIE, "")))
            or not secrets.compare_digest(data.get("csrf", "!"), digest(str(form.get("csrf", ""))))
        ):
            return JSONResponse({"error": "Invalid authorization session"}, status_code=403)
        params = AuthorizationParams.model_validate(data["params"])
        if form.get("decision") == "deny":
            self.store.take("pending", digest(transaction))
            return RedirectResponse(
                construct_redirect_uri(
                    str(params.redirect_uri),
                    error="access_denied",
                    state=params.state,
                    iss=self.settings.public_url,
                ),
                status_code=303,
            )
        if not await run_in_threadpool(self.password_valid, form.get("password", "")):
            return self.consent_response(
                data, transaction, str(form.get("csrf", "")), "访问口令不正确，请重试。", status_code=401
            )
        if not self.store.take("pending", digest(transaction)):
            return JSONResponse({"error": "Authorization already used"}, status_code=400)
        code = secrets.token_urlsafe(32)
        value = AuthorizationCode(
            code="",
            client_id=data["client_id"],
            expires_at=time.time() + 120,
            **params.model_dump(exclude={"state"}),
        ).model_dump(mode="json")
        self.store.put("code", digest(code), value, value["expires_at"])
        response = RedirectResponse(
            construct_redirect_uri(
                str(params.redirect_uri), code=code, state=params.state, iss=self.settings.public_url
            ),
            status_code=303,
        )
        response.delete_cookie(COOKIE, path="/consent")
        return response

    def consent_response(self, data, transaction, csrf, error="", status_code=200):
        # Chromium also checks the form's 303 redirect against the originating page's CSP.
        # Permit only the redirect already validated for this authorization transaction.
        redirect = urlsplit(data["params"]["redirect_uri"])
        source = quote(urlunsplit((redirect.scheme, redirect.netloc, redirect.path, "", "")), safe=":/%[]")
        policy = CONTENT_SECURITY_POLICY.replace("form-action 'self'", "form-action 'self' " + source)
        return HTMLResponse(
            self.consent_html(data, transaction, csrf, error),
            status_code=status_code,
            headers={"Content-Security-Policy": policy},
        )

    def consent_html(self, data, transaction, csrf, error=""):
        esc = html.escape
        redirect = data["params"]["redirect_uri"]
        return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>授权 {APP_NAME}</title>
<style>body{{font:16px system-ui;background:#f3f4f6;color:#17212d;margin:0;padding:8vh 20px}}
main{{max-width:540px;margin:auto;background:white;padding:32px;border-radius:16px}}
h1{{font-size:26px}}p{{line-height:1.6;overflow-wrap:anywhere}}small{{color:#536172}}
input{{box-sizing:border-box;width:100%;padding:12px;border:1px solid #9daab8;border-radius:8px}}
button{{padding:12px 20px;margin-top:20px;border:0;border-radius:8px;cursor:pointer}}
button[value=allow]{{background:#164e63;color:white}}.error{{color:#b42318}}</style></head>
<body><main><small>{APP_NAME} · 项目治理</small><h1>授权读取项目治理文件</h1>
<p>客户端：<strong>{esc(data["client_name"])}</strong></p>
<p>授权后，ChatGPT 可发现受管理项目，只能读取治理索引、Memory 和 Attention 的索引及已登记条目。
此范围也适用于之后自动发现的项目。权限：<code>{SCOPE}</code>。</p>
<p>数据、源码、Results、Deliverables、Audit、README 和其他 Canonical 正文不开放。
治理文档中的链接不会授予目标文件的访问权限。旧版 R v2 项目只开放 project.md。</p>
<p>治理文档本身的正文会提供给 ChatGPT。不提供修改、删除或执行命令。</p>
<p><small>授权回调：{esc(redirect)}</small></p><p class="error">{esc(error)}</p>
<form method="post" action="/consent"><input type="hidden" name="transaction" value="{esc(transaction)}">
<input type="hidden" name="csrf" value="{esc(csrf)}"><label for="password">本机生成的访问口令</label>
<input id="password" type="password" name="password" autocomplete="current-password" maxlength="256">
<button name="decision" value="allow">登录并授权治理文件只读</button>
<button name="decision" value="deny">取消</button></form></main></body></html>'''

    async def load_authorization_code(self, client, authorization_code):
        data = self.store.get("code", digest(authorization_code))
        if not data or data["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate({**data, "code": authorization_code})

    def issue(self, client_id, scopes, *, consume_kind, consume_key, grant=None):
        now = int(time.time())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        grant = grant or secrets.token_urlsafe(24)
        with self.store.connection() as db:
            # Consuming the code/refresh and creating replacements is one SQLite transaction.
            row = db.execute(
                "DELETE FROM state WHERE kind=? AND key=? AND expires>? RETURNING value",
                (consume_kind, consume_key, now),
            ).fetchone()
            if not row:
                raise TokenError("invalid_grant", "Credential expired or already used")
            if consume_kind == "refresh":
                self.store.write(
                    db,
                    "used_refresh",
                    consume_key,
                    {"grant": grant, "client_id": client_id},
                    now + REFRESH_TTL,
                )
            common = {
                "client_id": client_id,
                "scopes": scopes,
                "resource": self.settings.resource,
                "issuer": self.settings.public_url,
                "grant": grant,
            }
            self.store.write(
                db, "access", digest(access), {**common, "expires_at": now + ACCESS_TTL}, now + ACCESS_TTL
            )
            self.store.write(
                db, "refresh", digest(refresh), {**common, "expires_at": now + REFRESH_TTL}, now + REFRESH_TTL
            )
        return OAuthToken(
            access_token=access,
            refresh_token=refresh,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        if authorization_code.resource != self.settings.resource:
            raise TokenError("invalid_grant", "Wrong resource")
        return self.issue(
            client.client_id,
            authorization_code.scopes,
            consume_kind="code",
            consume_key=digest(authorization_code.code),
        )

    def valid_token(self, kind, token):
        data = self.store.get(kind, digest(token))
        if (
            not data
            or data["issuer"] != self.settings.public_url
            or data["resource"] != self.settings.resource
            or data["scopes"] != [SCOPE]
            or data["expires_at"] <= time.time()
            or self.store.get("revoked", data["grant"])
        ):
            return None
        return data

    async def load_access_token(self, token):
        data = self.valid_token("access", token)
        return AccessToken.model_validate({**data, "token": token}) if data else None

    async def verify_token(self, token):
        return await self.load_access_token(token)

    async def load_refresh_token(self, client, refresh_token):
        used = self.store.get("used_refresh", digest(refresh_token))
        if used and used["client_id"] == client.client_id:
            self.store.put("revoked", used["grant"], True, time.time() + REFRESH_TTL)
            return None
        data = self.valid_token("refresh", refresh_token)
        if not data or data["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        data = self.valid_token("refresh", refresh_token.token)
        if not data or scopes != [SCOPE]:
            raise TokenError("invalid_grant", "Invalid refresh token or scopes")
        return self.issue(
            client.client_id,
            scopes,
            consume_kind="refresh",
            consume_key=digest(refresh_token.token),
            grant=data["grant"],
        )

    async def revoke_token(self, token):
        for kind in ("access", "refresh", "used_refresh"):
            data = self.store.get(kind, digest(token.token))
            if data:
                self.store.put("revoked", data["grant"], True, time.time() + REFRESH_TTL)


def configure_owner(settings: Settings, rotate=False):
    store = Store(settings)
    if store.get("meta", "owner_password") and not rotate:
        return
    password = secrets.token_urlsafe(32)
    store.revoke_all()
    store.put("meta", "owner_password", hash_password(password))
    target = settings.state_dir / "owner-access.txt"
    if os.name == "nt":
        from .windows_fs import make_private, open_beneath

        with open_beneath(target.parent, (target.name,), write=True) as fd:
            os.ftruncate(fd, 0)
            os.write(fd, (password + "\n").encode("utf-8"))
        make_private(target)
    else:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(password + "\n")
    target.chmod(0o600)
