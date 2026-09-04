import json
import struct


MAGIC = b"PFIP"
VERSION = 1
HEADER = struct.Struct("<4sHHIQ")

SESSION_BEGIN = 1
FRAME = 2
CANCEL = 3
SESSION_END = 4
SESSION_RESET = 5
PREVIEW_RENDER = 6

READY = 101
SESSION_ACCEPTED = 102
FRAME_COMPLETE = 103
SESSION_COMPLETE = 104
CANCELLED = 105
FAILED = 106
PREVIEW_COMPLETE = 107


class ProtocolError(RuntimeError):
    pass


def write_message(stream, message_type, data=None, payload=b""):
    header = json.dumps(
        data or {},
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    stream.write(HEADER.pack(
        MAGIC,
        VERSION,
        message_type,
        len(header),
        len(payload),
    ))
    stream.write(header)
    if payload:
        stream.write(payload)
    stream.flush()


def read_message(stream):
    envelope = _read_exactly(stream, HEADER.size)
    magic, version, message_type, header_size, payload_size = HEADER.unpack(envelope)
    if magic != MAGIC:
        raise ProtocolError("Bridge returned an invalid protocol signature")
    if version != VERSION:
        raise ProtocolError(f"Unsupported bridge protocol version {version}")

    try:
        data = json.loads(_read_exactly(stream, header_size))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError(f"Bridge returned invalid JSON: {error}") from error
    return message_type, data, _read_exactly(stream, payload_size)


def _read_exactly(stream, size):
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("Bridge closed the protocol stream")
        chunks.extend(chunk)
    return bytes(chunks)
