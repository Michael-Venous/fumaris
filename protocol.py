import json
import struct


MAGIC = b"FMIP"
VERSION = 1
HEADER = struct.Struct("<4sHHIQ")

SESSION_BEGIN = 1
FRAME = 2
CANCEL = 3
SESSION_END = 4
SESSION_RESET = 5
PREVIEW_RENDER = 6
CACHE_FLUSH = 7
PREVIEW_CACHE_EXPORT = 8

READY = 101
SESSION_ACCEPTED = 102
FRAME_COMPLETE = 103
SESSION_COMPLETE = 104
CANCELLED = 105
FAILED = 106
PREVIEW_COMPLETE = 107
CACHE_FLUSHED = 108
PREVIEW_CACHE_EXPORTED = 109


# Private key holding a preview request's scene depth array until it is packed.
SCENE_DEPTH_KEY = "_scene_depth"


# Private key holding the scene view transform table (display_lut.py).
DISPLAY_LUT_KEY = "_display_lut"
# Table last packed for the running bridge; the bridge keeps it by key, so it
# is sent once. BridgeWorker.start() resets it for a new bridge process.
_PACKED_DISPLAY_LUT = [None]
# Scene depth last packed, by (view, size, depth serial): the bridge's renderer
# keeps it, so an unchanged depth (e.g. while orbiting) is not sent again.
_PACKED_SCENE_DEPTH = [None]


def reset_packed_display_lut():
    """A new bridge process holds no table or depth yet."""
    _PACKED_DISPLAY_LUT[0] = None
    _PACKED_SCENE_DEPTH[0] = None


def pack_preview_payload(preview, payload):
    """JSON-safe preview request; its scene depth and display table go into payload."""
    preview, depth = split_scene_depth(preview)
    if not preview:
        return preview
    table = preview.get(DISPLAY_LUT_KEY)
    if table is not None:
        preview = {key: value for key, value in preview.items() if key != DISPLAY_LUT_KEY}
    depth_key = None
    if depth is not None and preview.get("depth_serial") is not None:
        depth_key = (preview.get("view_id"), preview.get("width"), preview.get("height"),
                     preview["depth_serial"])
    if depth_key is not None and depth_key == _PACKED_SCENE_DEPTH[0]:
        preview["scene_depth"] = {"reuse": True}
    elif depth is not None:
        preview["scene_depth"] = {"offset": len(payload), "count": int(depth.size)}
        payload.extend(depth.tobytes())
    # A request without depth leaves the bridge unoccluded (far), so the next
    # depth must be sent again.
    _PACKED_SCENE_DEPTH[0] = depth_key
    key = preview.get("display_lut_key")
    if table is not None and key and key != _PACKED_DISPLAY_LUT[0]:
        preview["display_lut"] = {"offset": len(payload), "count": int(table.size)}
        payload.extend(table.astype("<f4").tobytes())
        _PACKED_DISPLAY_LUT[0] = key
    return preview


class ProtocolError(RuntimeError):
    pass


def split_scene_depth(preview):
    """Return a JSON-safe preview request and its scene depth array, if any."""
    depth = preview.get(SCENE_DEPTH_KEY) if preview else None
    if depth is None:
        return preview, None
    return {key: value for key, value in preview.items() if key != SCENE_DEPTH_KEY}, depth


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
    chunk = stream.read(size)
    if len(chunk) == size:
        # Buffered pipes normally satisfy the request without extra copies.
        return chunk
    chunks = bytearray(chunk)
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise EOFError("Bridge closed the protocol stream")
        chunks.extend(chunk)
    return bytes(chunks)
