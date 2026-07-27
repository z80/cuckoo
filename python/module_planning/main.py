import pyb
import time
from machine import Pin

uart = pyb.USB_VCP()

# Allow USB to enumerate
pyb.delay(300)

# Flush REPL banner
while uart.any():
    uart.recv(64)

# Safe LED init
try:
    led1 = Pin('A15', Pin.OUT)
    led2 = Pin('C10', Pin.OUT)
except Exception as e:
    uart.write("LED init failed: {}\n".format(e))
    # fallback to known-good pins
    led1 = Pin('X1', Pin.OUT)
    led2 = Pin('X2', Pin.OUT)

def blink_once():
    led1.on()
    led2.off()
    time.sleep(0.2)
    led1.off()
    led2.on()
    time.sleep(0.2)

while True:
    blink_once()

    if uart.any():
        try:
            data = uart.read(64)
            #print("RX:", data)
            if data:
                uart.write(data)
        except Exception as e:
            uart.write( "EXCEPTION: " + str(e) )
            #raise

