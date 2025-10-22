# boot.py -- run on boot to configure USB and filesystem
# Put app code in main.py

import machine
import pyb

"""
led1 = pyb.LED(1)
led2 = pyb.LED(2)

for _ in range(10):  # Blink for ~5 seconds
    led1.toggle()
    led2.toggle()
    pyb.delay(500)
"""

#pyb.main('main.py') # main script to run after this one
#pyb.usb_mode('VCP+MSC') # act as a serial and a storage device
#pyb.usb_mode('VCP+HID') # act as a serial device and a mouse
#import network
#network.country('US') # ISO 3166-1 Alpha-2 code, eg US, GB, DE, AU or XX for worldwide
#network.hostname('...') # DHCP/mDNS hostname

pyb.usb_mode('VCP') # act as a serial only


