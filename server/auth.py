"""Dashboard session auth — same design as remote_pc/auth.py:

- Constant-time credential comparison (no username enumeration).
- Per-IP login rate limiting / lockout.
- Stateless HMAC-signed session tokens ("<exp>.<nonce>.<sig>") so a logged-in
  browser survives server restarts when DASH_SECRET is set.
"""
import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict

import config

_attempts = defaultdict(list)   # ip -> [failure timestamps]
_revoked = set()                # best-effort logout list (cleared on restart)


def hash_password(password: str, iterations: int = 240_000) -> str:
    """Produce a "pbkdf2_sha256$iters$salt$hash" string for DASH_PASSWORD_HASH."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (
        iterations,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def _verify_hash(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_b64, hash_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
    except Exception:
        return False
    return hmac.compare_digest(dk, expected)


def check_credentials(username: str, password: str) -> bool:
    user_ok = hmac.compare_digest(username or "", config.DASH_USER)
    if config.DASH_PASSWORD_HASH:
        pass_ok = _verify_hash(password or "", config.DASH_PASSWORD_HASH)
    else:
        pass_ok = hmac.compare_digest(password or "", config.DASH_PASSWORD or "")
    return user_ok and pass_ok


def is_locked(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _attempts[ip] if now - t < config.DASH_LOCKOUT_SECONDS]
    _attempts[ip] = recent
    return len(recent) >= config.DASH_MAX_LOGIN_ATTEMPTS


def record_failure(ip: str) -> None:
    _attempts[ip].append(time.time())


def clear_failures(ip: str) -> None:
    _attempts.pop(ip, None)


def _sign(msg: str) -> str:
    sig = hmac.new(config.DASH_SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def create_session() -> str:
    exp = int(time.time() + config.DASH_SESSION_TTL)
    payload = "%d.%s" % (exp, secrets.token_urlsafe(8))
    return "%s.%s" % (payload, _sign(payload))


def validate_token(token: str) -> bool:
    if not token or token in _revoked:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    exp_s, nonce, sig = parts
    payload = "%s.%s" % (exp_s, nonce)
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return time.time() <= int(exp_s)
    except ValueError:
        return False


def destroy_session(token: str) -> None:
    if token:
        _revoked.add(token)
