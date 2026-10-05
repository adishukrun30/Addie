"""Session handling: one unlocked session at a time, auto-lock on inactivity."""
import hmac
import secrets
import threading
import time


class SessionManager:
    def __init__(self, db, auto_lock_seconds: int):
        self.db = db
        self.auto_lock_seconds = auto_lock_seconds
        self._token: str | None = None
        self._last_seen = 0.0
        self._failed_attempts = 0
        self._lock = threading.Lock()

    def start(self) -> str:
        with self._lock:
            self._token = secrets.token_urlsafe(32)
            self._last_seen = time.monotonic()
            self._failed_attempts = 0
            return self._token

    def check(self, token: str | None) -> bool:
        with self._lock:
            if self._token is None or not token:
                return False
            if time.monotonic() - self._last_seen > self.auto_lock_seconds:
                self._lock_now()
                return False
            if not hmac.compare_digest(self._token, token):
                return False
            self._last_seen = time.monotonic()
            return True

    def lock(self) -> None:
        with self._lock:
            self._lock_now()

    def expire_if_idle(self) -> None:
        with self._lock:
            if self._token and time.monotonic() - self._last_seen > self.auto_lock_seconds:
                self._lock_now()

    def _lock_now(self) -> None:
        self._token = None
        self.db.close()  # drops the key from memory along with the connection

    def failed_attempt_delay(self) -> float:
        """Grows with consecutive wrong passwords to slow down guessing."""
        with self._lock:
            self._failed_attempts += 1
            return min(2 ** (self._failed_attempts - 1), 30)
