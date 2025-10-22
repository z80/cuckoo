import uasyncio as asyncio
import pyb

# Setup PWM channels
tim = pyb.Timer(2, freq=1000)
ch1 = tim.channel(2, pyb.Timer.PWM, pin=pyb.Pin.cpu.A1)  # PA1 = CH2
ch2 = tim.channel(3, pyb.Timer.PWM, pin=pyb.Pin.cpu.A2)  # PA2 = CH3

async def fade_leds(period=2.0, steps=50):
    delay = period / (2 * steps)

    while True:
        # Fade in
        for i in range(steps):
            duty = int((i / steps) * 20)
            ch1.pulse_width_percent(duty)
            ch2.pulse_width_percent(20 - duty)
            await asyncio.sleep(delay)

        # Fade out
        for i in range(steps, -1, -1):
            duty = int((i / steps) * 20)
            ch1.pulse_width_percent(duty)
            ch2.pulse_width_percent(20 - duty)
            await asyncio.sleep(delay)

async def main():
    asyncio.create_task(fade_leds(period=1.0, steps=100))
    while True:
        await asyncio.sleep(1)  # Keep main alive or add other tasks here

asyncio.run(main())

