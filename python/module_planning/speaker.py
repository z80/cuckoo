import uasyncio as asyncio

import os
import pyb
from pyb import Pin, DAC, Timer

from array import array
import time

SAMPLE_RATE = 16000
CHUNK_SIZE = 4000
CHUNK_PERIOD_US = int(1_000_000 * CHUNK_SIZE / SAMPLE_RATE)
CHUNK_PERIOD_MS = CHUNK_PERIOD_US // 1000

async def delay_until(t_ref, period_us):
    now = time.ticks_us()
    wait = time.ticks_diff(t_ref + period_us, now)
    if wait > 0:
        asyncio.sleep_ms(0)
    return t_ref + period_us



class Speaker:
    def __init__( self ):
        self.pin_power = Pin( "C15", Pin.OUT )
        
        self.init_dac()

        self.buffer_a = array('H', bytearray(CHUNK_SIZE * 2) )
        self.buffer_b = array('H', bytearray(CHUNK_SIZE * 2) )
        self.buffer = self.buffer_a
        self.write_index = 0
        self.event_buffer_ready = asyncio.Event()

    def _power_enable( self, en ):
        if en:
            pin_power.on()
        else:
            pin_power.off()


    def _init_dac( self ):
        self.dac = DAC(Pin('A4'), bits=12, buffering=True)


    async def _request_next_chunk( self, node, src_id ):
        cmd = { "cmd": "audio_chunk", "size": CHUNK_SIZE }
        reply = await node.send_command_and_wait_reply(
            src_id,
            timeout_ms=5000,
        )
        print("on audio_chunk reply:", reply)

    async def _next_chunk( self, node, src_id ):
        await self._request_next_chunk( node, src_id )
        try:
            await asyncio.wait_for_ms( self.event_buffer_ready.wait(), timeout=CHUNK_PERIOD_MS)

            ret = self.buffer
            if self.buffer == self.buffer_a:
                self.buffer = self.buffer_b
            else:
                self.buffer = self.buffer_a
            self.write_index = 0
            self.event_buffer_ready.reset()

        except asyncio.TimeoutError:
            print( "Failed to receive audio chunk in time" )
            ret = None

        return ret


    def on_stream_data( self, data_chunk ):
        arr = array('H', data_chunk)
        qty = len(arr)
        n = self.write_index + qty
        if n > CHUNK_SIZE:
        n = CHUNK_SIZE
        t = 0
        for i in range( self.write_index, n ):
            self.buffer[i] = arr[t]
            t += 1

        self.write_index += n
        if self.write_index >= CHUNK_SIZE:
            self.event_buffer_ready.set()
    

    async def play_stream( self, node, src_id ):
        self._power_enable( True )
        chunk = await self._next_chunk( node, src_id )
        
        while chunk != None:
            t_until = time.ticks_us()
            self.dac.write_timed( chunk, SAMPLE_RATE, mode=DAC.NORMAL)

            next_chunk = await self.next_chunk( node, src_id )
            await delay_until( t_until, CHUNK_PERIOD_US )
        
        self._power_enable( False )


