
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



async def main():
    import pdb
    pdb.set_trace()

    PORT = sys.argv[1] if len(sys.argv) > 1 else "COM8"

    node = await PCHardwareNode.create( port=PORT )
    phase = "setup"
    try:
        while await node.get_node_id() is None:
            print("waiting for NRF registration")
            await asyncio.sleep(1)
        print("PC node ID:", node.node_id)

        target = await find_target(node)
        if target is None:
            print("No remote node available")
            return

        await test_pyro()

    except KeyboardInterrupt:
        print("\nExited.")



asyncio.run( main() )


