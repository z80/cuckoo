"""Network relay facades for PC-connected NRF transport nodes."""

import asyncio
import sys

# pyzmq requires an add_reader-capable loop on Windows.  Install the built-in
# selector policy while this package is imported, before callers use
# asyncio.run().  This avoids adding Tornado solely as an event-loop adapter.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from .protocol import HARDWARE, TRANSPORT, validate_service
from .remote_hardware import RemoteHardwareNode, RemoteMicStream
from .remote_transport import RemoteTransportNode
from .server import (
    HardwareNodeRelayServer,
    TransportNodeRelayServer,
    create_relay_server,
)


async def create_remote_node(service, endpoint, **kwargs):
    """Connect the remote facade selected by ``transport`` or ``hardware``."""
    service = validate_service(service)
    node_type = {
        TRANSPORT: RemoteTransportNode,
        HARDWARE: RemoteHardwareNode,
    }[service]
    return await node_type.connect(endpoint, **kwargs)


__all__ = (
    "HARDWARE",
    "TRANSPORT",
    "HardwareNodeRelayServer",
    "RemoteHardwareNode",
    "RemoteMicStream",
    "RemoteTransportNode",
    "TransportNodeRelayServer",
    "create_relay_server",
    "create_remote_node",
)
