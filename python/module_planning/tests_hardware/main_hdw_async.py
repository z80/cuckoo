
import sys
import asyncio

from tests_hardware.audio import load_audio_buffers
from pc_hardware_node import PCHardwareNode

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
    agen = await node.start_mic_stream( dest_id )
    try:
        async for chunk in agen:
            if chunk is None:
                print( "Received None" )

            else:
                qty = len(chunk)
                print(f"Received: {qty} bytes")

            await asyncio.sleep( 0.01 )
    except asyncio.CancelledError:
        print( "Generation stopped" )
    
        print( "Stopping mic stream" )
        await node.stop_mic_stream( dest_id )
        print( "Mic stream is stopped" )

        raise



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

        #await test_mic( node, target_id )

        await test_speaker( node, target_id )

    except KeyboardInterrupt:
        print("\nExited.")



asyncio.run( main() )


