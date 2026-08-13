from array import array
import asyncio

from pyb import Pin, Timer
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
            if led1.value():
                led1.off()
                led2.on()
            else:
                led1.on()
                led2.off()
            return block
        if not adc.running():
            raise RuntimeError("ADC stopped with error %d" % adc.error())
        await asyncio.sleep_ms(0)


async def loopback():
    pin_power = Pin( "B4", Pin.OUT )
    # Turn Microphone amplifier power on.
    pin_power.on()
    await asyncio.sleep_ms(20)

    # DAC(1) outputs on PA4. Make sure PA4 is not used as radio CS on the
    # particular wiring under test.
    timer = Timer(2, freq=SAMPLE_RATE)

    # These arrays must not be resized while either DMA is active.
    adc_buffer = uint16_buffer(BUFFER_SAMPLES)
    adc = ADCStream(Pin("C0"), adc_buffer, timer)
    adc.set_acquisition_cycles(ACQUISITION_CYCLES)

    adc.start()
    try:
        # At each ADC boundary, the corresponding DAC half may still be
        # active. Commit the previous ADC half to the opposite, inactive DAC
        # half, then stage the newly completed ADC half for the next boundary.
        while True:
            block = await next_adc_half(adc)
            overruns = adc.overruns()
            if overruns != last_overruns:
                raise RuntimeError("ADC overrun: %d" % overruns)

            yield block
    finally:
        if reporter is not None:
            reporter.cancel()
        adc.stop()
        timer.deinit()

        # Turn microphone amplifier power off.
        pin_power.off()
        await asyncio.sleep_ms(20)


