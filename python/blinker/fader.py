import time
import pyb

tim = pyb.Timer(2, freq=1000)
ch1 = tim.channel(2, pyb.Timer.PWM, pin=pyb.Pin.cpu.A1)  # PA1 = CH2
ch2 = tim.channel(3, pyb.Timer.PWM, pin=pyb.Pin.cpu.A2)  # PA2 = CH3


# Fade loop
def fade_leds(period=2.0, steps=50):
    while True:
        # Fade in
        for i in range(steps):
            duty = int((i / steps) * 20)  # Duty cycle: 0–100%
            ch1.pulse_width_percent(duty)
            duty = 20 - duty
            ch2.pulse_width_percent(duty)
            time.sleep(period / (2 * steps))
        # Fade out
        for i in range(steps, -1, -1):
            duty = int((i / steps) * 20)
            ch1.pulse_width_percent(duty)
            duty = 20 - duty
            ch2.pulse_width_percent(duty)
            time.sleep(period / (2 * steps))

# Start fading
fade_leds(period=1.0, steps=100)
