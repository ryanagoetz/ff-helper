"""Background poller that keeps the board fed from Yahoo.

Runs on its own thread so a slow or failing Yahoo request never blocks the UI. Every
mutation of shared state goes through a lock, because the web handlers read the same
``DraftState`` the poller writes.

Failures here are expected, not exceptional: this runs for two hours over a home network
against an API with no uptime promise. So a failed poll records an error and keeps going
rather than killing the thread, and the staleness of the last good sync is exposed to the
UI so you can see when to start entering picks by hand.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from ff_helper.draft.state import DraftState
from ff_helper.yahoo.client import YahooClient

logger = logging.getLogger(__name__)

# After this many consecutive failures the UI should treat the feed as down.
UNHEALTHY_AFTER = 3

# Poll far less often before the draft starts; nothing is changing yet.
PREDRAFT_INTERVAL = 15.0

# Ceiling on the backoff applied after repeated failures.
MAX_BACKOFF = 30.0


class DraftSync:
    def __init__(
        self,
        client: YahooClient,
        state: DraftState,
        *,
        interval: float = 2.0,
        lock: threading.Lock | None = None,
        on_picks: Callable[[list], None] | None = None,
    ) -> None:
        self.client = client
        self.state = state
        self.interval = interval
        self.lock = lock or threading.Lock()
        self.on_picks = on_picks

        self.consecutive_failures = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="draft-sync", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def healthy(self) -> bool:
        return self.consecutive_failures < UNHEALTHY_AFTER

    # -- polling -----------------------------------------------------------------------

    def poll_once(self) -> list:
        """One fetch-and-merge. Returns picks that were new to the board."""
        league_key = self.state.league.league_key
        picks = self.client.draft_results(league_key)
        with self.lock:
            new_picks = self.state.apply_sync(picks, timestamp=time.time())
        self.consecutive_failures = 0
        return new_picks

    def refresh_status(self) -> str:
        """Re-read draft_status so we know when predraft becomes drafting."""
        status = self.client.draft_status(self.state.league.league_key)
        with self.lock:
            self.state.draft_status = status
        return status

    def _current_interval(self) -> float:
        if self.consecutive_failures:
            # Back off on repeated failure so a dead endpoint is not hammered, but keep
            # the ceiling low enough that recovery is noticed quickly.
            return min(MAX_BACKOFF, self.interval * (2**self.consecutive_failures))
        if self.state.draft_status == "predraft":
            return PREDRAFT_INTERVAL
        return self.interval

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if self.state.draft_status == "predraft":
                    self.refresh_status()

                new_picks = self.poll_once()
                if new_picks and self.on_picks is not None:
                    self.on_picks(new_picks)

                if self.state.is_complete:
                    logger.info("Draft complete; stopping sync.")
                    return

            except Exception as exc:  # noqa: BLE001 - the loop must survive anything
                self.consecutive_failures += 1
                with self.lock:
                    self.state.last_sync_error = str(exc)
                logger.warning(
                    "Draft sync failed (%s consecutive): %s", self.consecutive_failures, exc
                )

            self._stop.wait(self._current_interval())
