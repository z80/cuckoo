from array import array
import time

from pyb import Pin, Timer
from adc_stream import ADCStream


SAMPLE_RATE = 16000
BUFFER_SAMPLES = 1024

# Timer 3 is accepted by ADC1 TRGO and is not one of the DAC trigger timers.
timer = Timer(3, freq=SAMPLE_RATE)
# Do not resize this array while adc or a returned memoryview exists.
samples = array("H", bytearray(BUFFER_SAMPLES * 2))
adc = ADCStream(Pin("A0"), samples, timer)
adc.set_acquisition_cycles(56)

halves = 0
last_value = 0
report_at = time.ticks_add(time.ticks_ms(), 1000)

adc.start()
try:
    while True:
        block = adc.poll()
        if block is not None:
            # Only the currently safe half is retained. Consume or copy this
            # view before DMA returns to the same half.
            halves += 1
            last_value = block[-1]

        if not adc.running():
            raise RuntimeError("ADC DMA stopped with error %d" % adc.error())

        now = time.ticks_ms()
        if time.ticks_diff(now, report_at) >= 0:
            print("ADC", halves, "halves", "last", last_value,
                "overruns", adc.overruns(), "error", adc.error())
            halves = 0
            report_at = time.ticks_add(now, 1000)

        time.sleep_ms(1)
finally:
    adc.stop()
    timer.deinit()
