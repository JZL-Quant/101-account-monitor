import asyncio
import threading
import time
import weakref


class BinanceMinuteTickerCache:
    """Fetch Binance's full ticker list at most once per natural minute."""

    def __init__(self, *, clock=time.time):
        self._clock = clock
        self._minute = None
        self._value = None
        self._state_lock = threading.Lock()
        self._loop_locks = weakref.WeakKeyDictionary()

    def _request_lock(self):
        """An asyncio.Lock cannot be reused by a different event loop."""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            lock = self._loop_locks.get(loop)
            if lock is None:
                lock = asyncio.Lock()
                self._loop_locks[loop] = lock
            return lock

    def _cached_value(self, minute):
        with self._state_lock:
            if self._minute == minute and self._value is not None:
                return True, self._value
        return False, None

    async def get(self, loader):
        minute = int(self._clock() // 60)
        found, value = self._cached_value(minute)
        if found:
            return value

        async with self._request_lock():
            minute = int(self._clock() // 60)
            found, value = self._cached_value(minute)
            if found:
                return value

            # Only successful results are cached. A failed request is retried by
            # the next caller instead of poisoning the entire minute.
            value = await loader()
            with self._state_lock:
                self._value = value
                self._minute = minute
            return value
