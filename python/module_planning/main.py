import pyb

# Normal boot dedicates VCP 0 to the binary gateway protocol.
usb = pyb.USB_VCP()
usb.setinterrupt(-1)

import uasyncio

from transport_node import TransportNode
from usb_node_bridge import USBNodeBridge

#import gc
#gc.threshold(256000)


# Set False to make the PC-backed board join an existing master.
IS_MASTER = True
NETWORK_ID = "D26AB53C"


async def async_main():
    node = TransportNode(
        is_master=IS_MASTER,
        network_id=NETWORK_ID,
        debug=False,
    )
    bridge = USBNodeBridge(node, usb)

    uasyncio.create_task(node.process())
    await bridge.process()


def main():
    try:
        uasyncio.run(async_main())
    except Exception:
        # Never fall through into REPL and mix a traceback with binary frames.
        pyb.delay(100)
        pyb.hard_reset()


main()
