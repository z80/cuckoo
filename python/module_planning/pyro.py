import uasyncio as asyncio
from pyb import Pin


POLL_MS = 50


class Pyro:
    """Power and edge-latched state for the pyro input."""

    def __init__(self):
        self._power = Pin("C13", Pin.OUT)
        self._sense = Pin("B8", Pin.IN)
        self._enabled = False
        self._pending = False
        self._last = self._sense.value()
        self._power.off()

    def set_enable(self, enabled):
        enabled = bool(enabled)
        self._enabled = enabled
        self._pending = False
        self._last = self._sense.value()
        self._power.value(1 if enabled else 0)
        return {"ok": True, "enabled": enabled}

    def get_state(self):
        triggered = self._pending
        self._pending = False
        return {
            "ok": True,
            "enabled": self._enabled,
            "triggered": triggered,
        }

    async def process(self):
        try:
            while True:
                value = self._sense.value()
                if self._enabled and value and not self._last:
                    self._pending = True
                self._last = value
                await asyncio.sleep_ms(POLL_MS)
        finally:
            self._enabled = False
            self._pending = False
            self._power.off()

    def shutdown(self):
        self._enabled = False
        self._pending = False
        self._power.off()
