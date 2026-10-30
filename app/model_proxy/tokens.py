"""Per-job token issuance, hashing, TTL, and revocation."""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from app.model_proxy.settings import ProxySettings

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "opr_job_"
_JOB_ID_RE_MIN = 1
_JOB_ID_RE_MAX = 128


@dataclass
class JobTokenRecord:
    token_hash: str
    job_id: str
    owner_id: str
    expires_at: float
    req_count: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    active_streams: int = 0


@dataclass
class IssuedToken:
    job_id: str
    token: str
    expires_in: int
    owner_id: str


class TokenStore:
    """In-memory hashed job tokens (process-local; lost on restart -> 401)."""

    def __init__(self, settings: ProxySettings) -> None:
        self._settings = settings
        self._lock = threading.RLock()
        self._by_hash: dict[str, JobTokenRecord] = {}
        self._by_job: dict[str, str] = {}  # job_id -> token_hash

    def _purge_expired(self, now: float) -> None:
        expired = [h for h, rec in self._by_hash.items() if rec.expires_at <= now]
        for h in expired:
            rec = self._by_hash.pop(h, None)
            if rec is not None:
                self._by_job.pop(rec.job_id, None)

    @staticmethod
    def hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def issue(
        self,
        *,
        job_id: str,
        timeout_seconds: int,
        owner_id: str = "",
    ) -> IssuedToken:
        jid = (job_id or "").strip()
        if not jid or not (_JOB_ID_RE_MIN <= len(jid) <= _JOB_ID_RE_MAX):
            raise ValueError("invalid job_id")
        timeout = max(1, int(timeout_seconds))
        if timeout > self._settings.max_review_timeout_seconds:
            raise ValueError(
                f"timeout_seconds ({timeout}) exceeds OPR_MAX_REVIEW_TIMEOUT_SECONDS "
                f"({self._settings.max_review_timeout_seconds})"
            )
        ttl = min(
            timeout + int(self._settings.token_grace_seconds),
            self._settings.max_token_ttl_seconds,
        )
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_hash = self.hash_token(token)
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            old_hash = self._by_job.get(jid)
            if old_hash:
                self._by_hash.pop(old_hash, None)
            rec = JobTokenRecord(
                token_hash=token_hash,
                job_id=jid,
                owner_id=(owner_id or "").strip(),
                expires_at=now + float(ttl),
            )
            self._by_hash[token_hash] = rec
            self._by_job[jid] = token_hash
        logger.info(
            "model_proxy token issued job_id=%s owner_id=%s ttl=%s",
            jid,
            (owner_id or "").strip() or "-",
            ttl,
        )
        return IssuedToken(job_id=jid, token=token, expires_in=ttl, owner_id=rec.owner_id)

    def revoke(self, job_id: str) -> bool:
        jid = (job_id or "").strip()
        with self._lock:
            h = self._by_job.pop(jid, None)
            if not h:
                return False
            self._by_hash.pop(h, None)
        logger.info("model_proxy token revoked job_id=%s", jid)
        return True

    def resolve(self, token: str) -> JobTokenRecord | None:
        raw = (token or "").strip()
        if not raw.startswith(TOKEN_PREFIX):
            return None
        h = self.hash_token(raw)
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            rec = self._by_hash.get(h)
            if rec is None:
                return None
            if rec.expires_at <= now:
                self._by_hash.pop(h, None)
                self._by_job.pop(rec.job_id, None)
                return None
            return rec

    def still_valid(self, rec: JobTokenRecord) -> bool:
        """True if the job token is still present and unexpired."""
        now = time.time()
        with self._lock:
            live = self._by_hash.get(rec.token_hash)
            return live is not None and live.expires_at > now

    def begin_request(self, rec: JobTokenRecord, *, body_len: int) -> str | None:
        """Reserve a request slot. Returns error code or None on success."""
        with self._lock:
            live = self._by_hash.get(rec.token_hash)
            if live is None or live.expires_at <= time.time():
                return "expired"
            if live.req_count >= self._settings.max_requests_per_job:
                return "request_limit"
            if live.bytes_in + live.bytes_out + body_len > self._settings.max_bytes_per_job:
                return "byte_limit"
            if live.active_streams >= self._settings.max_concurrent_per_job:
                return "concurrency_limit"
            live.req_count += 1
            live.bytes_in += max(0, int(body_len))
            live.active_streams += 1
            return None

    def consume_bytes_out(self, rec: JobTokenRecord, n: int) -> int:
        """Atomically account for up to ``n`` outbound bytes.

        Returns how many bytes were accepted (0..n). Concurrent callers for the
        same job cannot jointly exceed ``max_bytes_per_job``.
        """
        want = max(0, int(n))
        if want == 0:
            return 0
        with self._lock:
            live = self._by_hash.get(rec.token_hash)
            if live is None:
                return 0
            used = live.bytes_in + live.bytes_out
            remaining = max(0, int(self._settings.max_bytes_per_job) - used)
            take = min(want, remaining)
            live.bytes_out += take
            return take

    def end_request(self, rec: JobTokenRecord) -> None:
        """Release one concurrent stream slot (outbound bytes already recorded)."""
        with self._lock:
            live = self._by_hash.get(rec.token_hash)
            if live is None:
                return
            live.active_streams = max(0, live.active_streams - 1)

    def bytes_used(self, rec: JobTokenRecord) -> int:
        with self._lock:
            live = self._by_hash.get(rec.token_hash)
            if live is None:
                return 0
            return int(live.bytes_in + live.bytes_out)

    def active_job_count(self) -> int:
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            return len(self._by_job)
