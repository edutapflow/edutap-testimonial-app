"""Password gate for ALL Streamlit HTTP/WebSocket routes, with signed cookies."""
from collections import OrderedDict
from http.cookies import SimpleCookie
import hashlib
import hmac
import html
import os
import secrets
import time
import uuid
from urllib.parse import urlsplit

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, PlainTextResponse

COOKIE = '__Host-edutap_session'
CSRF_COOKIE = '__Host-edutap_csrf'
OWNER_HEADER = b'x-edutap-session'
SEVEN_DAYS = 7 * 24 * 60 * 60


class PasswordGate:
    def __init__(self, app, *, password=None, secret=None):
        self.app = app
        self.password = password if password is not None else os.getenv('APP_PASSWORD', '')
        secret = secret if secret is not None else os.getenv('AUTH_COOKIE_SECRET', '')
        if not self.password:
            raise RuntimeError('Set APP_PASSWORD in Render before deploying.')
        if len(secret) < 32:
            raise RuntimeError('Set AUTH_COOKIE_SECRET to a stable random value of at least 32 characters.')
        self.sessions = URLSafeTimedSerializer(secret, salt='edutap-browser-session-v1')
        self.csrf = URLSafeTimedSerializer(secret, salt='edutap-csrf-v1')
        self.version = hmac.new(secret.encode(), self.password.encode(), hashlib.sha256).hexdigest()
        self.attempts = OrderedDict()

    def identity(self, cookie):
        try:
            item = self.sessions.loads(cookie, max_age=SEVEN_DAYS)
            if not hmac.compare_digest(str(item.get('version', '')), self.version):
                return None
            if item.get('expires', 0) <= time.time():
                return None
            return str(uuid.UUID(item['sid']))
        except (BadSignature, SignatureExpired, ValueError, TypeError, KeyError, AttributeError):
            return None

    def login_page(self, message='', status=200, *, logout=False):
        nonce = self.csrf.dumps(secrets.token_urlsafe(24))
        title = 'Log out' if logout else 'EduTap Access'
        action = '/auth/logout' if logout else '/auth/login'
        fields = '' if logout else '''<label>Password</label>
        <input type="password" name="password" autocomplete="current-password" required>
        <label class="remember"><input type="checkbox" name="remember" value="yes" checked> Remember me for 7 days</label>'''
        page = f'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
        <title>{title}</title><style>body{{font:16px system-ui;background:#0b1120;color:#e5e7eb;margin:0;padding:60px 20px}}
        main{{max-width:380px;margin:60px auto;padding:30px;background:#111827;border-radius:16px}}
        label,input,button{{box-sizing:border-box}}input[type=password]{{width:100%;padding:12px;margin:10px 0 20px;background:#0b1120;border:1px solid #475569;color:white;border-radius:8px}}
        .remember{{display:block;font-size:14px;margin:0 0 20px}}button{{padding:12px 22px;background:#0891b2;color:white;border:0;border-radius:8px;cursor:pointer}}
        a{{color:#67e8f9}}.error{{color:#fda4af}}</style></head><body><main><h1>{title}</h1>
        <p class="error">{html.escape(message)}</p><form method="post" action="{action}">
        <input type="hidden" name="csrf" value="{html.escape(nonce, quote=True)}">{fields}
        <button type="submit">{'Log out' if logout else 'Open app'}</button></form>
        {'<p><a href="/">Return to app</a></p>' if logout else ''}</main></body></html>'''
        response = HTMLResponse(page, status_code=status, headers={
            'Cache-Control': 'no-store', 'Referrer-Policy': 'same-origin',
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
        })
        response.set_cookie(CSRF_COOKIE, nonce, max_age=600, secure=True, httponly=True, samesite='strict')
        return response

    def csrf_valid(self, request, form):
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return False
        sent = str(form.get('csrf') or '')
        cookie = request.cookies.get(CSRF_COOKIE, '')
        if not sent or not hmac.compare_digest(sent, cookie):
            return False
        try:
            self.csrf.loads(sent, max_age=600)
            return True
        except BadSignature:
            return False

    def rate_limited(self, peer):
        now = time.monotonic()
        timestamps = [stamp for stamp in self.attempts.pop(peer, []) if now - stamp < 60]
        limited = len(timestamps) >= 10
        if not limited:
            timestamps.append(now)
        self.attempts[peer] = timestamps
        while len(self.attempts) > 2048:
            self.attempts.popitem(last=False)
        return limited

    async def auth_route(self, request, owner):
        logout = request.url.path == '/auth/logout'
        if logout and not owner:
            return RedirectResponse('/auth/login', status_code=303)
        if request.method == 'GET':
            if owner and not logout:
                return RedirectResponse('/', status_code=303)
            return self.login_page(logout=logout)
        if request.method != 'POST':
            return PlainTextResponse('Method not allowed', status_code=405)
        if int(request.headers.get('content-length', '0')) > 8192:
            return PlainTextResponse('Request too large', status_code=413)
        form = await request.form(max_fields=4, max_files=0)
        if not self.csrf_valid(request, form):
            return self.login_page('Please reload this page and try again.', 403, logout=logout)
        if logout:
            response = RedirectResponse('/auth/login', status_code=303)
            response.delete_cookie(COOKIE, secure=True, httponly=True, samesite='lax')
            return response
        peer = request.client.host if request.client else 'unknown'
        if self.rate_limited(peer):
            return self.login_page('Too many attempts. Wait one minute and retry.', 429)
        supplied = str(form.get('password') or '')
        if not hmac.compare_digest(supplied.encode(), self.password.encode()):
            return self.login_page('Incorrect password.', 401)
        remember = form.get('remember') == 'yes'
        ttl = SEVEN_DAYS if remember else 12 * 60 * 60
        token = self.sessions.dumps(dict(sid=str(uuid.uuid4()), version=self.version, expires=int(time.time()) + ttl))
        response = RedirectResponse('/', status_code=303, headers={'Cache-Control': 'no-store'})
        response.set_cookie(COOKIE, token, max_age=ttl if remember else None,
                            secure=True, httponly=True, samesite='lax')
        response.delete_cookie(CSRF_COOKIE, secure=True, httponly=True, samesite='strict')
        return response

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        cookie = SimpleCookie()
        try:
            for key, value in scope.get('headers', []):
                if key.lower() == b'cookie':
                    cookie.load(value.decode('latin-1'))
            owner = self.identity(cookie[COOKIE].value) if COOKIE in cookie else None
        except Exception:
            owner = None
        path = scope.get('path', '')
        if scope['type'] == 'http' and path in ('/auth/login', '/auth/logout'):
            request = Request(scope, receive)
            try:
                response = await self.auth_route(request, owner)
            except (ValueError, UnicodeError):
                response = PlainTextResponse('Invalid request', status_code=400)
            return await response(scope, receive, send)
        if path == '/_stcore/health' and scope['type'] == 'http':
            return await self.app(scope, receive, send)
        if not owner:
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 4401})
                return
            return await RedirectResponse('/auth/login', status_code=303)(scope, receive, send)
        # Trust only our verified identity. Strip any browser-supplied lookalike.
        trusted = dict(scope)
        trusted['headers'] = [(k, v) for k, v in scope.get('headers', []) if k.lower() != OWNER_HEADER]
        trusted['headers'].append((OWNER_HEADER, owner.encode('ascii')))
        await self.app(trusted, receive, send)
