import uasyncio as asyncio
from machine import Pin, DAC, PWM
import pyb
from array import array
import math

# === Pin Setup ===
pyr_input = Pin('PA6', Pin.IN)        # PYR sensor input
pyr_power = Pin('PA7', Pin.OUT)       # PYR sensor power

servo_power_pwm = PWM(Pin('PC4'))     # Servo power control (P-MOSFET)
servo_control_pwm = PWM(Pin('PC5'))   # Servo angle control

audio_power = Pin('PA5', Pin.OUT)     # Audio chip power
dac = DAC(Pin('PA4'))                 # DAC output (PA4)

led1 = pyb.LED(1)
led2 = pyb.LED(2)

# === Constants ===
SERVO_FADE_TIME = 2000     # ms
SERVO_MOVE_TIME = 1000     # ms
AUDIO_PLAY_TIME = 10000    # ms
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
    led2.toggle()
    await fade_servo_power_in()

    led2.toggle()
    await move_servo(0, 60, SERVO_MOVE_TIME)

    led2.toggle()
    await play_audio_waveform(AUDIO_PLAY_TIME)

    led2.toggle()
    await move_servo(60, 0, SERVO_MOVE_TIME)

    led2.toggle()
    servo_power_pwm.duty(0)

    led2.toggle()


async def sensor_monitor():
    audio_power.off()
    servo_power_pwm.off()
    pyr_power.on()
    print("Sensor powered. Waiting for trigger...")
    while True:
        if pyr_input.value():
            led1.on()
            print("Motion detected! Starting sequence.")
            await cuckoo_sequence()
            led1.off()

            print("Sequence complete. Waiting for next trigger.")
            await asyncio.sleep_ms(500)  # debounce

        await asyncio.sleep_ms(100)


async def main():
    asyncio.create_task(sensor_monitor())
    while True:
        await asyncio.sleep(1)  # keep main alive

asyncio.run(main())
