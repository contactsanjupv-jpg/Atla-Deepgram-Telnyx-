"""
Worker watchdog. One daemon thread that does two things:

  * beats the liveness key, but ONLY while the main loop is inside its time budget;
  * when the main loop is stuck (a bounded operation overran its budget, or the loop went silent between
    operations), stops beating, records why, and exits the process so the supervisor restarts it.

So the liveness thread can never make a stuck main loop look healthy: the beat stops at the budget, readiness turns
to `stalled`, and shortly afterwards the process is gone. Python cannot kill a thread, so restarting the process is
the only reliable recovery from a hang (a call blocked in a driver, a deadlock).

The clock, the beat and the exit are injectable so the behaviour is tested without waiting or killing the test run.
"""
import logging
import os
import threading
import time

logger = logging.getLogger("atla.watchdog")
EXIT_CODE = 70  # EX_SOFTWARE: the supervisor restarts on any non-zero exit


class Watchdog:
    def __init__(self, *, beat, idle_grace: float, interval: float = 10.0, exit_fn=None, on_stall=None, clock=time.monotonic):
        self._beat = beat
        self._idle_grace = idle_grace  # longest the loop may stay silent BETWEEN operations (its sleep + margin)
        self._interval = interval
        self._exit = exit_fn or self._hard_exit
        self._on_stall = on_stall
        self._clock = clock
        self._lock = threading.Lock()
        self._phase: str | None = None
        self._budget = 0.0
        self._since = clock()
        self.stalled = False

    @staticmethod
    def _hard_exit() -> None:
        logging.shutdown()  # flush the log line that explains why
        os._exit(EXIT_CODE)

    # --- called by the main loop ---
    def enter(self, name: str, budget_seconds: float) -> None:
        with self._lock:
            self._phase, self._budget, self._since = name, float(budget_seconds), self._clock()

    def leave(self) -> None:
        with self._lock:
            self._phase, self._since = None, self._clock()

    # --- called by the watchdog thread ---
    def check(self) -> bool:
        """True while the main loop is healthy (and beats); False once it is stalled (never beats again)."""
        with self._lock:
            phase, budget, since = self._phase, self._budget, self._since
        elapsed = self._clock() - since
        limit = budget if phase is not None else self._idle_grace
        if elapsed > limit:
            if not self.stalled:
                self.stalled = True
                what = phase or "idle"
                logger.critical("Worker main loop stalled in '%s' for %.0fs (budget %.0fs); stopping the liveness beat and exiting so the supervisor restarts it", what, elapsed, limit)
                try:
                    if self._on_stall:
                        self._on_stall(what)
                except Exception:  # noqa: BLE001 - recording why must never prevent the restart
                    pass
                self._exit()
            return False
        self._beat()
        return True

    def start(self) -> threading.Event:
        stop = threading.Event()

        def loop():
            while not stop.wait(self._interval):
                if not self.check():
                    return

        threading.Thread(target=loop, name="atla-worker-watchdog", daemon=True).start()
        return stop
