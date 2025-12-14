import time
from machine import I2C, Pin
import pyb
from pyb import Pin, DAC, ADC

def send_pulse( axis, time_sec, value, value_zero=2045 ):
    axis     = str(axis)
    time_sec = float(time_sec)
    value    = int(value)

    led1 = pyb.Pin('A15', Pin.OUT)
    led2 = pyb.Pin('C10', Pin.OUT)

    dac_x = DAC(Pin('A5'), bits=12, buffering=True)
    dac_y = DAC(Pin('A4'), bits=12, buffering=True)

    adc_en = ADC( Pin('A2') )
    pin_en = Pin('A3', Pin.OUT)

    led1.off()
    led2.off()
    pin_en.off()

    if axis == 'x':
        dac_x.write( value )
        dac_y.write( value_zero )

    elif axis == 'y':
        dac_x.write( value_zero )
        dac_y.write( value )


    pin_en.on()
    time.sleep( time_sec )
    pin_en.off()

    dac_x.write( value_zero )
    dac_y.write( value_zero )

    print( "Done. axis:", axis, "; time: ", str(time_sec), "; value:", str(value) )






