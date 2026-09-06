"""Remote facade matching :mod:`pc_transport_node_async`."""

import asyncio

from pc_transport_node_async import (
    DEFAULT_TIMEOUT,
    TransportProxyError,
    TransportProxyRemoteError,
    TransportProxyTimeout,
)

from .connection import (
    RelayBusyError,
    RelayClientConnection,
    RelayConnectionError,
    RelayTimeoutError,
    RemoteRelayError,
)
from .protocol import CALLBACK, CANCEL, EVENT, TRANSPORT


class RemoteTransportNode:
    """Network-backed equivalent of ``AsyncPCTransportNode``."""

    def __init__(self, connection, timeout=DEFAULT_TIMEOUT):
        self._connection = connection
        self.timeout = timeout
        self.node_id = None
        self._open_pipes = set()
        self._event_queue = asyncio.Queue()
        self._callback_tasks = {}
        self._event_task = asyncio.create_task(self._event_loop())
        connection.set_message_handler(self._receive_message)
        connection.set_disconnect_handler(self._disconnected)

    @classmethod
    async def connect(cls, endpoint, timeout=DEFAULT_TIMEOUT, **kwargs):
        connection = await RelayClientConnection.connect(
            endpoint, TRANSPORT, **kwargs
        )
        return cls(connection, timeout=timeout)

    async def close(self):
        for pipe_id in tuple(self._open_pipes):
            try:
                await self.send_pipe(pipe_id, b"", close=True)
            except Exception:
                pass
        self._open_pipes.clear()
        await self._connection.close()
        self._event_task.cancel()
        await asyncio.gather(self._event_task, return_exceptions=True)

    async def get_node_id(self):
        reply = await self._call("get_node_id")
        value = reply.get("value")
        if value is not None and not isinstance(value, int):
            raise TransportProxyError("invalid node-id response")
        self.node_id = value
        return value

    async def get_nodes_qty(self):
        reply = await self._call("get_nodes_qty")
        value = reply.get("value")
        if not isinstance(value, int):
            raise TransportProxyError("invalid node-count response")
        return value

    async def get_node_info(self, node_index):
        if not 0 <= node_index <= 255:
            raise ValueError("invalid node index")
        return (await self._call(
            "get_node_info", {"node_index": node_index}
        )).get("value")

    async def get_core_diagnostics(self):
        value = (await self._call("get_core_diagnostics")).get("value")
        if not isinstance(value, dict):
            raise TransportProxyError("invalid core diagnostics response")
        stats = value.get("stats")
        if isinstance(stats, list):
            value["stats"] = tuple(stats)
        return value

    async def send_command(self, node_id, command):
        await self._call(
            "send_command", {"node_id": node_id, "command": command}
        )
        return True

    async def send_command_and_wait_reply(
        self, node_id, command, timeout_ms=2000
    ):
        if not 1 <= timeout_ms <= 0xFFFF:
            raise ValueError("invalid request timeout")
        timeout = max(self.timeout, timeout_ms / 1000.0 + 1.0) + 2.0
        reply = await self._call(
            "send_command_and_wait_reply",
            {
                "node_id": node_id,
                "command": command,
                "timeout_ms": timeout_ms,
            },
            timeout=timeout,
        )
        return reply.get("value")

    async def open_pipe(self, node_id):
        pipe_id = (await self._call(
            "open_pipe", {"node_id": node_id}
        )).get("value")
        if not isinstance(pipe_id, int) or not 0 <= pipe_id <= 255:
            raise TransportProxyError("invalid pipe response")
        self._open_pipes.add(pipe_id)
        return pipe_id

    async def send_pipe(self, pipe_id, data, close=False):
        data = bytes(data)
        await self._call(
            "send_pipe",
            {"pipe_id": pipe_id, "close": bool(close)},
            data=data,
            timeout=max(self.timeout, len(data) / 4000.0 + 5.0) + 2.0,
            bulk=bool(data),
        )
        if close:
            self._open_pipes.discard(pipe_id)

    async def send_pipe_streamed(self, pipe_id, data, close=False):
        if not 0 <= pipe_id <= 255:
            raise ValueError("invalid pipe id")
        data = bytes(data)
        await self._call(
            "send_pipe_streamed",
            {"pipe_id": pipe_id, "close": bool(close)},
            data=data,
            timeout=max(self.timeout, len(data) / 4000.0 + 5.0) + 2.0,
            bulk=bool(data),
        )
        if close:
            self._open_pipes.discard(pipe_id)

    async def _call(
        self, operation, metadata=None, data=None, timeout=None, bulk=False
    ):
        try:
            message = await self._connection.call(
                operation,
                metadata,
                data,
                timeout=self.timeout + 2.0 if timeout is None else timeout,
                bulk=bulk,
            )
            return dict(message.metadata or {})
        except RelayTimeoutError as error:
            raise TransportProxyTimeout(str(error)) from error
        except (RelayBusyError, RemoteRelayError) as error:
            raise TransportProxyRemoteError(str(error)) from error
        except RelayConnectionError as error:
            raise TransportProxyError(str(error)) from error

    async def _receive_message(self, message):
        if message.kind == CANCEL:
            task = self._callback_tasks.get(message.id)
            if task is not None:
                task.cancel()
            return
        await self._event_queue.put(message)

    async def _event_loop(self):
        try:
            while True:
                message = await self._event_queue.get()
                try:
                    if message.kind == CALLBACK:
                        task = asyncio.create_task(
                            self._handle_callback(message)
                        )
                        self._callback_tasks[message.id] = task
                        try:
                            await task
                        finally:
                            self._callback_tasks.pop(message.id, None)
                    elif message.kind == EVENT:
                        await self._handle_event(message)
                except asyncio.CancelledError:
                    if message.kind != CALLBACK:
                        raise
                except Exception as error:
                    self.on_callback_error(error)
                finally:
                    self._event_queue.task_done()
        except asyncio.CancelledError:
            pass

    async def _handle_callback(self, message):
        metadata = message.metadata or {}
        if metadata.get("operation") != "on_command":
            await self._connection.send_error(
                message.id, "unsupported callback", error_type="CallbackError"
            )
            return
        try:
            result = self.on_command(
                metadata.get("src_id"), metadata.get("command")
            )
            if asyncio.iscoroutine(result):
                result = await result
            await self._connection.send_callback_result(
                message.id, {"result": result}
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.on_callback_error(error)
            await self._connection.send_error(message.id, error)

    async def _handle_event(self, message):
        metadata = message.metadata or {}
        operation = metadata.get("operation")
        if operation == "on_command_completed":
            result = self.on_command_completed(
                metadata.get("src_id"),
                metadata.get("command"),
                metadata.get("result"),
            )
        elif operation == "on_pipe_opened":
            result = self.on_pipe_opened(
                metadata.get("pipe_id"), metadata.get("src_id")
            )
        elif operation == "on_pipe_data":
            result = self.on_pipe_data(
                metadata.get("pipe_id"),
                metadata.get("src_id"),
                message.data or b"",
            )
        elif operation == "on_pipe_closed":
            result = self.on_pipe_closed(
                metadata.get("pipe_id"), metadata.get("src_id")
            )
        elif operation == "on_pipe_failed":
            result = self.on_pipe_failed(
                metadata.get("pipe_id"),
                metadata.get("src_id"),
                metadata.get("reason"),
                metadata.get("transferred_bytes"),
            )
        else:
            raise TransportProxyError(
                "unknown relay event: {}".format(operation)
            )
        if asyncio.iscoroutine(result):
            await result

    async def _disconnected(self, error):
        self.on_callback_error(TransportProxyError(str(error)))

    async def on_command(self, src_id, command):
        return None

    async def on_command_completed(self, src_id, command, result):
        pass

    async def on_pipe_opened(self, pipe_id, src_id):
        pass

    async def on_pipe_data(self, pipe_id, src_id, data_chunk):
        pass

    async def on_pipe_closed(self, pipe_id, src_id):
        pass

    async def on_pipe_failed(
        self, pipe_id, src_id, reason, transferred_bytes
    ):
        pass

    def on_callback_error(self, error):
        pass


__all__ = ("RemoteTransportNode",)
