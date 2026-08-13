from array import array
import asyncio

from pyb import DAC, Pin, Timer
from adc_stream import ADCStream


SAMPLE_RATE = 16000
BUFFER_SAMPLES = 1024
HALF_SAMPLES = BUFFER_SAMPLES // 2
ACQUISITION_CYCLES = 56


def uint16_buffer(samples, initial=0):
    data = array("H", bytearray(samples * 2))
    if initial:
        for index in range(samples):
            data[index] = initial
    return data


async def next_adc_half(adc):
    while True:
        block = adc.poll()
        if block is not None:
            return block
        if not adc.running():
            raise RuntimeError("ADC stopped with error %d" % adc.error())
        await asyncio.sleep_ms(0)


async def report(adc, state):
    while True:
        await asyncio.sleep_ms(1000)
        print("LOOP", state[0], "halves", "overruns", adc.overruns(),
            "error", adc.error())
        state[0] = 0


async def loopback():
    # DAC(1) outputs on PA4. Make sure PA4 is not used as radio CS on the
    # particular wiring under test.
    timer = Timer(2, freq=SAMPLE_RATE)
    dac = DAC(1, bits=12)

    # These arrays must not be resized while either DMA is active.
    adc_buffer = uint16_buffer(BUFFER_SAMPLES)
    dac_buffer = uint16_buffer(BUFFER_SAMPLES, 2048)
    pending = uint16_buffer(HALF_SAMPLES)

    dac_view = memoryview(dac_buffer)
    dac_halves = (
        dac_view[:HALF_SAMPLES],
        dac_view[HALF_SAMPLES:],
    )

    adc = ADCStream(Pin("A0"), adc_buffer, timer)
    adc.set_acquisition_cycles(ACQUISITION_CYCLES)
    state = [0]
    reporter = None

    adc.start()
    try:
        # Prime the output pipeline. Starting the DAC immediately after the
        # ADC's second-half completion aligns both circular DMAs closely.
        first = await next_adc_half(adc)
        dac_halves[0][:] = first
        second = await next_adc_half(adc)
        pending[:] = second

        if adc.overruns():
            raise RuntimeError("ADC overrun while priming")

        dac.write_timed(dac_buffer, timer, mode=DAC.CIRCULAR)
        reporter = asyncio.create_task(report(adc, state))

        # At each ADC boundary, the corresponding DAC half may still be
        # active. Commit the previous ADC half to the opposite, inactive DAC
        # half, then stage the newly completed ADC half for the next boundary.
        destination = 1
        last_overruns = 0
        while True:
            block = await next_adc_half(adc)
            overruns = adc.overruns()
            if overruns != last_overruns:
                raise RuntimeError("ADC overrun: %d" % overruns)

            dac_halves[destination][:] = pending
            pending[:] = block
            destination ^= 1
            state[0] += 1
            last_overruns = overruns
    finally:
        if reporter is not None:
            reporter.cancel()
        adc.stop()
        dac.deinit()
        timer.deinit()


asyncio.run(loopback())
