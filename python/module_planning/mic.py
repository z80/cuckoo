from array import array
import uasyncio as asyncio

from adc_stream import ADCStream
from pyb import Pin, Timer

SAMPLE_RATE = 16000
BUFFER_SAMPLES = 1024
ACQUISITION_CYCLES = 56


class Mic:
    def __init__(self):
        self._power = Pin("B4", Pin.OUT)
        self._power.off()
        self._state = "idle"
        self._owner = None
        self._task = None
        self._stop = asyncio.Event()
        self._last_error = None

    async def handle_command(self, node, src_id, operation):
        if operation == "start":
            if self._state != "idle":
                return {"err": "busy"}
            self._owner = src_id
            self._state = "starting"
            self._last_error = None
            self._stop.clear()
            self._task = asyncio.create_task(self._run(node, src_id))
            return {"ok": True, "rate": SAMPLE_RATE, "bits": 12}
        if operation == "stop":
            if self._state == "idle":
                return {"ok": True, "state": "idle"}
            if src_id != self._owner:
                return {"err": "owner"}
            self._stop.set()
            return {"ok": True, "state": "stopping"}
        if operation == "state":
            return {"ok": True, "state": self._state,
                    "error": self._last_error}
        return {"err": "op"}

    async def _run(self, node, destination):
        timer = None
        adc = None
        pipe_id = None
        try:
            await asyncio.sleep_ms(30)
            self._power.on()
            await asyncio.sleep_ms(20)
            timer = Timer(3, freq=SAMPLE_RATE)
            samples = array("H", bytearray(BUFFER_SAMPLES * 2))
            adc = ADCStream(Pin("C0"), samples, timer)
            adc.set_acquisition_cycles(ACQUISITION_CYCLES)
            pipe_id = await node.open_pipe(destination)
            adc.start()
            self._state = "streaming"
            while not self._stop.is_set():
                block = adc.poll()
                if block is None:
                    if not adc.running():
                        raise RuntimeError("ADC stopped %d" % adc.error())
                    await asyncio.sleep_ms(0)
                    continue
                await node.send_pipe(pipe_id, block)
                if adc.overruns():
                    raise RuntimeError("ADC overrun %d" % adc.overruns())
            block = adc.poll()
            if block is not None:
                await node.send_pipe(pipe_id, block)
            await node.send_pipe(pipe_id, b"", close=True)
        except Exception as error:
            self._last_error = str(error)
            print("MIC!", error)
            if pipe_id is not None:
                try:
                    await node.send_pipe(pipe_id, b"", close=True)
                except Exception:
                    pass
        finally:
            if adc is not None:
                adc.stop()
            if timer is not None:
                timer.deinit()
            self._power.off()
            self._owner = None
            self._state = "idle"
            self._task = None

    def shutdown(self):
        self._stop.set()
        self._power.off()
