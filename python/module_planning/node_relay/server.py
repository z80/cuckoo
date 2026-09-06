"""Gateway-side relay servers for USB transport and hardware facades."""

import asyncio

from pc_hardware_node import PCHardwareNode
from pc_transport_node_async import AsyncPCTransportNode

from .connection import (
    ClientDisconnected,
    RelayDisconnectedError,
    RelayServerEndpoint,
    ServerMessage,
)
from .protocol import CALL, CANCEL, HARDWARE, TRANSPORT, validate_service
from .remote_hardware import MAX_PLAY_BYTES


class _RelayedTransportBackend(AsyncPCTransportNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.relay = None

    async def on_command(self, src_id, command):
        relay = self.relay
        if relay is None or not relay.connected:
            return {"ok": False, "err": "relay disconnected"}
        try:
            reply = await relay.callback(
                "on_command", {"src_id": src_id, "command": command}
            )
            return (reply.metadata or {}).get("result")
        except Exception as error:
            self.on_callback_error(error)
            return {"ok": False, "err": "relay callback failed"}

    async def on_command_completed(self, src_id, command, result):
        relay = self.relay
        if relay is not None and relay.connected:
            await relay.event("on_command_completed", {
                "src_id": src_id,
                "command": command,
                "result": result,
            })

    async def on_pipe_opened(self, pipe_id, src_id):
        await self._event(
            "on_pipe_opened", {"pipe_id": pipe_id, "src_id": src_id}
        )

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        await self._event(
            "on_pipe_data",
            {"pipe_id": pipe_id, "src_id": src_id},
            data=data_chunk,
            bulk=True,
        )

    async def on_pipe_closed(self, pipe_id, src_id):
        await self._event(
            "on_pipe_closed", {"pipe_id": pipe_id, "src_id": src_id}
        )

    async def on_pipe_failed(
        self, pipe_id, src_id, reason, transferred_bytes
    ):
        await self._event("on_pipe_failed", {
            "pipe_id": pipe_id,
            "src_id": src_id,
            "reason": reason,
            "transferred_bytes": transferred_bytes,
        })

    async def _event(self, operation, metadata, data=None, bulk=False):
        relay = self.relay
        if relay is not None and relay.connected:
            try:
                await relay.event(
                    operation, metadata, data=data, bulk=bulk
                )
            except RelayDisconnectedError:
                pass


class NodeRelayServer:
    """Dispatch one selected facade to one active network client."""

    def __init__(self, endpoint, backend, service):
        self.endpoint = endpoint
        self.backend = backend
        self.service = validate_service(service)
        self._calls = {}
        self._suppressed = set()
        self._cleanup_tasks = set()
        self._closed = False
        self._serve_task = asyncio.create_task(self._serve())

    @property
    def connected(self):
        return self.endpoint.connected

    async def serve_forever(self):
        await self._serve_task

    async def close(self):
        if self._closed:
            return
        self._closed = True
        self._serve_task.cancel()
        await asyncio.gather(self._serve_task, return_exceptions=True)
        await self.endpoint.close()
        for task in tuple(self._calls.values()):
            if self.service == HARDWARE:
                task.cancel()
        if self.service == HARDWARE and self._calls:
            await asyncio.gather(
                *tuple(self._calls.values()), return_exceptions=True
            )
        for task in tuple(self._cleanup_tasks):
            task.cancel()
        if self._cleanup_tasks:
            await asyncio.gather(
                *tuple(self._cleanup_tasks), return_exceptions=True
            )
        await self.backend.close()
        mic_pump = getattr(self, "_mic_pump", None)
        if mic_pump is not None:
            mic_pump.cancel()
            await asyncio.gather(mic_pump, return_exceptions=True)
        if self.service == TRANSPORT and self._calls:
            await asyncio.gather(
                *tuple(self._calls.values()), return_exceptions=True
            )

    async def _serve(self):
        try:
            while True:
                envelope = await self.endpoint.receive()
                if isinstance(envelope, ClientDisconnected):
                    self._client_disconnected()
                elif isinstance(envelope, ServerMessage):
                    message = envelope.message
                    if message.kind == CALL:
                        self._start_call(envelope)
                    elif message.kind == CANCEL:
                        self._cancel_call(message.id)
        except asyncio.CancelledError:
            pass

    def _start_call(self, envelope):
        request_id = envelope.message.id
        if request_id in self._calls:
            task = asyncio.create_task(self.endpoint.error(
                request_id,
                "duplicate request id",
                identity=envelope.identity,
                error_type="ProtocolError",
            ))
            self._track_cleanup(task)
            return
        task = asyncio.create_task(self._run_call(envelope))
        self._calls[request_id] = task

    def _cancel_call(self, request_id):
        task = self._calls.get(request_id)
        if task is None:
            return
        self._suppressed.add(request_id)
        # PCHardwareNode.play_buffer has cancellation-safe pipe cleanup.
        # Raw USB proxy requests are allowed to settle so their internal
        # request slots cannot be stranded by task cancellation.
        if self.service == HARDWARE:
            task.cancel()

    async def _run_call(self, envelope):
        message = envelope.message
        request_id = message.id
        try:
            metadata, data, bulk = await self._dispatch(message)
            if request_id not in self._suppressed:
                await self.endpoint.result(
                    request_id,
                    metadata,
                    data,
                    identity=envelope.identity,
                    bulk=bulk,
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if request_id not in self._suppressed:
                try:
                    await self.endpoint.error(
                        request_id, error, identity=envelope.identity
                    )
                except RelayDisconnectedError:
                    pass
        finally:
            self._calls.pop(request_id, None)
            self._suppressed.discard(request_id)

    async def _dispatch(self, message):
        metadata = dict(message.metadata or {})
        operation = metadata.pop("operation", None)
        if self.service == TRANSPORT:
            value = await self._dispatch_transport(
                operation, metadata, message.data
            )
        else:
            value = await self._dispatch_hardware(
                operation, metadata, message.data
            )
            if operation == "start_mic_stream":
                return (value, None, False)
        return ({"value": value} if value is not None else {}, None, False)

    async def _common_call(self, operation, metadata):
        if operation == "get_node_id":
            return True, await self.backend.get_node_id()
        if operation == "get_nodes_qty":
            return True, await self.backend.get_nodes_qty()
        if operation == "get_node_info":
            return True, await self.backend.get_node_info(
                metadata.get("node_index")
            )
        if operation == "get_core_diagnostics":
            return True, await self.backend.get_core_diagnostics()
        return False, None

    async def _dispatch_transport(self, operation, metadata, data):
        handled, value = await self._common_call(operation, metadata)
        if handled:
            return value
        if operation == "send_command":
            return await self.backend.send_command(
                metadata.get("node_id"), metadata.get("command")
            )
        if operation == "send_command_and_wait_reply":
            return await self.backend.send_command_and_wait_reply(
                metadata.get("node_id"),
                metadata.get("command"),
                timeout_ms=metadata.get("timeout_ms", 2000),
            )
        if operation == "open_pipe":
            return await self.backend.open_pipe(metadata.get("node_id"))
        if operation == "send_pipe":
            await self.backend.send_pipe(
                metadata.get("pipe_id"),
                data or b"",
                close=bool(metadata.get("close")),
            )
            return None
        if operation == "send_pipe_streamed":
            await self.backend.send_pipe_streamed(
                metadata.get("pipe_id"),
                data or b"",
                close=bool(metadata.get("close")),
            )
            return None
        raise ValueError("unsupported transport operation: {}".format(
            operation
        ))

    async def _dispatch_hardware(self, operation, metadata, data):
        handled, value = await self._common_call(operation, metadata)
        if handled:
            return value
        if operation == "play_buffer":
            payload = data or b""
            if not payload or len(payload) & 1:
                raise ValueError(
                    "speaker data must contain whole 16-bit samples"
                )
            if len(payload) > MAX_PLAY_BYTES:
                raise ValueError("speaker data exceeds 30-second limit")
            await self.backend.play_buffer(metadata.get("node_id"), payload)
            return None
        if operation == "start_mic_stream":
            return await self._start_mic(metadata.get("node_id"))
        if operation == "stop_mic_stream":
            await self.backend.stop_mic_stream(metadata.get("node_id"))
            return None
        if operation == "set_pyro_enable":
            return await self.backend.set_pyro_enable(
                metadata.get("node_id"), bool(metadata.get("enable"))
            )
        if operation == "get_pyro_state":
            return await self.backend.get_pyro_state(
                metadata.get("node_id")
            )
        raise ValueError("unsupported hardware operation: {}".format(
            operation
        ))

    async def _start_mic(self, node_id):
        if getattr(self, "_mic_pump", None) is not None:
            raise RuntimeError("microphone relay already active")
        stream = await self.backend.start_mic_stream(node_id)
        stream_id = getattr(self, "_next_stream_id", 1)
        self._next_stream_id = 1 if stream_id == 0xFFFFFFFF else stream_id + 1
        self._mic_stream = stream
        self._mic_node_id = node_id
        self._mic_pump = asyncio.create_task(
            self._pump_mic(stream_id, stream)
        )
        return {
            "stream_id": stream_id,
            "pipe_id": getattr(stream, "pipe_id", None),
        }

    async def _pump_mic(self, stream_id, stream):
        stop_source = False
        try:
            while True:
                data = await stream.read()
                overruns = getattr(stream, "overrun_count", 0)
                dropped = getattr(stream, "dropped_bytes", 0)
                if overruns or dropped:
                    stop_source = True
                    if self.connected:
                        try:
                            await self.endpoint.event("mic_error", {
                                "stream_id": stream_id,
                                "message": "microphone source overrun",
                                "overrun_count": overruns,
                                "dropped_bytes": dropped,
                            })
                        except RelayDisconnectedError:
                            pass
                    break
                if not data:
                    if self.connected:
                        await self.endpoint.event(
                            "mic_closed", {"stream_id": stream_id}
                        )
                    break
                if self.connected:
                    try:
                        await self.endpoint.event(
                            "mic_data",
                            {
                                "stream_id": stream_id,
                                "pipe_id": getattr(stream, "pipe_id", None),
                                "overrun_count": 0,
                                "dropped_bytes": 0,
                            },
                            data=data,
                            bulk=True,
                        )
                    except RelayDisconnectedError:
                        pass
                # With no client, reading and doing nothing purges the data.
        except asyncio.CancelledError:
            raise
        except RelayDisconnectedError:
            pass
        except Exception as error:
            if self.connected:
                try:
                    await self.endpoint.event("mic_error", {
                        "stream_id": stream_id,
                        "message": str(error),
                    })
                except RelayDisconnectedError:
                    pass
        finally:
            if stop_source:
                try:
                    await self.backend.stop_mic_stream(
                        getattr(self, "_mic_node_id", None)
                    )
                except Exception:
                    pass
            if getattr(self, "_mic_stream", None) is stream:
                self._mic_stream = None
                self._mic_node_id = None
                self._mic_pump = None

    def _client_disconnected(self):
        for request_id, task in tuple(self._calls.items()):
            self._suppressed.add(request_id)
            if self.service == HARDWARE:
                task.cancel()
        if self.service == HARDWARE:
            task = asyncio.create_task(self._cleanup_hardware_client())
        else:
            task = asyncio.create_task(self._cleanup_transport_client())
        self._track_cleanup(task)

    async def _cleanup_hardware_client(self):
        node_id = getattr(self, "_mic_node_id", None)
        if node_id is not None:
            try:
                await self.backend.stop_mic_stream(node_id)
            except Exception:
                pass

    async def _cleanup_transport_client(self):
        for pipe_id in tuple(getattr(self.backend, "_open_pipes", ())):
            try:
                await self.backend.send_pipe(pipe_id, b"", close=True)
            except Exception:
                pass

    def _track_cleanup(self, task):
        self._cleanup_tasks.add(task)
        task.add_done_callback(self._cleanup_tasks.discard)


class TransportNodeRelayServer(NodeRelayServer):
    def __init__(self, endpoint, backend):
        super().__init__(endpoint, backend, TRANSPORT)
        if isinstance(backend, _RelayedTransportBackend):
            backend.relay = endpoint


class HardwareNodeRelayServer(NodeRelayServer):
    def __init__(self, endpoint, backend):
        self._mic_stream = None
        self._mic_node_id = None
        self._mic_pump = None
        self._next_stream_id = 1
        super().__init__(endpoint, backend, HARDWARE)


async def create_relay_server(
    service,
    port=None,
    bind="tcp://127.0.0.1:43840",
    *,
    baudrate=115200,
    timeout=3.0,
    backend=None,
    context=None,
):
    service = validate_service(service)
    if backend is None:
        if port is None:
            raise ValueError("port is required")
        backend_type = (
            _RelayedTransportBackend if service == TRANSPORT
            else PCHardwareNode
        )
        backend = await backend_type.create(
            port=port, baudrate=baudrate, timeout=timeout
        )
    endpoint = await RelayServerEndpoint.bind(
        bind, service, context=context
    )
    server_type = (
        TransportNodeRelayServer if service == TRANSPORT
        else HardwareNodeRelayServer
    )
    return server_type(endpoint, backend)


__all__ = (
    "HardwareNodeRelayServer",
    "NodeRelayServer",
    "TransportNodeRelayServer",
    "create_relay_server",
)
