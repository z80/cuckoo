# boot.py -- run on boot to configure USB and filesystem
# Put app code in main.py

import pyb
from debug_mode import is_pin_down

dbg = is_pin_down( pin_name='C7', hold_time_ms=500 )

if dbg:
    # Act as a serial and a storage device
    pyb.usb_mode('VCP+MSC')
    # Execute a safe main.
    pyb.main('main_debug.py')

else:
    firmware_upgrade = is_pin_down( pin_name='C8', hold_time_ms=500 )
    if firmware_upgrade:
        pyb.bootloader()

    else:
        # Only serial and disable REPL
        pyb.repl_uart(None)
        pyb.usb_mode('VCP')
        # Execute a normal experimental main.
        pyb.main('main.py')




