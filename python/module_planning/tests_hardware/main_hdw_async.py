
import sys
import asyncio
import numpy as np

#from tests_hardware.audio import load_audio_buffers
from pc_hardware_node import PCHardwareNode

def uint16_to_float32(data: bytes) -> np.ndarray:
    """Incoming radio mic: unsigned 16-bit 12-bit-scaled → float32 -1..1"""
    samples = np.frombuffer(data, dtype=np.uint16)
    signed = (samples.astype(np.int32) << 4) - 32768
    return signed.astype(np.float32) / 32768.0


async def find_target( node ):
    quantity = await node.get_nodes_qty()
    print("online nodes:", quantity)
    for index in range(quantity):
        info = await node.get_node_info(index)
        print("node", index, info)
        node_id = info.get("id") if info else None
        if node_id is not None and node_id != node.node_id:
            return node_id
    return None



async def test_pyro( node, dest_id ):
    ret = await node.set_pyro_enable( dest_id, True )
    print( "set_pyro_enable: ", ret )

    ret = await node.get_pyro_state( dest_id )
    print( "get_pyro_state: ", ret )



async def test_mic( node, dest_id ):
    try:
        agen = await node.start_mic_stream( dest_id )
        counter = 0
        async for chunk in agen:
            if chunk is None:
                print( "Received None" )

            else:
                qty = len(chunk)

                samples = uint16_to_float32(chunk)
                rms = np.sqrt(np.mean( (samples - np.mean(samples))**2 ) )
                print(f"Received: {qty} bytes; rms: {rms}" )
                if rms > 0.99:
                    print( samples )

            await asyncio.sleep( 0.01 )

            counter += 1
            if counter >= 500:
                break
        await node.stop_mic_stream( dest_id )

    except asyncio.CancelledError:
        print( "Generation stopped" )
    
        print( "Stopping mic stream" )
        await node.stop_mic_stream( dest_id )
        print( "Mic stream is stopped" )

        raise

    except:
        pass



async def test_speaker( node, dest_id ):
    #import pdb
    #pdb.set_trace()
    streams = load_audio_buffers( "../waveform_preparation/sermons/as_is/01" )
    key = list(streams.keys())[0]
    stream = streams[key]
    await node.play_buffer( dest_id, stream )
    print( "done." )



async def main():
    #import pdb
    #pdb.set_trace()

    PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"

    node = await PCHardwareNode.create( port=PORT )
    phase = "setup"
    try:
        while await node.get_node_id() is None:
            print("waiting for NRF registration")
            await asyncio.sleep(1)
        print("PC node ID:", node.node_id)

        target_id = await find_target(node)
        if target_id is None:
            print("No remote node available")
            return

        await test_pyro( node, target_id )
        
        while True:
            await test_mic( node, target_id )
            await asyncio.sleep( 1.0 )

        #await test_speaker( node, target_id )

    except KeyboardInterrupt:
        print("\nExited.")



asyncio.run( main() )


