"""Fetcher: calls one Garmin endpoint, stores the raw response, logs the outcome.

Every call is isolated: a failing endpoint is logged and the sync continues.
Only authentication failures and persistent rate limiting abort a run.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from .store import Store

log = logging.getLogger(__name__)

RATE_LIMIT_BACKOFF_S = (60, 180)


class SyncAborted(RuntimeError):
    """Raised when continuing the sync is pointless (auth failure, rate limit)."""


@dataclass
class FetchStats:
    new: int = 0
    unchanged: int = 0
    empty: int = 0
    errors: int = 0
    failed: list[str] = field(default_factory=list)


def _is_empty(payload: Any) -> bool:
    return payload is None or payload == [] or payload == {}


class Fetcher:
    def __init__(self, store: Store, run_id: str, delay_s: float = 0.5) -> None:
        self.store = store
        self.run_id = run_id
        self.delay_s = delay_s
        self.stats = FetchStats()

    def fetch(
        self,
        endpoint: str,
        source_key: str,
        fn: Callable[..., Any],
        *args: Any,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> Any | None:
        """Call ``fn(*args)``; store and return its payload, or None on failure."""
        payload = self._call(endpoint, source_key, fn, *args)
        if payload is _FAILED:
            return None
        is_new = self.store.put_raw(endpoint, source_key, payload, date_from, date_to)
        if _is_empty(payload):
            status = "empty"
            self.stats.empty += 1
        elif is_new:
            status = "ok"
            self.stats.new += 1
        else:
            status = "unchanged"
            self.stats.unchanged += 1
        self.store.log(self.run_id, endpoint, source_key, status)
        log.debug("%s %s: %s", endpoint, source_key, status)
        return payload

    def _call(self, endpoint: str, source_key: str, fn: Callable[..., Any], *args: Any) -> Any:
        backoffs = list(RATE_LIMIT_BACKOFF_S)
        while True:
            if self.delay_s:
                time.sleep(self.delay_s)
            try:
                return fn(*args)
            except GarminConnectAuthenticationError as e:
                self._error(endpoint, source_key, e, status="aborted")
                raise SyncAborted(f"Authentication failed: {e}. Run `garmin-data login`.") from e
            except GarminConnectTooManyRequestsError as e:
                if not backoffs:
                    self._error(endpoint, source_key, e, status="aborted")
                    raise SyncAborted("Garmin rate limit hit repeatedly; try again later.") from e
                wait = backoffs.pop(0)
                log.warning("Rate limited on %s %s; sleeping %ss", endpoint, source_key, wait)
                time.sleep(wait)
            except GarminConnectNotFoundError:
                # Resource does not exist (e.g. no weather for indoor activity).
                return None
            except Exception as e:  # noqa: BLE001 - one endpoint must never stop the sync
                self._error(endpoint, source_key, e)
                return _FAILED

    def _error(
        self, endpoint: str, source_key: str, exc: BaseException, status: str = "error"
    ) -> None:
        """Log a failed request. ``aborted`` (auth / rate limit) is a session
        problem and does not count as a failed attempt for this item."""
        msg = f"{type(exc).__name__}: {exc}"
        if status == "error":
            self.stats.errors += 1
            self.stats.failed.append(f"{endpoint} {source_key}")
        self.store.log(self.run_id, endpoint, source_key, status, msg[:2000])
        log.warning("%s %s failed: %s", endpoint, source_key, msg)


_FAILED = object()
