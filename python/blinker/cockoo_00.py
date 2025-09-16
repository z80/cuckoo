import uasyncio as asyncio
#from machine import Pin, DAC, PWM
import pyb
from pyb import Pin, DAC, Timer

from array import array
import math

# === Pin Setup ===
pyr_input = Pin('A6', Pin.IN)        # PYR sensor input
pyr_power = Pin('A7', Pin.OUT)       # PYR sensor power

timer = Timer(2, freq=1000)

audio_power = Pin('A5', Pin.OUT)     # Audio chip power
dac = DAC(Pin('A4'))                 # DAC output (PA4)

led1 = pyb.Pin('A1', Pin.OUT)
led2 = pyb.Pin('A2', Pin.OUT)

# === Constants ===
SERVO_FADE_TIME = 2000     # ms
SERVO_MOVE_TIME = 1000     # ms
AUDIO_PLAY_TIME = 3000    # ms
SERVO_FREQ = 50            # Hz typical for servo
POWER_FADE_STEPS = 50

# === Helper Functions ===

async def fade_servo_power_in():
    timer.init( freq=1000 )
    pwm = timer.channel( 1, Timer.PWM, pin=Pin('C4') )

    for i in range(POWER_FADE_STEPS + 1):
        duty = int((i / POWER_FADE_STEPS) * 100)
        pwm.pulse_width_percent(duty)
        await asyncio.sleep_ms(SERVO_FADE_TIME // POWER_FADE_STEPS)

    timer.deinit()
    servo_power = pyb.Pin( 'C4', Pin.OUT )
    servo_power.on()


async def move_servo(from_deg, to_deg, duration_ms):
    freq = 50
    period_us = 20000
    timer.init( freq=freq )
    pwm = timer.channel( 2, Timer.PWM, pin=Pin('C5') )   # Servo angle control
    steps = 50
    for i in range(steps + 1):
        frac = i / steps
        angle = from_deg + frac * (to_deg - from_deg)
        pulse_width_us = 1000 + (angle / 180) * 1000  # 1ms to 2ms pulse
        duty_cycle_percent = pulse_width_us * 100 / period_us
        pwm.duty_cycle_percent( duty_cycle_percent )
        await asyncio.sleep_ms(duration_ms // steps)

    timer.deinit()


async def servo_power_off():
    pin = pyb.Pin( 'C5', Pin.OUT )
    pin.off()


async def play_audio_waveform(duration_ms):
    audio_power.on()
    buf = array('H', [2048 + int(2047 * math.sin(2 * math.pi * i / 128)) for i in range(128)])
    dac.write_timed(buf, 400 * len(buf), mode=DAC.CIRCULAR)
    await asyncio.sleep_ms(duration_ms)
    dac.write(0)
    audio_power.off()


async def cuckoo_sequence():
    led2.on()
    await fade_servo_power_in()
    await asyncio.sleep_ms(100)

    led2.off()
    await move_servo(0, 60, SERVO_MOVE_TIME)
    await asyncio.sleep_ms(100)

    led2.on()
    await play_audio_waveform(AUDIO_PLAY_TIME)
    await asyncio.sleep_ms(1000)

    led2.off()
    await move_servo(60, 0, SERVO_MOVE_TIME)
    await asyncio.sleep_ms(100)

    led2.on()
    await servo_power_off()
    await asyncio.sleep_ms(100)

    led2.off()
    await asyncio.sleep_ms(1000)


async def main():
    audio_power.off()
    await servo_power_off()
    pyr_power.on()
    print("Sensor powered. Waiting for trigger...")
    while True:
        #if pyr_input.value():
        if True:
            led1.on()
            print("Motion detected! Starting sequence.")
            await cuckoo_sequence()
            led1.off()

            print("Sequence complete. Waiting for next trigger.")
            await asyncio.sleep_ms(500)  # debounce

        await asyncio.sleep_ms(100)


asyncio.run(main())

