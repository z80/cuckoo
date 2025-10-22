import uasyncio as asyncio

import os
import pyb
from pyb import Pin, DAC, Timer

import struct
from array import array
import time
import math
import random

SAMPLE_RATE = 16000
CHUNK_SIZE = 4000
CHUNK_PERIOD_US = int(1_000_000 * CHUNK_SIZE / SAMPLE_RATE)


# === Pin Setup ===
pyr_input = Pin('A6', Pin.IN)        # PYR sensor input
pyr_power = Pin('A7', Pin.OUT)       # PYR sensor power

# Timer 3 can driver B0 and B1 pins through its channels 3 and 4.
timer = Timer(3, freq=1000)

audio_power = Pin('A5', Pin.OUT)     # Audio chip power
dac = DAC(Pin('A4'), bits=12)                 # DAC output (PA4)

led1 = pyb.Pin('A15', Pin.OUT)
led2 = pyb.Pin('C10', Pin.OUT)

async def servo_power_off():
    pin = pyb.Pin( 'B0', Pin.OUT )
    pin.off()
    pin = pyb.Pin( 'B1', Pin.OUT )
    pin.off()

# === Helper Functions ===
def delay_until(t_ref, period_us):
    now = time.ticks_us()
    wait = time.ticks_diff(t_ref + period_us, now)
    if wait > 0:
        time.sleep_us(wait)
    return t_ref + period_us


def play_audio_file( filename ):
    print( "Entered playing ", filename )
    audio_power.on()
    with open(filename, "rb") as f:
        print( "a", f )

        t_next = time.ticks_us()
        print( "t_next", t_next )
        while True:
            raw = f.read(CHUNK_SIZE * 2)
            if not raw:
                break
            buf = array('H', struct.unpack("<" + "H" * (len(raw) // 2), raw))
            t_next = delay_until(t_next, CHUNK_PERIOD_US)  # ⬅️ Wait until scheduled time
            dac.write_timed(buf, SAMPLE_RATE, mode=DAC.NORMAL)

    audio_power.off()
    dac.write(2048)





def list_files(path="."):
    try:
        return os.listdir(path)
    except OSError:
        return []


def _join_path(dir_path, name):
    if dir_path == "" or dir_path == ".":
        return name
    # ensure single slash separator
    if dir_path.endswith("/"):
        return dir_path + name
    return dir_path + "/" + name


def pick_random_file(full_dir_path=".", ext=None):
    """
    Return a full path to a randomly chosen file in full_dir_path.
    - full_dir_path: directory on the device, e.g. "/wav" or "."
    - ext: optional file extension filter, include the dot, e.g. ".raw" or ".wav"
    Returns None if no matching files found.
    """
    #print( "picking in ", full_dir_path )
    files = list_files(full_dir_path)
    #print( "files: ", files )
    if ext is not None:
        ext = ext.lower()
        files = [f for f in files if f.lower().endswith(ext)]
    #print( "Now files are: ", files )
    if files is None:
        return None
    idx = random.randint(0, len(files) - 1)
    name = files[idx]
    result = _join_path(full_dir_path, name)
    return result



async def play_audio_waveform(duration_ms=3000):
    audio_power.on()
    buf = array('H', [2048 + int(0.2*2047 * math.sin(2 * math.pi * i / 32)) for i in range(128)])
    dac.write_timed(buf, 400 * len(buf), mode=DAC.CIRCULAR)
    await asyncio.sleep_ms(duration_ms)
    dac.write(2048)
    audio_power.off()


async def cuckoo_sequence():
    led2.on()

    #await play_audio_waveform()
    file_path = pick_random_file( "phrases" )
    print( "picked ", file_path )
    play_audio_file( file_path )

    led2.off()



async def main():
    audio_power.off()
    await servo_power_off()
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

        await asyncio.sleep_ms(2000)


asyncio.run(main())

