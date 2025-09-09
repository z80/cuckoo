import uasyncio as asyncio
import pyb

# Setup PWM channels
tim = pyb.Timer(2, freq=1000)
ch1 = tim.channel(2, pyb.Timer.PWM, pin=pyb.Pin.cpu.A1)
ch2 = tim.channel(3, pyb.Timer.PWM, pin=pyb.Pin.cpu.A2)

fader_task = None  # Global reference to current fader coroutine

async def fade_leds(period_ms=1000, steps=100):
    delay = (period_ms / 1000) / (2 * steps)
    while True:
        for i in range(steps):
            duty = int((i / steps) * 20)
            ch1.pulse_width_percent(duty)
            ch2.pulse_width_percent(20 - duty)
            await asyncio.sleep(delay)
        for i in range(steps, -1, -1):
            duty = int((i / steps) * 20)
            ch1.pulse_width_percent(duty)
            ch2.pulse_width_percent(20 - duty)
            await asyncio.sleep(delay)


async def input_handler():
    global fader_task
    while True:
        print("Enter fader period (ms): ", end="")
        # This blocks, but we yield afterward to stay cooperative
        user_input = input()
        try:
            new_period = int(user_input.strip())
            print(f"Restarting fader with period: {new_period} ms")

            if fader_task:
                fader_task.cancel()
                await asyncio.sleep(0)  # Allow cancellation to propagate

            fader_task = asyncio.create_task(fade_leds(period_ms=new_period))

        except ValueError:
            print("Invalid input. Please enter an integer.")

        await asyncio.sleep(0)  # Yield to event loop


async def main():
    global fader_task
    fader_task = asyncio.create_task(fade_leds(period_ms=1000))
    await input_handler()  # Runs until manually stopped

asyncio.run(main())


