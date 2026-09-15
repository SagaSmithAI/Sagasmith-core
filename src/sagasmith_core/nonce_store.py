"""Durable operational nonce claims, separate from story snapshots."""

import hashlib

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from .models import AuthNonceRecord


class DatabaseNonceStore:
    def __init__(self, database):
        self.database = database

    def claim(self, key: str, expires: float, now: float, maximum: int) -> None:
        self.database.require_independent_work()
        try:
            with self.database.transaction(immediate=True) as session:
                session.execute(delete(AuthNonceRecord).where(AuthNonceRecord.expires_at < now))
                if session.scalar(select(func.count()).select_from(AuthNonceRecord)) >= maximum:
                    raise RuntimeError("auth context replay guard is at capacity")
                session.add(
                    AuthNonceRecord(
                        key=hashlib.sha256(key.encode()).hexdigest(), expires_at=expires
                    )
                )
                session.flush()
        except IntegrityError as error:
            raise ValueError("auth context nonce was already used") from error
