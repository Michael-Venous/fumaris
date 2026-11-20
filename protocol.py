import json
import mmap
import os
import struct


MAGIC = b"FMIP"
# Envelope (framing) version: magic, this, the message type and the header
# and payload sizes. Unchanged, so builds of any protocol version can read
# each other's messages and report a mismatch.
VERSION = 1
HEADER = struct.Struct("<4sHHIQ")
# The message schema's version: the fields each message carries and what they
# mean. Equal to kProtocolVersion in the bridge (src/protocol.h) and bumped
# with every schema change. The bridge sends its own in READY and checks this
# one in SESSION_BEGIN and SESSION_RESET; a bridge before version 2 sent none.
PROTOCOL_VERSION = 5

SESSION_BEGIN = 1
FRAME = 2
CANCEL = 3
SESSION_END = 4
SESSION_RESET = 5
PREVIEW_RENDER = 6
CACHE_FLUSH = 7
PREVIEW_CACHE_EXPORT = 8
PREVIEW_RELEASE = 9

READY = 101
SESSION_ACCEPTED = 102
FRAME_COMPLETE = 103
SESSION_COMPLETE = 104
CANCELLED = 105
FAILED = 106
PREVIEW_COMPLETE = 107
CACHE_FLUSHED = 108
PREVIEW_CACHE_EXPORTED = 109
PREVIEW_RELEASED = 110


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


class ProtocolMismatchError(ProtocolError):
    """The add-on and the bridge are from builds of different protocol versions."""


def mismatch_message(addon_version, bridge_version):
    # The bridge's protocolMismatchMessage() says the same to older add-ons.
    return (
        "The Fumaris add-on and its simulation program are from different versions "
        f"(add-on protocol {addon_version}, simulation program protocol {bridge_version}): "
        "the installation is incomplete. Close Blender, reinstall Fumaris from its ZIP file, "
        "and start Blender again."
    )


def check_ready(data):
    """READY from the bridge: raise ProtocolMismatchError unless it speaks this
    add-on's protocol version (a bridge before version 2 sent none)."""
    version = data.get("protocol_version", 1) if isinstance(data, dict) else 1
    try:
        # The bridge writes every JSON value as a string (Boost.PropertyTree).
        version = int(version)
    except (TypeError, ValueError):
        version = 0
    if version != PROTOCOL_VERSION:
        raise ProtocolMismatchError(mismatch_message(PROTOCOL_VERSION, version))


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


# Preview images in shared memory (B4; notes/reference/protocol.md). The
# bridge puts a large preview image into a slot of its mapping and names it
# in the reply (preview.shared_name, shared_offset, shared_sequence) instead
# of sending it through the pipe; the reader thread copies it out and writes
# the sequence back, which frees the slot. The control block's layout is
# SharedImageChannel's (src/shared_image.h).
_SHARED_MAGIC = b"FMIS"
_SHARED_CONTROL_BYTES = 4 * 1024   # SharedImageChannel::kControlBytes
_SHARED_CONSUMED = struct.Struct("<Q")
_SHARED_SIZES = struct.Struct("<QQQ")   # slot count, slot size, mapping size

try:
    import _posixshmem
except ImportError:
    _posixshmem = None


def shared_images_supported():
    """Whether this Blender can open the bridge's mappings (sent in the session)."""
    if os.name == "nt":
        return True
    return _posixshmem is not None or os.path.isdir("/dev/shm")


def _open_posix(name):
    if _posixshmem is not None:
        descriptor = _posixshmem.shm_open(name, os.O_RDWR, 0o600)
        unlink = _posixshmem.shm_unlink
    else:
        path = "/dev/shm/" + name.lstrip("/")
        descriptor = os.open(path, os.O_RDWR)
        unlink = lambda _name: os.unlink(path)  # noqa: E731
    try:
        mapping = mmap.mmap(descriptor, os.fstat(descriptor).st_size)
    finally:
        os.close(descriptor)
    try:
        # The bridge's and this mapping keep it; the name is no longer
        # needed, and a bridge killed later leaves nothing behind.
        unlink(name)
    except OSError:
        pass
    return mapping


def _open_windows(name):
    # The control block first: it gives the mapping's size.
    with mmap.mmap(-1, _SHARED_CONTROL_BYTES, tagname=name) as control:
        if control[:4] != _SHARED_MAGIC:
            raise OSError(f"no Fumaris image mapping named {name}")
        _slots, _slot_bytes, total = _SHARED_SIZES.unpack_from(control, 16)
    return mmap.mmap(-1, total, tagname=name)


class SharedImages:
    """The reader thread's view of one bridge's image mapping."""

    def __init__(self):
        self._name = None
        self._mapping = None
        self.failure = None

    def close(self):
        if self._mapping is not None:
            try:
                self._mapping.close()
            except (BufferError, OSError):
                pass
        self._mapping = None
        self._name = None

    def _open(self, name):
        if name == self._name:
            return self._mapping
        self.close()
        mapping = _open_windows(name) if os.name == "nt" else _open_posix(name)
        if mapping[:4] != _SHARED_MAGIC:
            mapping.close()
            raise OSError(f"{name} is not a Fumaris image mapping")
        self._name, self._mapping = name, mapping
        return mapping

    def resolve(self, data, payload):
        """The reply's image: its payload, or the bytes its shared slot holds.
        A slot that cannot be read leaves the frame without an image (its
        preview.type becomes none) rather than failing the preview."""
        preview = data.get("preview") if isinstance(data, dict) else None
        name = preview.get("shared_name") if isinstance(preview, dict) else None
        if not name:
            return payload
        try:
            offset = int(preview["shared_offset"])
            size = int(preview["payload_bytes"])
            sequence = int(preview["shared_sequence"])
            mapping = self._open(name)
            # The bridge's size, not the mapping's: macOS rounds that up to a page.
            total = min(len(mapping), _SHARED_SIZES.unpack_from(mapping, 16)[2])
            if offset < _SHARED_CONTROL_BYTES or offset + size > total:
                raise ValueError(f"image slot {offset}+{size} outside {name}")
            image = mapping[offset:offset + size]
            _SHARED_CONSUMED.pack_into(mapping, 8, sequence)
            return image
        except (OSError, ValueError, KeyError, TypeError) as error:
            self.failure = f"{type(error).__name__}: {error}"
            preview["type"] = "none"
            return b""
