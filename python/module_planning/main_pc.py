import sys
import time

from pc_transport_node import PCTransportNode


PORT = sys.argv[1] if len(sys.argv) > 1 else "COM7"


class PCNode(PCTransportNode):
    def on_command(self, src_id, command):
        print("command from", src_id, command)
        return {"ok": True}

    def on_pipe_opened(self, pipe_id, src_id):
        print("pipe opened", pipe_id, "from", src_id)

    def on_pipe_data(self, pipe_id, src_id, data_chunk):
        print("pipe data", pipe_id, "from", src_id, repr(data_chunk))

    def on_pipe_closed(self, pipe_id, src_id):
        print("pipe closed", pipe_id, "from", src_id)

    def on_pipe_failed(self, pipe_id, src_id, reason, transferred_bytes):
        print("pipe failed", pipe_id, "from", src_id, "reason", reason,
              "bytes", transferred_bytes)

    def on_callback_error(self, error):
        print("callback error:", error)

    def on_deferred_error(self, error):
        print("deferred call error:", error)


node = PCNode(PORT)

try:
    while node.get_node_id() is None:
        print("waiting for NRF registration")
        node.poll(250)
        time.sleep(0.75)

    print("PC node ID:", node.node_id)
    print("online nodes:", node.get_nodes_qty())

    for index in range(node.get_nodes_qty()):
        print("node", index, node.get_node_info(index))

    print("servicing callbacks; press Ctrl-C on the PC to stop")
    node.process()
except KeyboardInterrupt:
    pass
finally:
    node.close()
