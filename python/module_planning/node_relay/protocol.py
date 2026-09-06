"""Wire format shared by the node relay client and server."""

from __future__ import annotations

from dataclasses import dataclass
import json
import struct
from typing import Any, Mapping, Optional, Sequence


TRANSPORT = "transport"
HARDWARE = "hardware"
SERVICE_MODES = frozenset((TRANSPORT, HARDWARE))

HELLO = 1
RESULT = 2
ERROR = 3
BUSY = 4
CALL = 5
CANCEL = 6
CALLBACK = 7
CALLBACK_RESULT = 8
EVENT = 9
HEARTBEAT = 10
HEARTBEAT_ACK = 11

MESSAGE_KINDS = frozenset(
    (
        HELLO,
        RESULT,
        ERROR,
        BUSY,
        CALL,
        CANCEL,
        CALLBACK,
        CALLBACK_RESULT,
        EVENT,
        HEARTBEAT,
        HEARTBEAT_ACK,
    )
)

KIND_NAMES = {
    HELLO: "HELLO",
    RESULT: "RESULT",
    ERROR: "ERROR",
    BUSY: "BUSY",
    CALL: "CALL",
    CANCEL: "CANCEL",
    CALLBACK: "CALLBACK",
    CALLBACK_RESULT: "CALLBACK_RESULT",
    EVENT: "EVENT",
    HEARTBEAT: "HEARTBEAT",
    HEARTBEAT_ACK: "HEARTBEAT_ACK",
}

MAX_MESSAGE_ID = 0xFFFFFFFF
HEADER_SIZE = 5
_HEADER = struct.Struct("!BI")


class ProtocolError(ValueError):
    """A relay message does not conform to the wire format."""


@dataclass(frozen=True)
class Message:
    kind: int
    id: int = 0
    metadata: Mapping[str, Any] | None = None
    data: Optional[bytes] = None

    @property
    def name(self) -> str:
        return KIND_NAMES[self.kind]


def validate_service(service: str) -> str:
    if service not in SERVICE_MODES:
        raise ValueError("unknown relay service: %r" % (service,))
    return service


def encode_message(
    kind: int,
    message_id: int = 0,
    metadata: Mapping[str, Any] | None = None,
    data: bytes | bytearray | memoryview | None = None,
) -> list[bytes]:
    if kind not in MESSAGE_KINDS:
        raise ProtocolError("unknown message kind: %r" % (kind,))
    if not isinstance(message_id, int) or not 0 <= message_id <= MAX_MESSAGE_ID:
        raise ProtocolError("message id must be an unsigned 32-bit integer")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, Mapping):
        raise ProtocolError("message metadata must be an object")
    try:
        metadata_frame = json.dumps(
            metadata, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message metadata is not JSON serializable") from exc

    frames = [_HEADER.pack(kind, message_id), metadata_frame]
    if data is not None:
        try:
            frames.append(bytes(data))
        except (TypeError, ValueError) as exc:
            raise ProtocolError("message data must be bytes-like") from exc
    return frames


def encode(message: Message) -> list[bytes]:
    return encode_message(message.kind, message.id, message.metadata, message.data)


def decode_message(frames: Sequence[bytes | bytearray | memoryview]) -> Message:
    if len(frames) not in (2, 3):
        raise ProtocolError("relay message must contain two or three frames")
    header = bytes(frames[0])
    if len(header) != HEADER_SIZE:
        raise ProtocolError("invalid relay message header length")
    kind, message_id = _HEADER.unpack(header)
    if kind not in MESSAGE_KINDS:
        raise ProtocolError("unknown message kind: %r" % (kind,))
    try:
        metadata = json.loads(bytes(frames[1]).decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid message metadata") from exc
    if not isinstance(metadata, dict):
        raise ProtocolError("message metadata must be an object")
    data = bytes(frames[2]) if len(frames) == 3 else None
    return Message(kind, message_id, metadata, data)


def error_metadata(
    error: BaseException | str,
    *,
    error_type: str | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(error, BaseException):
        message = str(error)
        if error_type is None:
            error_type = type(error).__name__
    else:
        message = str(error)
    result: dict[str, Any] = {
        "type": error_type or "RelayError",
        "message": message,
    }
    if details is not None:
        result["details"] = dict(details)
    return result


__all__ = (
    "BUSY",
    "CALL",
    "CALLBACK",
    "CALLBACK_RESULT",
    "CANCEL",
    "ERROR",
    "EVENT",
    "HARDWARE",
    "HEADER_SIZE",
    "HEARTBEAT",
    "HEARTBEAT_ACK",
    "HELLO",
    "KIND_NAMES",
    "MAX_MESSAGE_ID",
    "MESSAGE_KINDS",
    "Message",
    "ProtocolError",
    "RESULT",
    "SERVICE_MODES",
    "TRANSPORT",
    "decode_message",
    "encode",
    "encode_message",
    "error_metadata",
    "validate_service",
)
