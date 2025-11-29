import asyncio
from array import array
import math
from pyb import Pin, DAC, ADC

led1 = pyb.Pin('A15', Pin.OUT)
led2 = pyb.Pin('C10', Pin.OUT)

dac_x = DAC(Pin('A4'), bits=12)
dac_y = DAC(Pin('A5'), bits=12)

adc_en = ADC( Pin('A2') )
pin_en = Pin('A3', Pin.OUT)

async def dac_waveform(duration_ms=1000):
    freq = 5
    adc_accum = 0
    adc_qty = 0
    for i in range(duration_ms):
        arg = 2 * math.pi * freq * i *0.001
        x = int( 2047 * math.sin( arg ) + 2048 )
        dac_x.write( x )
        dac_y.write( x )

        adc = adc_en.read()
        adc_accum += adc
        adc_qty += 1
        if adc_qty >= 100:
            print( "adc: ", adc_accum )
            adc_accum = 0
            adc_qty = 0
        await asyncio.sleep_ms( 1 )


async def main():
    led1.off()
    led2.off()

    while True:
        pin_en.off()
        led1.off()
        await dac_waveform()
        
        led1.on()
        pin_en.on()
        await dac_waveform()


asyncio.run(main())

