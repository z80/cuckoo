import time
import pyb

import machine

import release_mode


def blink(delay=0.5):
    # Use board-defined aliases directly
    led1 = pyb.Pin('A15', pyb.Pin.OUT)
    led2 = pyb.Pin('C10', pyb.Pin.OUT)

    while True:
        led1.on()
        led2.off()
        time.sleep(delay)
        led1.off()
        led2.on()
        time.sleep(delay)


def blink_lp(delay=500):
    # Use board-defined aliases directly
    led1 = pyb.Pin('A15', pyb.Pin.OUT)
    led2 = pyb.Pin('C10', pyb.Pin.OUT)

    while True:
        led1.on()
        led2.off()
        machine.lightsleep(delay)
        led1.off()
        led2.on()
        machine.lightsleep(delay)



def main():
    print( "Testing release mode function" )
    ret = not release_mode.is_debug_mode()
    if ret:
        pyb.usb_mode( None )
        machine.freq( 24000000 )
    
    if ret:
        blink_lp()

    else:
        blink()



main()

