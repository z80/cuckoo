import time
import pyb
from machine import Pin

# Use board-defined aliases directly
led1 = pyb.Pin('A15', Pin.OUT)
led2 = pyb.Pin('C10', Pin.OUT)

# Blink loop
def blink(delay=0.5):
    while True:
        led1.on()
        led2.off()
        time.sleep(delay)
        led1.off()
        led2.on()
        time.sleep(delay)



blink()

