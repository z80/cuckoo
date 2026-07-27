# boot.py -- run on boot to configure USB and filesystem
# Put app code in main.py

import pyb
from debug_mode import is_debug_mode

"""
led1 = pyb.LED(1)
led2 = pyb.LED(2)

for _ in range(10):  # Blink for ~5 seconds
    led1.toggle()
    led2.toggle()
    pyb.delay(500)
"""

dbg = is_debug_mode():

if dbg:
    pyb.main('main_debug.py')
    # act as a serial and a storage device
    pyb.usb_mode('VCP+MSC')

else:
    pyb.main('main.py')
    pyb.usb_mode('VCP')




