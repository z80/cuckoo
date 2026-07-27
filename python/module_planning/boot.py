# boot.py -- run on boot to configure USB and filesystem
# Put app code in main.py

import pyb
from debug_mode import is_debug_mode

dbg = is_debug_mode()

if dbg:
    # Act as a serial and a storage device
    pyb.usb_mode('VCP+MSC')
    # Execute a safe main.
    pyb.main('main_debug.py')

else:
    # Only serial and disable REPL
    pyb.repl_uart(None)
    pyb.usb_mode('VCP')
    # Execute a normal experimental main.
    pyb.main('main.py')




