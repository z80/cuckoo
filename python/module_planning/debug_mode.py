import time
import pyb


def is_debug_mode( pin_name='C7', hold_time_ms=500 ):
    pin = pyb.Pin( pin_name, pyb.Pin.IN, pyb.Pin.PULL_UP )
    time.sleep_ms(100)
    start_t = time.ticks_ms()

    while True:
        value = pin.value()
        if value != 0:
            return False
        current_t = time.ticks_ms()
        ticks_diff = time.ticks_diff( current_t, start_t )
        if ticks_diff >= hold_time_ms:
            break

        time.sleep_ms( 10 )

    return True
    



