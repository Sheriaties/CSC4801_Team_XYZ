"""Security helpers: password hashing, HTML escaping, CSRF tokens.

Password storage uses werkzeug's established password hash (scrypt/PBKDF2),
never plain text or a reversible cipher (FP-AUTH-2). Templates rely on Jinja2
autoescaping for user-controlled text (FP-SEC-3); ``escape_html`` is the
explicit helper for any text turned into markup outside a template.
"""

import hmac
import secrets
import threading
import time
from collections import deque

from markupsafe import escape
from werkzeug.security import check_password_hash, generate_password_hash


def hash_password(password: str) -> str:
    """Hash a password with an established, salted password-hashing function."""
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time verification of a password against its stored hash."""
    return check_password_hash(password_hash, password)


def escape_html(text: str) -> str:
    """Escape user-controlled text so it is never executable markup (FP-SEC-3)."""
    return str(escape(text if text is not None else ""))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_token_valid(session_token: str, submitted_token: str) -> bool:
    # Compare bytes: compare_digest raises TypeError on non-ASCII str (would be a 500).
    return bool(session_token) and hmac.compare_digest(
        session_token.encode(), (submitted_token or "").encode()
    )


class RateLimiter:
    """Small in-process sliding-window limiter (one instance per app).

    ``blocked`` returns the seconds to wait (0 when allowed); ``hit`` records an
    event. Process-local on purpose: it slows online password guessing and
    paid-API abuse in the single-container deployment without any external
    service. Memory is bounded: expired keys are swept periodically and the
    number of tracked keys is capped (oldest evicted), so unique-key floods
    (e.g. many different emails) cannot grow it without limit.
    """

    MAX_KEYS = 10_000
    SWEEP_EVERY = 256

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._events = {}
        self._windows = {}
        self._hits = 0
        self._lock = threading.Lock()

    def size(self) -> int:
        """Number of tracked keys (an empty limiter must stay truthy-safe: use `is None`)."""
        return len(self._events)

    def _live(self, key, window):
        events = self._events.get(key)
        if events is None:
            return ()  # never create state for a key that has no events
        cutoff = self._clock() - window
        while events and events[0] <= cutoff:
            events.popleft()
        if not events:
            self._events.pop(key, None)
            self._windows.pop(key, None)
            return ()
        return events

    def blocked(self, key, limit: int, window: float) -> int:
        with self._lock:
            events = self._live(key, window)
            if len(events) < limit:
                return 0
            return max(1, int(events[0] + window - self._clock()) + 1)

    def hit(self, key, window: float = 3600) -> None:
        with self._lock:
            if key not in self._events:
                self._events[key] = deque()
            self._windows[key] = max(window, self._windows.get(key, 0))
            self._events[key].append(self._clock())
            self._hits += 1
            if self._hits % self.SWEEP_EVERY == 0 or len(self._events) > self.MAX_KEYS:
                self._sweep()

    def _sweep(self):
        for key in list(self._events):
            self._live(key, self._windows.get(key, 0))
        while len(self._events) > self.MAX_KEYS:  # still too many live keys: drop oldest
            oldest = next(iter(self._events))
            self._events.pop(oldest)
            self._windows.pop(oldest, None)

    def reset(self, key) -> None:
        with self._lock:
            self._events.pop(key, None)
            self._windows.pop(key, None)
