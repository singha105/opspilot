"""Approval tokens: the only way a write action can be authorised.

A token is minted after a human approves one specific action. It is an HMAC-SHA256
over ``action_hash|expiry|approver|nonce`` with ``OPSPILOT_APPROVAL_SECRET``. The
actions server verifies the signature, the action hash, the expiry (at most 10
minutes ahead) and that the nonce has never been used. This module is the only
place that mints tokens.
"""

import base64
import hashlib
import hmac
import json
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from opspilot.config import Settings, get_settings

TOKEN_VERSION = "v1"  # noqa: S105 - a format version, not a credential
MAX_TTL_S = 600
MIN_SECRET_CHARS = 32


class ApprovalError(Exception):
    """A token was missing, malformed, forged, expired, reused or for another action."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ApprovalClaims:
    action_hash: str
    expires_at: int
    approver: str
    nonce: str


def canonical_json(action: BaseModel | Mapping[str, Any]) -> str:
    """Canonical JSON of an action: sorted keys, no whitespace, nulls dropped."""
    data = (
        action.model_dump(mode="json", exclude_none=True)
        if isinstance(action, BaseModel)
        else dict(action)
    )
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def action_hash(action: BaseModel | Mapping[str, Any]) -> str:
    """sha256 of the canonical action JSON."""
    return hashlib.sha256(canonical_json(action).encode()).hexdigest()


def _secret(settings: Settings | None) -> bytes:
    settings = settings or get_settings()
    secret = settings.approval_secret.get_secret_value() if settings.approval_secret else ""
    if len(secret) < MIN_SECRET_CHARS:
        raise ApprovalError(
            "OPSPILOT_APPROVAL_SECRET is not configured (needs at least 32 characters)"
        )
    return secret.encode()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(secret: bytes, claims: ApprovalClaims) -> bytes:
    message = f"{claims.action_hash}|{claims.expires_at}|{claims.approver}|{claims.nonce}"
    return hmac.new(secret, message.encode(), hashlib.sha256).digest()


def mint_approval_token(
    action_hash: str,
    approver: str,
    ttl_s: int = MAX_TTL_S,
    *,
    settings: Settings | None = None,
    now: float | None = None,
) -> str:
    """Mint a single-use token for exactly one action, valid for at most 10 minutes."""
    if not 0 < ttl_s <= MAX_TTL_S:
        raise ApprovalError(f"ttl_s must be between 1 and {MAX_TTL_S} seconds")
    if not approver.strip():
        raise ApprovalError("an approver is required")
    issued = int(now if now is not None else time.time())
    claims = ApprovalClaims(action_hash, issued + ttl_s, approver.strip(), uuid.uuid4().hex)
    payload = {
        "h": claims.action_hash,
        "exp": claims.expires_at,
        "by": claims.approver,
        "n": claims.nonce,
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{TOKEN_VERSION}.{body}.{_b64(_signature(_secret(settings), claims))}"


def peek_claims(token: str) -> dict[str, Any]:
    """Unverified claims for audit logging only (approver, nonce prefix). Never trust them."""
    try:
        _, body, _ = token.split(".")
        data = json.loads(_unb64(body))
        return {"approver": str(data.get("by", ""))[:64], "nonce": str(data.get("n", ""))[:8]}
    except (ValueError, AttributeError):
        return {}


class NonceStore:
    """Persistent set of used nonces (SQLite), so a token works exactly once."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS used (nonce TEXT PRIMARY KEY, action_hash TEXT, ts REAL)"
        )
        self._db.commit()

    def consume(self, nonce: str, action_hash: str) -> bool:
        """Mark a nonce as used; False if it was already used."""
        try:
            with self._db:
                self._db.execute(
                    "INSERT INTO used VALUES (?, ?, ?)", (nonce, action_hash, time.time())
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def is_used(self, nonce: str) -> bool:
        row = self._db.execute("SELECT 1 FROM used WHERE nonce = ?", (nonce,)).fetchone()
        return row is not None


def verify_approval_token(
    token: str | None,
    expected_hash: str,
    nonces: NonceStore,
    *,
    consume: bool = True,
    settings: Settings | None = None,
    now: float | None = None,
) -> ApprovalClaims:
    """Check format, signature, action hash, expiry window and single use.

    With ``consume=True`` (the default) a valid token's nonce is burned, so a second
    call with the same token fails.
    """
    if not token:
        raise ApprovalError("missing approval token")
    try:
        version, body, sig = token.split(".")
        data = json.loads(_unb64(body))
        claims = ApprovalClaims(str(data["h"]), int(data["exp"]), str(data["by"]), str(data["n"]))
        signature = _unb64(sig)
    except (ValueError, KeyError, TypeError) as exc:
        raise ApprovalError("malformed approval token") from exc
    if version != TOKEN_VERSION:
        raise ApprovalError("unsupported token version")
    if not hmac.compare_digest(signature, _signature(_secret(settings), claims)):
        raise ApprovalError("invalid token signature")
    if not hmac.compare_digest(claims.action_hash, expected_hash):
        raise ApprovalError("token was approved for a different action")
    current = now if now is not None else time.time()
    if claims.expires_at <= current:
        raise ApprovalError("approval token expired")
    if claims.expires_at - current > MAX_TTL_S:
        raise ApprovalError("approval token lifetime exceeds 10 minutes")
    if consume:
        if not nonces.consume(claims.nonce, claims.action_hash):
            raise ApprovalError("approval token was already used")
    elif nonces.is_used(claims.nonce):
        raise ApprovalError("approval token was already used")
    return claims
