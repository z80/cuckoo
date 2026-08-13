
import asyncio
from pyb import Pin

pin_power = Pin( "C13", Pin.OUT )
pin_sense = Pin( "B8", Pin.IN )


async def pyro( event: asyncio.Event ):
    pin_power.on()

    while True:
        await asyncio.sleep_ms( 100 )

        val = pin_sense.value()
        # Only set the event. It should be reset on the receiver size 
        # to avoid a race condition.
        if val != 0:
            event.set()


