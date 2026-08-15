import asyncio
import importlib
import sys
import time as host_time
import types


def import_speaker():
    uasyncio = types.ModuleType("uasyncio")
    uasyncio.Event = asyncio.Event
    uasyncio.create_task = asyncio.create_task
    uasyncio.CancelledError = asyncio.CancelledError

    async def sleep_ms(milliseconds):
        await asyncio.sleep(milliseconds / 1000)

    uasyncio.sleep_ms = sleep_ms

    pyb = types.ModuleType("pyb")

    class Pin:
        OUT = 1

        def __init__(self, *args, **kwargs):
            pass

        def on(self):
            pass

        def off(self):
            pass

    class DAC:
        NORMAL = 0

        def __init__(self, *args, **kwargs):
            pass

    pyb.Pin = Pin
    pyb.DAC = DAC

    time_module = types.ModuleType("time")
    time_module.ticks_ms = lambda: int(host_time.monotonic() * 1000)
    time_module.ticks_us = lambda: int(host_time.monotonic() * 1000000)
    time_module.ticks_diff = lambda new, old: new - old
    time_module.ticks_add = lambda value, delta: value + delta

    sys.modules["uasyncio"] = uasyncio
    sys.modules["pyb"] = pyb
    saved_time = sys.modules.get("time")
    sys.modules["time"] = time_module
    sys.modules.pop("hardware_node.speaker", None)
    try:
        return importlib.import_module("hardware_node.speaker")
    finally:
        if saved_time is None:
            sys.modules.pop("time", None)
        else:
            sys.modules["time"] = saved_time


def test_complete_data_does_not_wait_for_lost_pull_reply():
    class Node:
        def __init__(self):
            self.cancelled = False

        async def send_command_and_wait_reply(self, *args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled = True

    async def run():
        module = import_speaker()
        speaker = module.Speaker()
        speaker._owner = 3
        speaker._pipe_id = 7
        speaker._expected = 4
        node = Node()

        load = asyncio.create_task(speaker._load(node, bytearray(4)))
        await asyncio.sleep(0)
        await speaker.on_pipe_data(7, 3, b"abcd")

        assert await asyncio.wait_for(load, 0.2) == 4
        assert node.cancelled

    asyncio.run(run())
