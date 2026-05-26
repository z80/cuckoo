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

SERMON_ROOT = "sermons"  # Root folder containing sermon subfolders
MIN_PAUSE_SEC = 3
MAX_PAUSE_SEC = 15
MIN_SEGMENTS = 3
MAX_SEGMENTS = 6

# Pause between files inside a sermon
MIN_FILE_PAUSE_SEC = 1
MAX_FILE_PAUSE_SEC = 3

# Pause between full sermon cycles
MIN_SERMON_PAUSE_SEC = 3
MAX_SERMON_PAUSE_SEC = 15



# Timer 3 can driver B0 and B1 pins through its channels 3 and 4.
timer = Timer(3, freq=1000)

audio_power = Pin('A5', Pin.OUT)     # Audio chip power
dac = DAC(Pin('A4'), bits=12)        # DAC output (PA4)

led1 = pyb.Pin('A15', Pin.OUT)
led2 = pyb.Pin('C10', Pin.OUT)


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


async def play_random_sermon():
    # List sermon folders and sort them
    sermon_folders = list_files(SERMON_ROOT)
    sermon_folders = sorted(sermon_folders)

    if not sermon_folders:
        print("No sermon folders found.")
        return

    print("Sermon folders:", sermon_folders)

    # Loop over folders in sorted order
    for folder in sermon_folders:
        full_path = _join_path(SERMON_ROOT, folder)

        # List .raw files inside this folder
        files = list_files(full_path)
        raw_files = [f for f in files if f.lower().endswith(".raw")]

        if not raw_files:
            print("No .raw files in", folder)
            continue

        # Pick one random file
        chosen = random.choice(raw_files)
        full_file_path = _join_path(full_path, chosen)

        print("Playing from folder:", folder, "file:", chosen)
        play_audio_file(full_file_path)

        # Pause between files
        pause = random.uniform(MIN_FILE_PAUSE_SEC, MAX_FILE_PAUSE_SEC)
        print("Pause between files:", pause)
        await asyncio.sleep(pause)

    # After finishing all folders, pause before next cycle
    sermon_pause = random.uniform(MIN_SERMON_PAUSE_SEC, MAX_SERMON_PAUSE_SEC)
    print("Finished sermon cycle. Pausing:", sermon_pause)
    await asyncio.sleep(sermon_pause)







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


def pick_random_file( last_file, full_dir_path=".", ext=None ):
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

    while True:
        idx = random.randint(0, len(files) - 1)
        name = files[idx]
        result = _join_path(full_dir_path, name)

        if result != last_file:
            break

    return result



async def cuckoo_sequence( starting_over, last_file ):
    led2.on()

    #await play_audio_waveform()
    if starting_over:
        file_path = "phrases/praise_the_omnissiah.raw"
    else:
        file_path = pick_random_file( last_file, "phrases" )
        print( "picked ", file_path )

    play_audio_file( file_path )

    led2.off()
    await asyncio.sleep_ms(2000)

    return file_path








async def main():

    audio_power.off()
    print("Sensor powered. Waiting for trigger...")
    while True:
        await play_random_sermon()

        pause_sec = random.uniform(MIN_PAUSE_SEC, MAX_PAUSE_SEC)
        print("Pausing for", pause_sec, "seconds")
        await asyncio.sleep(pause_sec)


asyncio.run(main())

