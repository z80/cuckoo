"""Asynchronous ZMQ connection machinery for the node relay."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
from typing import Any, Awaitable, Callable, Mapping, Optional, Union

import zmq
import zmq.asyncio

from .protocol import (
    BUSY,
    CALL,
    CALLBACK,
    CALLBACK_RESULT,
    CANCEL,
    ERROR,
    EVENT,
    HARDWARE,
    HEARTBEAT,
    HEARTBEAT_ACK,
    HELLO,
    RESULT,
    TRANSPORT,
    Message,
    ProtocolError,
    decode_message,
    encode,
    error_metadata,
    validate_service,
)


DEFAULT_HEARTBEAT_INTERVAL = 1.0
DEFAULT_CONNECTION_TIMEOUT = 5.0
DEFAULT_CALLBACK_TIMEOUT = 10.0
DEFAULT_CALL_TIMEOUT = 30.0
DEFAULT_BULK_QUEUE_MESSAGES = 8

MessageHandler = Callable[[Message], Awaitable[None]]
DisconnectHandler = Callable[[BaseException], Awaitable[None]]


class RelayConnectionError(RuntimeError):
    pass


class RelayDisconnectedError(RelayConnectionError):
    pass


class RelayTimeoutError(RelayConnectionError):
    pass


class RelayBusyError(RelayConnectionError):
    pass


class RemoteRelayError(RelayConnectionError):
    def __init__(
        self,
        error_type: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.error_type = error_type
        self.details = dict(details) if details is not None else None

    @classmethod
    def from_message(cls, message: Message) -> "RemoteRelayError":
        metadata = message.metadata or {}
        return cls(
            str(metadata.get("type", "RelayError")),
            str(metadata.get("message", "remote relay error")),
            metadata.get("details") if isinstance(metadata.get("details"), dict) else None,
        )


@dataclass(frozen=True)
class ServerMessage:
    identity: bytes
    message: Message


@dataclass(frozen=True)
class ClientDisconnected:
    identity: bytes
    reason: RelayDisconnectedError


ServerEvent = Union[ServerMessage, ClientDisconnected]


@dataclass
class _ClientOutbound:
    message: Message


@dataclass
class _ServerOutbound:
    identity: bytes
    message: Message


def _operation_metadata(
    operation: str, metadata: Mapping[str, Any] | None
) -> dict[str, Any]:
    if not isinstance(operation, str) or not operation:
        raise ValueError("operation must be a non-empty string")
    result = dict(metadata or {})
    if "operation" in result:
        raise ValueError("metadata must not contain operation")
    result["operation"] = operation
    return result


async def _cancel_tasks(*tasks: Optional[asyncio.Task]) -> None:
    active = [task for task in tasks if task is not None and not task.done()]
    for task in active:
        task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)


class RelayClientConnection:
    """Multiplexed DEALER connection used by both remote node facades."""

    def __init__(
        self,
        endpoint: str,
        service: str,
        *,
        identity: bytes | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
        context: zmq.asyncio.Context | None = None,
    ):
        self.endpoint = endpoint
        self.service = validate_service(service)
        self.heartbeat_interval = float(heartbeat_interval)
        self.connection_timeout = float(connection_timeout)
        if self.heartbeat_interval <= 0 or self.connection_timeout <= 0:
            raise ValueError("heartbeat timings must be positive")

        self._context = context or zmq.asyncio.Context.instance()
        self._socket = self._context.socket(zmq.DEALER)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt(zmq.SNDHWM, DEFAULT_BULK_QUEUE_MESSAGES)
        self._socket.setsockopt(zmq.RCVHWM, DEFAULT_BULK_QUEUE_MESSAGES)
        self._socket.setsockopt(zmq.IDENTITY, identity or secrets.token_bytes(16))
        self._socket.connect(endpoint)
        self._control: asyncio.Queue[_ClientOutbound] = asyncio.Queue()
        self._bulk: asyncio.Queue[_ClientOutbound] = asyncio.Queue(
            maxsize=DEFAULT_BULK_QUEUE_MESSAGES
        )
        self._pending: dict[int, asyncio.Future[Message]] = {}
        self._next_id = secrets.randbelow(0xFFFFFFFF) + 1
        self._message_handler: MessageHandler | None = None
        self._disconnect_handler: DisconnectHandler | None = None
        self._handler_tasks: set[asyncio.Task] = set()
        self._socket_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._closed = asyncio.Event()
        self._close_reason: RelayDisconnectedError | None = None
        self._last_received = asyncio.get_running_loop().time()

    @classmethod
    async def connect(
        cls,
        endpoint: str,
        service: str,
        *,
        identity: bytes | None = None,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
        context: zmq.asyncio.Context | None = None,
    ) -> "RelayClientConnection":
        self = cls(
            endpoint,
            service,
            identity=identity,
            heartbeat_interval=heartbeat_interval,
            connection_timeout=connection_timeout,
            context=context,
        )
        self._socket_task = asyncio.create_task(self._socket_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            reply = await self.request(
                HELLO,
                {"service": self.service},
                timeout=self.connection_timeout,
                cancel_remote=False,
            )
            actual_service = (reply.metadata or {}).get("service")
            if actual_service != self.service:
                raise RelayConnectionError(
                    "relay service mismatch: expected %s, got %r"
                    % (self.service, actual_service)
                )
            return self
        except BaseException:
            await self.close()
            raise

    @property
    def connected(self) -> bool:
        return not self._closed.is_set()

    @property
    def close_reason(self) -> RelayDisconnectedError | None:
        return self._close_reason

    def set_message_handler(self, handler: MessageHandler | None) -> None:
        self._message_handler = handler

    def set_disconnect_handler(self, handler: DisconnectHandler | None) -> None:
        self._disconnect_handler = handler

    def _allocate_id(self) -> int:
        for _ in range(0xFFFFFFFF):
            result = self._next_id
            self._next_id = 1 if result == 0xFFFFFFFF else result + 1
            if result not in self._pending:
                return result
        raise RelayConnectionError("no relay request identifiers available")

    async def call(
        self,
        operation: str,
        metadata: Mapping[str, Any] | None = None,
        data: bytes | bytearray | memoryview | None = None,
        *,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
        bulk: bool = False,
    ) -> Message:
        return await self.request(
            CALL,
            _operation_metadata(operation, metadata),
            data,
            timeout=timeout,
            bulk=bulk,
        )

    async def request(
        self,
        kind: int,
        metadata: Mapping[str, Any] | None = None,
        data: bytes | bytearray | memoryview | None = None,
        *,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
        bulk: bool = False,
        cancel_remote: bool = True,
    ) -> Message:
        if not self.connected:
            raise self._close_reason or RelayDisconnectedError("relay is closed")
        message_id = self._allocate_id()
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self.send(Message(kind, message_id, metadata or {}, None if data is None else bytes(data)), bulk=bulk)
        try:
            if timeout is None:
                return await future
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError as exc:
            if cancel_remote and self.connected:
                await self.send(Message(CANCEL, message_id, {}))
            raise RelayTimeoutError("relay request timed out") from exc
        except asyncio.CancelledError:
            if cancel_remote and self.connected:
                await self.send(Message(CANCEL, message_id, {}))
            raise
        finally:
            self._pending.pop(message_id, None)
            if not future.done():
                future.cancel()

    async def send(self, message: Message, *, bulk: bool = False) -> None:
        if not self.connected:
            raise self._close_reason or RelayDisconnectedError("relay is closed")
        queue = self._bulk if bulk else self._control
        await queue.put(_ClientOutbound(message))

    async def send_callback_result(
        self,
        callback_id: int,
        metadata: Mapping[str, Any] | None = None,
        data: bytes | bytearray | memoryview | None = None,
    ) -> None:
        await self.send(
            Message(
                CALLBACK_RESULT,
                callback_id,
                metadata or {},
                None if data is None else bytes(data),
            )
        )

    async def send_error(
        self,
        request_id: int,
        error: BaseException | str,
        *,
        error_type: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        await self.send(
            Message(
                ERROR,
                request_id,
                error_metadata(error, error_type=error_type, details=details),
            )
        )

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def close(self) -> None:
        if not self._closed.is_set():
            self._mark_disconnected(RelayDisconnectedError("relay closed"), notify=False)
        current = asyncio.current_task()
        tasks = [task for task in (self._socket_task, self._heartbeat_task) if task is not current]
        await _cancel_tasks(*tasks)
        await _cancel_tasks(*list(self._handler_tasks))
        self._socket.close(0)

    async def _socket_loop(self) -> None:
        receive = asyncio.ensure_future(self._socket.recv_multipart())
        control = asyncio.create_task(self._control.get())
        bulk = asyncio.create_task(self._bulk.get())
        try:
            while self.connected:
                done, _ = await asyncio.wait(
                    (receive, control, bulk), return_when=asyncio.FIRST_COMPLETED
                )
                if receive in done:
                    frames = receive.result()
                    receive = asyncio.ensure_future(
                        self._socket.recv_multipart()
                    )
                    self._last_received = asyncio.get_running_loop().time()
                    await self._receive(decode_message(frames))
                if control in done:
                    outbound = control.result()
                    await self._socket.send_multipart(encode(outbound.message))
                    while not self._control.empty():
                        outbound = self._control.get_nowait()
                        await self._socket.send_multipart(encode(outbound.message))
                    control = asyncio.create_task(self._control.get())
                if bulk in done:
                    outbound = bulk.result()
                    await self._socket.send_multipart(encode(outbound.message))
                    bulk = asyncio.create_task(self._bulk.get())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._mark_disconnected(
                RelayDisconnectedError("relay connection failed: %s" % (exc,))
            )
        finally:
            await _cancel_tasks(receive, control, bulk)

    async def _receive(self, message: Message) -> None:
        if message.kind in (RESULT, ERROR, BUSY, CALLBACK_RESULT):
            future = self._pending.get(message.id)
            if future is None or future.done():
                return
            if message.kind == ERROR:
                future.set_exception(RemoteRelayError.from_message(message))
            elif message.kind == BUSY:
                metadata = message.metadata or {}
                future.set_exception(
                    RelayBusyError(str(metadata.get("message", "relay busy")))
                )
            else:
                future.set_result(message)
            return
        if message.kind == HEARTBEAT_ACK:
            return
        if message.kind in (CALLBACK, EVENT, CANCEL):
            if self._message_handler is None:
                if message.kind == CALLBACK:
                    await self.send_error(
                        message.id,
                        "no callback handler",
                        error_type="CallbackUnavailable",
                    )
                return
            task = asyncio.create_task(self._dispatch_message(message))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)

    async def _dispatch_message(self, message: Message) -> None:
        try:
            await self._message_handler(message)  # type: ignore[misc]
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if message.kind == CALLBACK and self.connected:
                await self.send_error(message.id, exc)

    async def _heartbeat_loop(self) -> None:
        try:
            while self.connected:
                await asyncio.sleep(self.heartbeat_interval)
                now = asyncio.get_running_loop().time()
                if now - self._last_received >= self.connection_timeout:
                    self._mark_disconnected(
                        RelayDisconnectedError("relay heartbeat timed out")
                    )
                    return
                await self.send(Message(HEARTBEAT, 0, {}))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._mark_disconnected(
                RelayDisconnectedError("relay heartbeat failed: %s" % (exc,))
            )

    def _mark_disconnected(
        self, reason: RelayDisconnectedError, *, notify: bool = True
    ) -> None:
        if self._closed.is_set():
            return
        self._close_reason = reason
        self._closed.set()
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(reason)
        if notify and self._disconnect_handler is not None:
            task = asyncio.create_task(self._disconnect_handler(reason))
            self._handler_tasks.add(task)
            task.add_done_callback(self._handler_tasks.discard)


class RelayServerEndpoint:
    """Single-client ROUTER endpoint used by both relay server modes."""

    def __init__(
        self,
        endpoint: str,
        service: str,
        *,
        heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
        callback_timeout: float = DEFAULT_CALLBACK_TIMEOUT,
        context: zmq.asyncio.Context | None = None,
    ):
        self.endpoint = endpoint
        self.service = validate_service(service)
        self.heartbeat_interval = float(heartbeat_interval)
        self.connection_timeout = float(connection_timeout)
        self.callback_timeout = float(callback_timeout)
        if min(self.heartbeat_interval, self.connection_timeout, self.callback_timeout) <= 0:
            raise ValueError("relay timings must be positive")

        self._context = context or zmq.asyncio.Context.instance()
        self._socket = self._context.socket(zmq.ROUTER)
        self._socket.setsockopt(zmq.LINGER, 0)
        self._socket.setsockopt(zmq.SNDHWM, DEFAULT_BULK_QUEUE_MESSAGES)
        self._socket.setsockopt(zmq.RCVHWM, DEFAULT_BULK_QUEUE_MESSAGES)
        self._socket.bind(endpoint)
        self._control: asyncio.Queue[_ServerOutbound] = asyncio.Queue()
        self._bulk: asyncio.Queue[_ServerOutbound] = asyncio.Queue(
            maxsize=DEFAULT_BULK_QUEUE_MESSAGES
        )
        self._events: asyncio.Queue[ServerEvent] = asyncio.Queue()
        self._pending: dict[int, asyncio.Future[Message]] = {}
        self._next_id = secrets.randbelow(0xFFFFFFFF) + 1
        self._active_identity: bytes | None = None
        self._last_received = 0.0
        self._closed = asyncio.Event()
        self._socket_task = asyncio.create_task(self._socket_loop())
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    @classmethod
    async def bind(
        cls,
        endpoint: str,
        service: str,
        **kwargs: Any,
    ) -> "RelayServerEndpoint":
        return cls(endpoint, service, **kwargs)

    @property
    def connected(self) -> bool:
        return self._active_identity is not None

    @property
    def active_identity(self) -> bytes | None:
        return self._active_identity

    def _allocate_id(self) -> int:
        for _ in range(0xFFFFFFFF):
            result = self._next_id
            self._next_id = 1 if result == 0xFFFFFFFF else result + 1
            if result not in self._pending:
                return result
        raise RelayConnectionError("no relay callback identifiers available")

    async def receive(self) -> ServerEvent:
        return await self._events.get()

    async def send(
        self,
        message: Message,
        *,
        identity: bytes | None = None,
        bulk: bool = False,
    ) -> None:
        target = identity or self._active_identity
        if target is None or target != self._active_identity:
            raise RelayDisconnectedError("no active relay client")
        queue = self._bulk if bulk else self._control
        await queue.put(_ServerOutbound(target, message))

    async def result(
        self,
        request_id: int,
        metadata: Mapping[str, Any] | None = None,
        data: bytes | bytearray | memoryview | None = None,
        *,
        identity: bytes | None = None,
        bulk: bool = False,
    ) -> None:
        await self.send(
            Message(RESULT, request_id, metadata or {}, None if data is None else bytes(data)),
            identity=identity,
            bulk=bulk,
        )

    async def error(
        self,
        request_id: int,
        error: BaseException | str,
        *,
        identity: bytes | None = None,
        error_type: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        await self.send(
            Message(
                ERROR,
                request_id,
                error_metadata(error, error_type=error_type, details=details),
            ),
            identity=identity,
        )

    async def event(
        self,
        operation: str,
        metadata: Mapping[str, Any] | None = None,
        data: bytes | bytearray | memoryview | None = None,
        *,
        bulk: bool = False,
    ) -> None:
        await self.send(
            Message(
                EVENT,
                0,
                _operation_metadata(operation, metadata),
                None if data is None else bytes(data),
            ),
            bulk=bulk,
        )

    async def callback(
        self,
        operation: str,
        metadata: Mapping[str, Any] | None = None,
        data: bytes | bytearray | memoryview | None = None,
        *,
        timeout: float | None = None,
    ) -> Message:
        if not self.connected:
            raise RelayDisconnectedError("no active relay client")
        callback_id = self._allocate_id()
        future: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
        self._pending[callback_id] = future
        await self.send(
            Message(
                CALLBACK,
                callback_id,
                _operation_metadata(operation, metadata),
                None if data is None else bytes(data),
            )
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(future),
                self.callback_timeout if timeout is None else timeout,
            )
        except asyncio.TimeoutError as exc:
            if self.connected:
                await self.send(Message(CANCEL, callback_id, {}))
            raise RelayTimeoutError("relay callback timed out") from exc
        except asyncio.CancelledError:
            if self.connected:
                await self.send(Message(CANCEL, callback_id, {}))
            raise
        finally:
            self._pending.pop(callback_id, None)
            if not future.done():
                future.cancel()

    async def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._disconnect_active("relay server closed")
        current = asyncio.current_task()
        tasks = [task for task in (self._socket_task, self._monitor_task) if task is not current]
        await _cancel_tasks(*tasks)
        self._socket.close(0)

    async def wait_closed(self) -> None:
        await self._closed.wait()

    async def _socket_loop(self) -> None:
        receive = asyncio.ensure_future(self._socket.recv_multipart())
        control = asyncio.create_task(self._control.get())
        bulk = asyncio.create_task(self._bulk.get())
        try:
            while not self._closed.is_set():
                done, _ = await asyncio.wait(
                    (receive, control, bulk), return_when=asyncio.FIRST_COMPLETED
                )
                if receive in done:
                    frames = receive.result()
                    receive = asyncio.ensure_future(
                        self._socket.recv_multipart()
                    )
                    if len(frames) >= 2:
                        identity = bytes(frames[0])
                        try:
                            message = decode_message(frames[1:])
                        except ProtocolError:
                            continue
                        await self._receive(identity, message)
                if control in done:
                    outbound = control.result()
                    await self._socket.send_multipart(
                        [outbound.identity, *encode(outbound.message)]
                    )
                    while not self._control.empty():
                        outbound = self._control.get_nowait()
                        await self._socket.send_multipart(
                            [outbound.identity, *encode(outbound.message)]
                        )
                    control = asyncio.create_task(self._control.get())
                if bulk in done:
                    outbound = bulk.result()
                    await self._socket.send_multipart(
                        [outbound.identity, *encode(outbound.message)]
                    )
                    bulk = asyncio.create_task(self._bulk.get())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            if not self._closed.is_set():
                self._disconnect_active("relay server connection failed: %s" % (exc,))
                self._closed.set()
        finally:
            await _cancel_tasks(receive, control, bulk)

    async def _receive(self, identity: bytes, message: Message) -> None:
        if message.kind == HELLO:
            await self._hello(identity, message)
            return
        if identity != self._active_identity:
            await self._queue_direct(
                identity,
                Message(BUSY, message.id, {"message": "relay busy"}),
            )
            return

        self._last_received = asyncio.get_running_loop().time()
        if message.kind == HEARTBEAT:
            await self.send(Message(HEARTBEAT_ACK, 0, {}))
            return
        if message.kind in (CALLBACK_RESULT, ERROR, BUSY, RESULT):
            future = self._pending.get(message.id)
            if future is not None and not future.done():
                if message.kind == ERROR:
                    future.set_exception(RemoteRelayError.from_message(message))
                elif message.kind == BUSY:
                    future.set_exception(RelayBusyError("remote client busy"))
                else:
                    future.set_result(message)
            return
        await self._events.put(ServerMessage(identity, message))

    async def _hello(self, identity: bytes, message: Message) -> None:
        requested = (message.metadata or {}).get("service")
        if requested != self.service:
            await self._queue_direct(
                identity,
                Message(
                    ERROR,
                    message.id,
                    error_metadata(
                        "relay provides %s, not %r" % (self.service, requested),
                        error_type="ServiceMismatch",
                    ),
                ),
            )
            return
        if self._active_identity is not None and identity != self._active_identity:
            await self._queue_direct(
                identity,
                Message(BUSY, message.id, {"message": "relay busy"}),
            )
            return
        self._active_identity = identity
        self._last_received = asyncio.get_running_loop().time()
        await self.send(Message(RESULT, message.id, {"service": self.service}))

    async def _queue_direct(self, identity: bytes, message: Message) -> None:
        await self._control.put(_ServerOutbound(identity, message))

    async def _monitor_loop(self) -> None:
        try:
            while not self._closed.is_set():
                await asyncio.sleep(self.heartbeat_interval)
                if self._active_identity is None:
                    continue
                if (
                    asyncio.get_running_loop().time() - self._last_received
                    >= self.connection_timeout
                ):
                    self._disconnect_active("relay client heartbeat timed out")
        except asyncio.CancelledError:
            raise

    def _disconnect_active(self, text: str) -> None:
        identity = self._active_identity
        if identity is None:
            return
        reason = RelayDisconnectedError(text)
        self._active_identity = None
        self._last_received = 0.0
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(reason)
        self._events.put_nowait(ClientDisconnected(identity, reason))


__all__ = (
    "DEFAULT_BULK_QUEUE_MESSAGES",
    "DEFAULT_CALLBACK_TIMEOUT",
    "DEFAULT_CALL_TIMEOUT",
    "DEFAULT_CONNECTION_TIMEOUT",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "ClientDisconnected",
    "HARDWARE",
    "RelayBusyError",
    "RelayClientConnection",
    "RelayConnectionError",
    "RelayDisconnectedError",
    "RelayServerEndpoint",
    "RelayTimeoutError",
    "RemoteRelayError",
    "ServerEvent",
    "ServerMessage",
    "TRANSPORT",
)
