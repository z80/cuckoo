import uasyncio as asyncio
#from machine import Pin, DAC, PWM
import pyb
from pyb import Pin, DAC

from array import array
import math

# === Pin Setup ===
pyr_input = Pin('A6', Pin.IN)        # PYR sensor input
pyr_power = Pin('A7', Pin.OUT)       # PYR sensor power

#servo_power_pwm = PWM(Pin('C4'))     # Servo power control (P-MOSFET)
#servo_control_pwm = PWM(Pin('C5'))   # Servo angle control

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
    servo_power_pwm.freq(1000)
    for i in range(POWER_FADE_STEPS + 1):
        duty = int((i / POWER_FADE_STEPS) * 100)
        servo_power_pwm.duty(duty)
        await asyncio.sleep_ms(SERVO_FADE_TIME // POWER_FADE_STEPS)


async def move_servo(from_deg, to_deg, duration_ms):
    servo_control_pwm.freq(SERVO_FREQ)
    steps = 50
    for i in range(steps + 1):
        frac = i / steps
        angle = from_deg + frac * (to_deg - from_deg)
        pulse_width = int(1000 + (angle / 180) * 1000)  # 1ms to 2ms pulse
        servo_control_pwm.duty_ns(pulse_width * 1000)
        await asyncio.sleep_ms(duration_ms // steps)


async def play_audio_waveform(duration_ms):
    audio_power.on()
    buf = array('H', [2048 + int(2047 * math.sin(2 * math.pi * i / 128)) for i in range(128)])
    dac.write_timed(buf, 400 * len(buf), mode=DAC.CIRCULAR)
    await asyncio.sleep_ms(duration_ms)
    dac.write(0)
    audio_power.off()


async def cuckoo_sequence():
    led2.on()
    #await fade_servo_power_in()
    await asyncio.sleep_ms(100)

    led2.off()
    #await move_servo(0, 60, SERVO_MOVE_TIME)
    await asyncio.sleep_ms(100)

    led2.on()
    await play_audio_waveform(AUDIO_PLAY_TIME)
    await asyncio.sleep_ms(1000)

    led2.off()
    #await move_servo(60, 0, SERVO_MOVE_TIME)
    await asyncio.sleep_ms(100)

    led2.on()
    #servo_power_pwm.duty(0)
    await asyncio.sleep_ms(100)

    led2.off()
    await asyncio.sleep_ms(1000)


async def main():
    audio_power.off()
    #servo_power_pwm.off()
    #pyr_power.off()
    pyr_power.on()
    print("Sensor powered. Waiting for trigger...")
    while True:
        if pyr_input.value():
        #if True:
            led1.on()
            print("Motion detected! Starting sequence.")
            await cuckoo_sequence()
            led1.off()

            print("Sequence complete. Waiting for next trigger.")
            await asyncio.sleep_ms(500)  # debounce

        await asyncio.sleep_ms(100)


asyncio.run(main())

