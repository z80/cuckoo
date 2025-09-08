import time
import pyb
from machine import Pin

# Use board-defined aliases directly
led1 = pyb.LED(1)
led2 = pyb.LED(2)

# Blink loop
def blink(delay=0.5):
    while True:
        led1.toggle()
        led2.toggle()
        time.sleep(delay)

