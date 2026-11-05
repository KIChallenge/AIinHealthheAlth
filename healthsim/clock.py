"""Injectable time. Deadlines are persisted as wall-clock instants and tracked
monotonically within a process, so a resume never restarts a timer."""

import asyncio
import time


class RealClock:
    def time(self):
        return time.time()

    def monotonic(self):
        return time.monotonic()

    async def sleep(self, seconds):
        await asyncio.sleep(max(0, seconds))

    async def wait_for(self, awaitable, timeout):
        """Cancel the operation when the total budget is used up."""
        return await asyncio.wait_for(awaitable, timeout)


class FakeClock:
    """Virtual time for offline tests: sleeping advances time immediately."""

    def __init__(self, start=1_800_000_000.0):
        self.now = start
        self.base = 1000.0 - start

    def time(self):
        return self.now

    def monotonic(self):
        return self.now + self.base

    def advance(self, seconds):
        self.now += seconds

    async def sleep(self, seconds):
        self.now += max(0, seconds)
        await asyncio.sleep(0)

    async def wait_for(self, awaitable, timeout):
        start = self.now
        result = await awaitable
        if self.now - start > timeout:
            raise TimeoutError
        return result


class Deadlines:
    """remaining() for a persisted wall-clock deadline, anchored monotonically."""

    def __init__(self, clock):
        self.clock = clock
        self._anchors = {}

    def remaining(self, deadline_at):
        if deadline_at is None:
            return None
        mono = self._anchors.get(deadline_at)
        if mono is None:
            mono = self.clock.monotonic() + (deadline_at - self.clock.time())
            self._anchors[deadline_at] = mono
        return mono - self.clock.monotonic()

    def expired(self, deadline_at):
        remaining = self.remaining(deadline_at)
        return remaining is not None and remaining <= 0
