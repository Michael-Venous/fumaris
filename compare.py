"""Versions: what a domain has saved, listed in the Versions section.

Two kinds of entry share one list:
- VDB versions: a cache slot folder of VDB frames (a bake, or a live preview
  kept with Save Sequence or Save Frame), with the settings that made it.
- Snapshots: viewport images (Save Snapshot), each with the settings of the
  moment it was taken.

Bakes are pinned and listed first; any entry can be pinned or unpinned. The
domain's current slot is where Bake writes: Bake replaces a pinned version or
starts a new one (operators.FUMARIS_OT_bake asks which). The eye shows a VDB
version in the scene; View Changes lists its settings against the current
ones and can load them.
"""
import json
import os
import shutil
import time

import bpy
import mathutils
from bpy.props import BoolProperty, StringProperty
from bpy.types import Operator

from .cache import (
    CacheLock,
    GUARD_FILE,
    SETTINGS_FILE,
    apply_settings_snapshot,
    cache_lock,
    is_protected,
    recover_cache_lock,
    require_unprotected,
    next_slot_name,
    read_settings_snapshot,
    settings_snapshot_data,
    write_settings_snapshot,
)
from .importers import (
    SHOWN_MARKER,
    VOLUME_MARKER,
    _frame_files,
    _frame_number,
    _remove_generated_volume_object,
    delete_generated_data,
    import_sequence,
)
from .runtime import active_job, active_mode
from .utils import cache_slot_directory, output_directory_for_object, peek_output_directory_for_object

# On the volume showing slot B: the name of the domain it is compared for.
COMPARE_MARKER = "fumaris_compare_b"
NOTE_FILE = ".fumaris_version.json"
STATS_FILE = ".fumaris_bake_stats.json"
SNAPSHOT_DIRECTORY = "snapshots"
# Snapshots (Save Snapshot) beside the versions, each PNG with a .json of its settings.
SNAPSHOT_ROOT = ".snapshots"
# Side by side: B sits this many domain widths along X.
SIDE_OFFSET = 1.15
# Background behind the live preview where no scene image is available
# (Blender's default viewport grey).
SNAPSHOT_BACKGROUND = 0.235
# The folder scan behind the list is reused for a moment (every redraw asks);
# a version folder added or removed shows at once (the root's mtime).
_SCAN_SECONDS = 1.0
_scan_cache = {}
_last_diff = {"title": "", "lines": [], "measures": []}


def _prefix(props):
    return props.output_prefix or "fumaris_"


def _domain(context):
    obj = context.object
    if obj and hasattr(obj, "fumaris") and obj.fumaris.smoke_object_type == "domain" and not obj.get(VOLUME_MARKER):
        return obj
    return None


def forget_scan():
    _scan_cache.clear()


def snapshots(directory):
    """Snapshot images of a slot, oldest first."""
    folder = os.path.join(directory, SNAPSHOT_DIRECTORY)
    try:
        names = sorted(name for name in os.listdir(folder) if name.lower().endswith(".png"))
    except OSError:
        return []
    return [os.path.join(folder, name) for name in names]


def read_note(directory):
    try:
        with open(os.path.join(directory, NOTE_FILE), encoding="utf-8") as stream:
            return str(json.load(stream).get("note", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def write_note(directory, note, **extra):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, NOTE_FILE)
    data = {}
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, ValueError):
        pass
    data.update(extra)
    data["note"] = note
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1)


def read_meta(directory):
    try:
        with open(os.path.join(directory, NOTE_FILE), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def read_pinned(directory):
    """Pinned: stored, or a bake (it kept bake statistics) when never set."""
    data = read_meta(directory)
    if "pinned" in data:
        return bool(data["pinned"])
    return os.path.isfile(os.path.join(directory, STATS_FILE))


def write_pinned(directory, pinned):
    write_note(directory, read_note(directory), pinned=bool(pinned))


def write_bake_stats(directory, props):
    frames = _frame_files(directory, _prefix(props))
    data = {
        "frames": len(frames),
        "seconds": round(float(props.bake_elapsed), 2),
        "peak_vram_mb": round(float(props.bake_peak_vram_mb), 1),
        "peak_ram_mb": round(float(props.bake_peak_ram_mb), 1),
        "resolution": int(props.resolution),
        "written": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(directory, STATS_FILE), "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1)


def read_bake_stats(directory):
    try:
        with open(os.path.join(directory, STATS_FILE), encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def frame_measures(directory, prefix, frame):
    """Cheap measures of one frame from the VDB header: smoke voxels, extent, top, file size."""
    files = [path for path in _frame_files(directory, prefix) if _frame_number(path, prefix) == frame]
    if not files:
        return None
    path = files[0]
    out = {"file": os.path.getsize(path)}
    try:
        import openvdb
        grids = {grid.name: grid for grid in openvdb.readAllGridMetadata(path)}
    except Exception:
        return out
    grid = grids.get("density")
    if grid is None:
        return out
    meta = grid.metadata if hasattr(grid, "metadata") else {}
    count = meta.get("file_voxel_count")
    low, high = meta.get("file_bbox_min"), meta.get("file_bbox_max")
    if count is not None:
        out["voxels"] = int(count)
    if low is not None and high is not None and all(h >= l for l, h in zip(low, high)):
        try:
            a = grid.transform.indexToWorld(tuple(float(v) for v in low))
            b = grid.transform.indexToWorld(tuple(float(v) + 1.0 for v in high))
            out["size"] = tuple(abs(y - x) for x, y in zip(a, b))
            out["top"] = max(a[2], b[2])
        except Exception:
            pass
    return out


def measured_differences(domain, frame):
    """Rows (measure, A, B) for the current frame and the two bakes' costs."""
    props = domain.fumaris
    prefix = _prefix(props)
    a_dir = output_directory_for_object(domain)
    b_dir = cache_slot_directory(domain, props.compare_slot)
    rows = []
    a, b = frame_measures(a_dir, prefix, frame), frame_measures(b_dir, prefix, frame)
    if a or b:
        a, b = a or {}, b or {}

        def row(label, key, fmt):
            if key in a or key in b:
                rows.append((f"{label} (frame {frame})",
                             fmt(a[key]) if key in a else "-", fmt(b[key]) if key in b else "-"))
        row("Smoke voxels", "voxels", lambda v: f"{v / 1e6:.2f} M" if v >= 100000 else f"{v:,}")
        row("Extent", "size", lambda v: " x ".join(f"{x:.1f}" for x in v) + " m")
        row("Top", "top", lambda v: f"{v:.2f} m")
        row("File", "file", _size_text)
    sa, sb = read_bake_stats(a_dir), read_bake_stats(b_dir)
    for label, key, fmt in (("Bake time", "seconds", lambda v: f"{v:.0f} s"),
                            ("Time per frame", None, None),
                            ("Peak VRAM", "peak_vram_mb", lambda v: f"{v / 1024:.2f} GB"),
                            ("Peak RAM", "peak_ram_mb", lambda v: f"{v / 1024:.1f} GB")):
        if key is None:
            per = [f"{s['seconds'] / s['frames'] * 1000:.0f} ms" if s.get("frames") and s.get("seconds") else "-"
                   for s in (sa, sb)]
            if per != ["-", "-"]:
                rows.append((label, *per))
            continue
        if key in sa or key in sb:
            rows.append((label, fmt(sa[key]) if key in sa else "-", fmt(sb[key]) if key in sb else "-"))
    return rows


def list_versions(domain, *, fresh=False):
    """Cache slots of this domain that hold anything, current slot always included.

    Drawn every redraw, so the folder scan is reused for a moment.
    """
    current = peek_output_directory_for_object(domain)
    if not current:
        return []
    root = os.path.dirname(current)
    prefix = _prefix(domain.fumaris)
    key = (root, prefix)
    now = time.monotonic()
    try:
        stamp = os.stat(root).st_mtime_ns
    except OSError:
        stamp = 0
    cached = _scan_cache.get(key)
    if cached and not fresh and now - cached[0] < _SCAN_SECONDS and cached[2] == stamp:
        return cached[1]
    try:
        with os.scandir(root) as entries:
            names = sorted(entry.name for entry in entries
                           if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."))
    except OSError:
        names = []
    versions = []
    for name in names:
        path = os.path.join(root, name)
        files = _frame_files(path, prefix)
        size = 0
        modified = 0.0
        for file in files:
            try:
                stat = os.stat(file)
            except OSError:
                continue
            size += stat.st_size
            modified = max(modified, stat.st_mtime)
        images = snapshots(path)
        protected = is_protected(path)
        if not (files or images or protected):
            continue
        versions.append({
            "name": name,
            "path": path,
            "frames": len(files),
            "first": _frame_number(files[0], prefix) if files else None,
            "last": _frame_number(files[-1], prefix) if files else None,
            "bytes": size,
            "modified": modified,
            "snapshots": len(images),
            "protected": protected,
            "note": read_note(path),
            "settings": read_settings_snapshot(path) is not None,
            "pinned": read_pinned(path),
            "kind": "vdb",
        })
    _scan_cache[key] = (now, versions, stamp)
    return versions


def _size_text(size):
    if size >= 1e9:
        return f"{size / 1e9:.1f} GB"
    if size >= 1e6:
        return f"{size / 1e6:.0f} MB"
    return f"{size / 1e3:.0f} kB" if size else "empty"


def version_summary(version, *, size=False):
    if version["frames"] == 1:
        text = f"frame {version['first']}"
    elif version["frames"]:
        text = f"{version['first']}-{version['last']}"
    else:
        text = "no frames"
    return f"{text} · {_size_text(version['bytes'])}" if size else text


# --- The B volume ------------------------------------------------------------

def _current_directory(domain):
    current = peek_output_directory_for_object(domain)
    return os.path.normpath(current) if current else None


def compare_volumes(domain):
    return [obj for obj in bpy.data.objects if obj.get(COMPARE_MARKER) == domain.name]


def _slot_volumes(directory):
    directory = os.path.normpath(directory)
    return [obj for obj in bpy.data.objects
            if obj.get(VOLUME_MARKER) and os.path.normpath(obj[VOLUME_MARKER]) == directory]


def _x_extent(obj, depsgraph):
    try:
        evaluated = obj.evaluated_get(depsgraph)
        xs = [(evaluated.matrix_world @ mathutils.Vector(corner)).x for corner in evaluated.bound_box]
    except (ReferenceError, RuntimeError):
        return 0.0
    return max(xs) - min(xs)


def _side_offset(domain, volumes):
    """Clear of A: the widest of the domain and the bakes, in X."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    width = max((_x_extent(obj, depsgraph) for obj in (domain, *volumes)), default=0.0)
    return (width if width > 1e-6 else 2.0) * SIDE_OFFSET


def _set_shown(obj, shown):
    hidden = not shown
    if obj.hide_viewport != hidden or obj.hide_render != hidden:
        obj.hide_viewport = hidden
        obj.hide_render = hidden
        obj.update_tag()


def apply_compare_view(domain):
    """Show A, B or both (B beside the domain) as Compare's Show asks."""
    volumes = compare_volumes(domain)
    current = _current_directory(domain)
    if not volumes or not current:
        return
    view = domain.fumaris.compare_view
    for obj in _slot_volumes(current):
        _set_shown(obj, view != "B")
    offset = _side_offset(domain, [*volumes, *_slot_volumes(current)]) if view == "SIDE" else 0.0
    for obj in volumes:
        _set_shown(obj, view != "A")
        if tuple(obj.location) != (offset, 0.0, 0.0):
            obj.location = (offset, 0.0, 0.0)


def clear_compare(domain):
    """Stop comparing: B's volume goes back in place and hides (its files stay)."""
    current = _current_directory(domain)
    for obj in compare_volumes(domain):
        del obj[COMPARE_MARKER]
        obj.location = (0.0, 0.0, 0.0)
        marker = obj.get(VOLUME_MARKER)
        _set_shown(obj, bool(marker and current and os.path.normpath(marker) == current))
        if obj.name.endswith(" (B)"):
            obj.name = obj.name[:-4]
    if current:
        for obj in _slot_volumes(current):
            _set_shown(obj, True)


def after_slot_change(domain):
    """Cache Slot changed: keep comparing unless B just became the current slot."""
    if not compare_volumes(domain):
        return
    props = domain.fumaris
    current = _current_directory(domain)
    if current and os.path.basename(current) == props.compare_slot:
        clear_compare(domain)
        props.compare_slot = ""
    else:
        apply_compare_view(domain)


def load_compare(domain, slot):
    """Show cache slot `slot` as B. Returns the volume object."""
    props = domain.fumaris
    directory = os.path.normpath(cache_slot_directory(domain, slot))
    if directory == _current_directory(domain):
        raise RuntimeError("B must be a different cache slot than the current one")
    prefix = _prefix(props)
    files = _frame_files(directory, prefix)
    if not files:
        raise RuntimeError(f"Cache slot '{slot}' has no baked frames")
    clear_compare(domain)
    volumes = _slot_volumes(directory)
    volume = volumes[0] if volumes else None
    if volume is None:
        volume = import_sequence(
            directory,
            _frame_number(files[0], prefix),
            prefix,
            material=props.volume_material,
            selectable=props.volume_selectable,
            appearance=props,
        )
        volume.name = f"{domain.name} {slot}"
    volume[COMPARE_MARKER] = domain.name
    if not volume.name.endswith(" (B)"):
        volume.name = f"{volume.name} (B)"
    props.compare_slot = os.path.basename(directory)
    apply_compare_view(domain)
    return volume


def show_slot(context, domain):
    """Import the current slot's bake if no volume shows it yet."""
    props = domain.fumaris
    directory = output_directory_for_object(domain)
    if not _frame_files(directory, _prefix(props)) or _slot_volumes(directory):
        return None
    from .operators import _import_cache
    first = _frame_number(_frame_files(directory, _prefix(props))[0], _prefix(props))
    return _import_cache(context, domain, directory, _prefix(props), frame_start=first)


# --- Settings differences ----------------------------------------------------

def _format(value):
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, (list, tuple)):
        return "(" + ", ".join(_format(item) for item in value) + ")"
    return str(value)


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)), abs(float(b)))
        except (TypeError, ValueError):
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _label(props, key):
    prop = props.bl_rna.properties.get(key) if props is not None else None
    return prop.name if prop is not None and prop.name else key


def settings_differences(a, b, props=None):
    """Lines 'Setting: a -> b' between two settings snapshots."""
    lines = []
    sa, sb = a.get("settings", {}), b.get("settings", {})
    for key in sorted(set(sa) | set(sb)):
        if key.startswith(("show_", "compare_")):
            continue
        if not _same(sa.get(key), sb.get(key)):
            lines.append((_label(props, key), _format(sa.get(key, "-")), _format(sb.get(key, "-"))))
    if a.get("detail_layers", []) != b.get("detail_layers", []):
        lines.append(("Detail & Shaping layers",
                      f"{len(a.get('detail_layers', []))} layers", f"{len(b.get('detail_layers', []))} layers"))
    pa, pb = a.get("participants", {}), b.get("participants", {})
    for name in sorted(set(pa) | set(pb)):
        if name not in pa or name not in pb:
            lines.append((name, "present" if name in pa else "-", "present" if name in pb else "-"))
            continue
        source = bpy.data.objects.get(name)
        source_props = getattr(source, "fumaris", None)
        for key in sorted(set(pa[name]) | set(pb[name])):
            if key.startswith(("show_", "compare_")):
                continue
            if not _same(pa[name].get(key), pb[name].get(key)):
                lines.append((f"{name}: {_label(source_props, key)}",
                              _format(pa[name].get(key, "-")), _format(pb[name].get(key, "-"))))
    return lines


def _current_settings(domain):
    from .cache import _rna_values
    from . import detail_stack
    participants = {}
    for obj in bpy.context.scene.objects:
        props = getattr(obj, "fumaris", None)
        if obj != domain and props is not None and props.smoke_object_type not in ("none", "domain"):
            participants[obj.name] = _rna_values(props)
    return {
        "settings": _rna_values(domain.fumaris),
        "detail_layers": detail_stack.to_list(domain.fumaris),
        "participants": participants,
    }


# --- New versions ------------------------------------------------------------

def free_slot_name(base, taken):
    """base, then base_2, base_3, ..."""
    if not taken(base):
        return base
    number = 2
    while taken(f"{base}_{number}"):
        number += 1
    return f"{base}_{number}"


def validate_prefix(prefix):
    """A file name prefix, never a path (the bridge checks the same)."""
    if "/" in prefix or "\\" in prefix or prefix in {".", ".."} or "\0" in prefix:
        raise RuntimeError(f"Output prefix '{prefix}' cannot contain a path: use a plain name")


def _new_version(domain, base):
    """A fresh version folder, locked as a bake's is (returns slot, directory,
    lock). A folder another Blender holds is passed over, so two saves never
    share one."""
    prefix = _prefix(domain.fumaris)
    validate_prefix(prefix)
    locks = {}

    def taken(slot):
        directory = cache_slot_directory(domain, slot)
        if is_protected(directory) or _frame_files(directory, prefix) or \
                os.path.isfile(os.path.join(directory, NOTE_FILE)):
            return True
        lock = CacheLock(directory)
        try:
            lock.acquire()
        except RuntimeError:
            return True
        locks[slot] = lock
        return False

    slot = free_slot_name(base, taken)
    directory = cache_slot_directory(domain, slot)
    return slot, directory, locks[slot]


def _participants_for_snapshot(domain):
    job = active_job()
    if job is not None and getattr(job, "_domain_name", None) == domain.name:
        return getattr(job, "_participants", ())
    return [obj for obj in bpy.context.scene.objects
            if getattr(obj, "fumaris", None) is not None and obj != domain
            and obj.fumaris.smoke_object_type not in ("none", "domain")]


# --- Snapshots ---------------------------------------------------------------

def _view3d_regions(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    yield window, area, region


def _scene_image(context, region, area):
    """The viewport as it draws the scene (no add-on overlays), bottom row first."""
    import gpu
    import numpy as np
    space = area.spaces.active
    offscreen = gpu.types.GPUOffScreen(region.width, region.height)
    try:
        offscreen.draw_view3d(
            context.scene, context.view_layer, space, region,
            space.region_3d.view_matrix, space.region_3d.window_matrix,
            do_color_management=True,
        )
        pixels = np.asarray(offscreen.texture_color.read(), dtype=np.float32)
    finally:
        offscreen.free()
    pixels = pixels.reshape(region.height, region.width, 4)
    if pixels.max() > 1.5:
        pixels = pixels / 255.0
    pixels[..., 3] = 1.0
    return pixels


def _preview_image(domain, view_id=None):
    """The live preview of this domain (premultiplied, bottom row first) and its view."""
    import numpy as np
    from . import preview
    for (name, region_id), item in preview._PREVIEWS.items():
        if name != domain.name or item.get("type") != "volume":
            continue
        if view_id is not None and region_id != view_id:
            continue
        width, height = item["width"], item["height"]
        if item.get("pixels") is not None:
            pixels = np.frombuffer(item["pixels"], dtype=np.uint8).astype(np.float32) / 255.0
        elif item.get("texture") is not None:
            pixels = np.asarray(item["texture"].read(), dtype=np.float32)
            if pixels.max() > 1.5:
                pixels = pixels / 255.0
        else:
            continue
        # The preview image is top row first.
        return pixels.reshape(height, width, 4)[::-1], region_id
    return None, None


def capture_snapshot(context, domain):
    """What the viewport shows of this domain, as an RGBA float array (bottom row first)."""
    import numpy as np
    regions = list(_view3d_regions(context))
    smoke, view_id = _preview_image(domain)
    chosen = None
    if view_id is not None:
        chosen = next((item for item in regions if item[2].as_pointer() == view_id), None)
    if chosen is None and regions:
        chosen = max(regions, key=lambda item: item[2].width * item[2].height)
    scene = None
    if chosen is not None:
        try:
            scene = _scene_image(context, chosen[2], chosen[1])
        except Exception as error:
            print(f"Fumaris snapshot: viewport image unavailable: {error}")
    if smoke is None:
        if scene is None:
            raise RuntimeError("No 3D Viewport to take a snapshot of")
        return scene
    if scene is None:
        scene = np.full(smoke.shape, SNAPSHOT_BACKGROUND, dtype=np.float32)
        scene[..., 3] = 1.0
    # The preview may be drawn at a lower resolution than the viewport.
    rows = ((np.arange(scene.shape[0]) + 0.5) * smoke.shape[0] / scene.shape[0]).astype(np.intp)
    columns = ((np.arange(scene.shape[1]) + 0.5) * smoke.shape[1] / scene.shape[1]).astype(np.intp)
    smoke = smoke[rows[:, None], columns[None, :]]
    # Blended in linear light like the viewport (its overlay buffer is sRGB):
    # both images are display-encoded, the preview premultiplied.
    alpha = smoke[..., 3:4]
    straight = np.divide(smoke[..., :3], alpha, out=np.zeros_like(smoke[..., :3]), where=alpha > 0.0)
    linear = _srgb_to_linear(np.clip(straight, 0.0, 1.0)) * alpha + (1.0 - alpha) * _srgb_to_linear(scene[..., :3])
    out = scene.copy()
    out[..., :3] = _linear_to_srgb(linear)
    out[..., 3] = 1.0
    return np.clip(out, 0.0, 1.0)


def _srgb_to_linear(values):
    import numpy as np
    return np.where(values <= 0.04045, values / 12.92, np.power((np.clip(values, 0.0, None) + 0.055) / 1.055, 2.4))


def _linear_to_srgb(values):
    import numpy as np
    return np.where(values <= 0.0031308, values * 12.92, 1.055 * np.power(np.clip(values, 0.0, None), 1.0 / 2.4) - 0.055)


def save_png(pixels, path):
    height, width = pixels.shape[:2]
    image = bpy.data.images.new("Fumaris Snapshot", width, height, alpha=False)
    try:
        image.pixels.foreach_set(pixels.astype("float32").ravel())
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
    finally:
        bpy.data.images.remove(image)


def load_png(path):
    import numpy as np
    image = bpy.data.images.load(path, check_existing=False)
    try:
        width, height = image.size
        pixels = np.empty(width * height * 4, dtype=np.float32)
        image.pixels.foreach_get(pixels)
    finally:
        bpy.data.images.remove(image)
    return pixels.reshape(height, width, 4)


def side_by_side(left, right, gap=6):
    import numpy as np
    height = max(left.shape[0], right.shape[0])
    width = left.shape[1] + gap + right.shape[1]
    out = np.zeros((height, width, 4), dtype=np.float32)
    out[..., 3] = 1.0
    # Top aligned (rows run bottom to top).
    out[height - left.shape[0]:, :left.shape[1]] = left
    out[height - right.shape[0]:, left.shape[1] + gap:] = right
    return out


def show_image(context, image):
    """Show `image` in an Image Editor, opening Blender's image window if none is open."""
    def find():
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "IMAGE_EDITOR":
                    area.spaces.active.image = image
                    area.tag_redraw()
                    return True
        return False
    if find():
        return True
    try:
        bpy.ops.render.view_show("INVOKE_DEFAULT")
    except RuntimeError:
        return False
    return find()


# --- Entries: VDB versions and snapshots --------------------------------------

def snapshot_root(domain, *, create=False):
    current = output_directory_for_object(domain) if create else peek_output_directory_for_object(domain)
    if not current:
        return None
    path = os.path.join(os.path.dirname(current), SNAPSHOT_ROOT)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def _sidecar(path):
    return path + ".json"


def read_sidecar(path):
    try:
        with open(_sidecar(path), encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_sidecar(path, **values):
    data = read_sidecar(path)
    data.update(values)
    with open(_sidecar(path), "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1, sort_keys=True)


def _image_frame(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    tail = stem.rsplit("_f", 1)
    return int(tail[1]) if len(tail) == 2 and tail[1].isdigit() else None


def list_entries(domain, *, fresh=False):
    """VDB versions and snapshots, pinned first, then newest first."""
    entries = []
    for version in list_versions(domain, fresh=fresh):
        if version["frames"] or version["protected"]:
            entries.append(dict(version, key="vdb:" + version["name"]))
    images = []
    root = snapshot_root(domain)
    if root:
        try:
            names = sorted(name for name in os.listdir(root) if name.lower().endswith(".png"))
        except OSError:
            names = []
        images.extend((os.path.join(root, name), None) for name in names)
    # Snapshots kept in a version's folder before 2.0 beta: that version's settings.
    for version in list_versions(domain, fresh=False):
        if version["snapshots"]:
            images.extend((path, version["path"]) for path in snapshots(version["path"]))
    for path, owner in images:
        meta = read_sidecar(path)
        try:
            modified = os.path.getmtime(path)
        except OSError:
            continue
        frame = _image_frame(path)
        entries.append({
            "key": "img:" + path,
            "kind": "image",
            "name": meta.get("note") or (f"Snapshot · frame {frame}" if frame is not None else "Snapshot"),
            "note": meta.get("note", ""),
            "path": path,
            "owner": owner,
            "frame": frame,
            "modified": modified,
            "pinned": bool(meta.get("pinned", False)),
            "settings": bool(meta.get("settings")) or bool(owner and read_settings_snapshot(owner) is not None),
            "protected": False,
        })
    entries.sort(key=lambda entry: (not entry["pinned"], -entry["modified"]))
    return entries


def find_entry(domain, key, *, fresh=False):
    return next((entry for entry in list_entries(domain, fresh=fresh) if entry["key"] == key), None)


def entry_settings(entry):
    """The settings snapshot an entry was made with (None if it has none)."""
    if entry["kind"] == "vdb":
        return read_settings_snapshot(entry["path"])
    meta = read_sidecar(entry["path"])
    if meta.get("settings"):
        return meta
    return read_settings_snapshot(entry["owner"]) if entry.get("owner") else None


def entry_summary(entry):
    if entry["kind"] == "image":
        return "image"
    return version_summary(entry)


def selected_entry(domain):
    key = domain.fumaris.compare_selected
    return find_entry(domain, key) if key else None


def _entry_volumes(entry):
    return _slot_volumes(entry["path"]) if entry["kind"] == "vdb" else []


def entry_shown(domain, entry):
    return any(not obj.hide_viewport for obj in _entry_volumes(entry))


def show_entry(context, domain, entry, shown):
    """A VDB version in the scene (imported on first show) or hidden."""
    props = domain.fumaris
    volumes = _entry_volumes(entry)
    current = _current_directory(domain)
    if shown and not volumes:
        prefix = _prefix(props)
        files = _frame_files(entry["path"], prefix)
        if not files:
            raise RuntimeError(f"'{entry['name']}' has no frames")
        volume = import_sequence(entry["path"], _frame_number(files[0], prefix), prefix,
                                 material=props.volume_material, selectable=props.volume_selectable,
                                 appearance=props)
        volume.name = f"{domain.name} {entry['name']}"
        volumes = [volume]
    for obj in volumes:
        if os.path.normpath(entry["path"]) != current:
            obj[SHOWN_MARKER] = True
        _set_shown(obj, shown)


def remove_entry_from_scene(entry):
    for obj in list(_entry_volumes(entry)):
        _remove_generated_volume_object(obj)


def delete_entry(domain, entry):
    if entry["kind"] == "vdb":
        delete_version(domain, entry["name"])
    else:
        for path in (entry["path"], _sidecar(entry["path"])):
            try:
                os.remove(path)
            except OSError:
                pass
    if domain.fumaris.compare_selected == entry["key"]:
        domain.fumaris.compare_selected = ""
    forget_scan()


def changes_from_current(domain, entry):
    """Lines (setting, current, entry's) where the entry differs from now."""
    data = entry_settings(entry)
    if data is None:
        return None
    return settings_differences(_current_settings(domain), data, domain.fumaris)


def delete_version(domain, name):
    """Delete a VDB version's frames, images, settings and note (a protected one is refused)."""
    props = domain.fumaris
    directory = cache_slot_directory(domain, name)
    require_unprotected(directory, "delete it")
    if props.compare_slot == name:
        clear_compare(domain)
        props.compare_slot = ""
    recover_cache_lock(directory, owner_pid=os.getpid())
    with cache_lock(directory):
        delete_generated_data(directory, _prefix(props))
        shutil.rmtree(os.path.join(directory, SNAPSHOT_DIRECTORY), ignore_errors=True)
        for file in (SETTINGS_FILE, NOTE_FILE, STATS_FILE):
            try:
                os.remove(os.path.join(directory, file))
            except OSError:
                pass
    # The lock's guard file outlives the lock on purpose (contenders lock the
    # same file); with the version gone it has nothing left to guard.
    try:
        os.remove(os.path.join(directory, GUARD_FILE))
    except OSError:
        pass
    try:
        os.rmdir(directory)
    except OSError:
        pass
    if name == props.cache_slot:
        props.simulation_state = "idle"
        props.baked_frames = 0
        props.bake_elapsed = 0.0
        props.bake_peak_vram_mb = 0.0
        props.bake_peak_ram_mb = 0.0
    forget_scan()


def bake_target(domain):
    """The pinned VDB version Bake would replace (None: there is none).
    The selected one if pinned, else the current slot if pinned, else the newest pinned."""
    pinned = [entry for entry in list_entries(domain, fresh=True) if entry["kind"] == "vdb" and entry["pinned"]]
    if not pinned:
        return None
    selected = domain.fumaris.compare_selected
    current = os.path.basename(output_directory_for_object(domain))
    return (next((e for e in pinned if e["key"] == selected), None)
            or next((e for e in pinned if e["name"] == current), None)
            or pinned[0])


def new_bake_slot(domain):
    prefix = _prefix(domain.fumaris)

    def taken(slot):
        directory = cache_slot_directory(domain, slot)
        return bool(is_protected(directory) or _frame_files(directory, prefix)
                    or os.path.isfile(os.path.join(directory, NOTE_FILE)))
    return free_slot_name("bake", taken)


# --- Operators ---------------------------------------------------------------

class _DomainOperator:
    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_mode() != "baking"


def _entry_or_report(op, context):
    domain = _domain(context)
    entry = find_entry(domain, op.key, fresh=True) if domain else None
    if entry is None:
        op.report({"ERROR"}, "That version no longer exists")
    return domain, entry


class FUMARIS_OT_entry_select(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_select"
    bl_label = "Select"
    bl_description = "Select this version; its buttons appear below the list. Right-click for more"

    key: StringProperty()

    @classmethod
    def description(cls, context, properties):
        domain = _domain(context)
        entry = find_entry(domain, properties.key) if domain else None
        if entry is None:
            return cls.bl_description
        parts = [entry["name"]]
        if entry["kind"] == "vdb":
            parts.append(version_summary(entry, size=True))
        if entry["modified"]:
            parts.append("Saved " + time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["modified"])))
        if entry["pinned"]:
            parts.append("Pinned" + (": Bake can replace it" if entry["kind"] == "vdb" else ""))
        if not entry["settings"]:
            parts.append("No stored settings")
        return ".\n".join(parts) + ".\nClick to select. Right-click for more"

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        _domain(context).fumaris.compare_selected = self.key
        return {"FINISHED"}


class FUMARIS_OT_entry_pin(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_pin"
    bl_label = "Pin"
    bl_description = "Pin or unpin. Pinned versions stay at the top; Bake offers to replace a pinned VDB version (a bake, or a preview sequence you want to keep as one)"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        domain, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        if entry["kind"] == "vdb":
            write_pinned(entry["path"], not entry["pinned"])
        else:
            write_sidecar(entry["path"], pinned=not entry["pinned"])
        forget_scan()
        return {"FINISHED"}


class FUMARIS_OT_entry_show(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_show"
    bl_label = "Show in Viewport"
    bl_description = "Show or hide this version in the viewport (imported as a volume the first time)"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_mode() != "baking"

    def execute(self, context):
        domain, entry = _entry_or_report(self, context)
        if entry is None or entry["kind"] != "vdb":
            return {"CANCELLED"}
        try:
            show_entry(context, domain, entry, not entry_shown(domain, entry))
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class FUMARIS_OT_entry_import(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_import"
    bl_label = "Import"
    bl_description = "Bring this version into the scene as a volume object, to render (EEVEE or Cycles) and keep; Remove takes it out of the scene again (its files stay)"

    key: StringProperty()

    def execute(self, context):
        domain, entry = _entry_or_report(self, context)
        if entry is None or entry["kind"] != "vdb":
            return {"CANCELLED"}
        if _entry_volumes(entry):
            # The bake the domain shows stays: removing it by mistake would
            # leave nothing to render. Its eye hides it; Delete is in the menu.
            if os.path.normpath(entry["path"]) == _current_directory(domain):
                self.report({"ERROR"}, "This is the bake the domain shows; hide it with its eye instead")
                return {"CANCELLED"}
            remove_entry_from_scene(entry)
            return {"FINISHED"}
        try:
            show_entry(context, domain, entry, True)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class FUMARIS_OT_entry_image(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_image"
    bl_label = "Preview"
    bl_description = "Open this snapshot in an Image Editor"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        domain, entry = _entry_or_report(self, context)
        if entry is None or entry["kind"] != "image":
            return {"CANCELLED"}
        path = os.path.normpath(entry["path"])
        image = next((image for image in bpy.data.images
                      if os.path.normpath(bpy.path.abspath(image.filepath)) == path), None)
        if image is None:
            image = bpy.data.images.load(path, check_existing=True)
        else:
            image.reload()
        if not show_image(context, image):
            self.report({"INFO"}, f"Image '{image.name}' ({path})")
        return {"FINISHED"}


_changes = {"title": "", "lines": [], "key": ""}


class FUMARIS_OT_entry_changes(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_changes"
    bl_label = "View Changes"
    bl_description = "The settings this version was made with that differ from the current ones; Load Settings from there"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def invoke(self, context, _event):
        domain, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        lines = changes_from_current(domain, entry)
        if lines is None:
            self.report({"ERROR"}, f"'{entry['name']}' has no stored settings")
            return {"CANCELLED"}
        _changes.update(title=f"{entry['name']}: changes from the current settings", lines=lines, key=entry["key"])
        return context.window_manager.invoke_popup(self, width=520)

    def draw(self, _context):
        layout = self.layout
        layout.label(text=_changes["title"], icon="SEQ_SPLITVIEW")
        lines = _changes["lines"]
        if not lines:
            layout.label(text="Same settings as now")
        else:
            grid = layout.grid_flow(row_major=True, columns=3, even_columns=False, align=True)
            grid.label(text="Setting")
            grid.label(text="Now")
            grid.label(text="This version")
            for name, now, then in lines[:60]:
                grid.label(text=name)
                grid.label(text=now)
                grid.label(text=then)
            if len(lines) > 60:
                layout.label(text=f"… and {len(lines) - 60} more")
        layout.separator()
        load = layout.row()
        load.enabled = active_job() is None or active_mode() == "previewing"
        load.operator("fumaris.entry_load", text="Load These Settings", icon="LOOP_BACK").key = _changes["key"]

    def execute(self, _context):
        return {"FINISHED"}


class FUMARIS_OT_entry_load(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_load"
    bl_label = "Load Settings"
    bl_description = "Set the domain and its sources to the settings this version was made with (sources are matched by name). Your current settings are overwritten"
    bl_options = {"REGISTER", "UNDO"}

    key: StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(
            self, event, title="Load Settings?",
            message="This replaces the current settings of the domain and its sources. Undo (Ctrl+Z) brings them back.",
            confirm_text="Load", icon="WARNING")

    def execute(self, context):
        domain, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        data = entry_settings(entry)
        if data is None:
            self.report({"ERROR"}, f"'{entry['name']}' has no stored settings")
            return {"CANCELLED"}
        changed, _restored, missing = apply_settings_snapshot(domain, data, bpy.data.objects)
        message = f"Loaded {changed} settings from '{entry['name']}'"
        if missing:
            self.report({"WARNING"}, message + f"; not found: {', '.join(missing[:4])}")
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class FUMARIS_OT_entry_save_preset(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_save_preset"
    bl_label = "Save Settings as Preset"
    bl_description = "Save the domain settings this version was made with as a preset, available in every file"

    key: StringProperty()
    preset: StringProperty(name="Preset Name")

    def invoke(self, context, _event):
        _domain_, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        self.preset = entry["name"]
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        from .presets import save_preset_data
        _domain_, entry = _entry_or_report(self, context)
        data = entry_settings(entry) if entry else None
        if data is None:
            self.report({"ERROR"}, "This version has no stored settings")
            return {"CANCELLED"}
        _path, count = save_preset_data(data, self.preset or entry["name"])
        self.report({"INFO"}, f"Saved preset '{self.preset}' ({count} settings)")
        return {"FINISHED"}


class FUMARIS_OT_entry_note(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_note"
    bl_label = "Rename…"
    bl_description = "Name or describe this version (its files keep their names)"

    key: StringProperty()
    note: StringProperty(name="Name")

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def invoke(self, context, _event):
        _domain_, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        self.note = entry["note"]
        return context.window_manager.invoke_props_dialog(self, title="Rename")

    def execute(self, context):
        _domain_, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        if entry["kind"] == "vdb":
            write_note(entry["path"], self.note)
        else:
            write_sidecar(entry["path"], note=self.note)
        forget_scan()
        return {"FINISHED"}


class FUMARIS_OT_entry_delete(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_delete"
    bl_label = "Delete"
    bl_description = "Delete this version's files (frames, or the image) and stored settings. They cannot be recovered"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_job() is None

    def invoke(self, context, event):
        _domain_, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        return context.window_manager.invoke_confirm(
            self, event, title="Delete?",
            message=f"Delete '{entry['name']}' ({entry_summary(entry)})? The files cannot be recovered.",
            confirm_text="Delete", icon="WARNING")

    def execute(self, context):
        domain, entry = _entry_or_report(self, context)
        if entry is None:
            return {"CANCELLED"}
        try:
            delete_entry(domain, entry)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Deleted '{entry['name']}'")
        return {"FINISHED"}


class FUMARIS_OT_entry_menu(_DomainOperator, Operator):
    bl_idname = "fumaris.entry_menu"
    bl_label = "More"
    bl_description = "Rename, save its settings as a preset, or delete"

    key: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def invoke(self, context, _event):
        key = self.key

        def draw(menu, menu_context):
            draw_entry_menu(menu.layout, _domain(menu_context), key)
        context.window_manager.popup_menu(draw, title="Version")
        return {"FINISHED"}

    def execute(self, _context):
        return {"FINISHED"}


# --- Saving the live preview ---------------------------------------------------

def preview_cache(job=None):
    """The live preview's cache stats ({} without a preview or cache)."""
    job = job if job is not None else active_job()
    if job is None or active_mode() != "previewing":
        return {}
    cache = getattr(job, "_preview_cache", None) or {}
    out = {}
    for key, value in cache.items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        out[key] = int(number) if number.is_integer() else number
    return out


def start_cache_export(context, domain, base, first=None, last=None):
    """Write the live preview's cache (or frames first-last of it) into a
    new version; returns its name. The preview reports when it is done."""
    job = active_job()
    if job is None or getattr(job, "_domain_name", None) != domain.name:
        raise RuntimeError(f"'{domain.name}' is not the domain being previewed")
    cache = preview_cache(job)
    first = cache["first"] if first is None else first
    if last is None:
        # Through the live frame: the stats can trail it by the copies still
        # on their way, which the bridge finishes before saving.
        live = getattr(job, "_live_frame", None)
        last = max(cache["last"], live if live is not None else cache["last"])
    slot, directory, lock = _new_version(domain, base)
    try:
        job.export_cache(context, directory, slot, first, last, lock=lock)
    except BaseException:
        lock.release()
        raise
    return slot


def _preview_save_blocker(context):
    """Why the live preview cannot be saved now (None: it can)."""
    domain = _domain(context)
    job = active_job()
    if domain is None or job is None or active_mode() != "previewing":
        return "Turn on Cache Preview, then play a live preview"
    if getattr(job, "_domain_name", None) != domain.name:
        return f"The live preview belongs to '{job._domain_name}': select that domain to save it"
    if not job._worker._session.get("preview_cache"):
        return "This preview keeps no cache: turn on Cache Preview (under Play) and play again"
    if getattr(job, "_export_pending", None):
        return "Already saving"
    if preview_cache(job).get("frames", 0) <= 0:
        return "Nothing cached yet"
    return None


class FUMARIS_OT_version_save(_DomainOperator, Operator):
    bl_idname = "fumaris.version_save"
    bl_label = "Save Sequence"
    bl_description = "Save the frames the live preview has cached as a new version: VDB files with the settings that made them, which import, render (Cycles included) and compare like a bake. Uses the Output settings (grids, resolutions, Glow), at the preview's resolution"

    frame_only: BoolProperty(name="This Frame Only", default=False)

    @classmethod
    def description(cls, _context, properties):
        if properties.frame_only:
            return "Save the frame the live preview shows as a new one-frame version (VDB files with the settings that made it)"
        return cls.bl_description

    @classmethod
    def poll(cls, context):
        reason = _preview_save_blocker(context)
        if reason:
            cls.poll_message_set(reason)
            return False
        return True

    def execute(self, context):
        domain = _domain(context)
        cache = preview_cache()
        if self.frame_only:
            # The frame the preview shows: the live one, or the cached frame at
            # or before it (Balanced skips some; the live frame's copy may
            # still be on its way, so the bridge finishes it first).
            frame = context.scene.frame_current
            if frame < cache["first"]:
                self.report({"ERROR"}, f"Frame {frame} is no longer cached (oldest kept: {cache['first']})")
                return {"CANCELLED"}
            try:
                slot = start_cache_export(context, domain, f"frame_{frame}", frame, frame)
            except (RuntimeError, OSError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            self.report({"INFO"}, f"Saving frame {frame} as '{slot}'")
        else:
            try:
                slot = start_cache_export(context, domain, "preview")
            except (RuntimeError, OSError) as error:
                self.report({"ERROR"}, str(error))
                return {"CANCELLED"}
            self.report({"INFO"}, f"Saving the cached frames from {cache['first']} as '{slot}'")
        if domain.fumaris.export_velocity_vdb and not active_job()._worker._session.get("preview_cache_velocity"):
            # Velocity is kept only when the preview starts with it on.
            self.report({"WARNING"}, "This preview did not keep velocity: the version has no velocity grid. "
                        "Turn on velocity export before Play to save it")
        domain.fumaris.compare_selected = "vdb:" + slot
        forget_scan()
        return {"FINISHED"}


class FUMARIS_OT_snapshot(_DomainOperator, Operator):
    bl_idname = "fumaris.snapshot"
    bl_label = "Save Snapshot"
    bl_description = "Save what the 3D Viewport shows (the live preview included) as an image in Versions, with the current settings"

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        domain = _domain(context)
        try:
            pixels = capture_snapshot(context, domain)
        except Exception as error:
            self.report({"ERROR"}, f"Snapshot failed: {error}")
            return {"CANCELLED"}
        root = snapshot_root(domain, create=True)
        path = os.path.join(root, f"{time.strftime('%Y%m%d-%H%M%S')}_f{context.scene.frame_current:04d}.png")
        save_png(pixels, path)
        write_sidecar(path, **settings_snapshot_data(domain, _participants_for_snapshot(domain)))
        domain.fumaris.compare_selected = "img:" + path
        forget_scan()
        self.report({"INFO"}, "Snapshot saved in Versions")
        return {"FINISHED"}


# --- Panel -------------------------------------------------------------------

def draw_entry_menu(layout, domain, key):
    """An entry's extra actions (its More button and right-click)."""
    if domain is None:
        return
    entry = find_entry(domain, key)
    if entry is None:
        return
    layout.operator("fumaris.entry_note", icon="GREASEPENCIL").key = key
    settings = layout.row()
    settings.enabled = entry["settings"]
    settings.operator("fumaris.entry_save_preset", icon="PRESET_NEW").key = key
    layout.separator()
    layout.operator("fumaris.entry_delete", text="Delete…", icon="TRASH").key = key


def _button_context_menu(self, context):
    """Right-click on an entry in Versions: its actions."""
    op = getattr(context, "button_operator", None)
    try:
        identifier = op.bl_rna.identifier if op is not None else ""
    except (AttributeError, ReferenceError):
        return
    if identifier not in {"FUMARIS_OT_entry_select", "FUMARIS_OT_entry_show", "FUMARIS_OT_entry_pin",
                          "FUMARIS_OT_entry_image"}:
        return
    domain = _domain(context)
    if domain is None or not op.key:
        return
    entry = find_entry(domain, op.key)
    if entry is None:
        return
    layout = self.layout
    layout.separator()
    layout.label(text=entry["name"], icon="VOLUME_DATA" if entry["kind"] == "vdb" else "IMAGE_DATA")
    if entry["kind"] == "vdb":
        layout.operator("fumaris.entry_import", text="Remove from Scene" if _entry_volumes(entry) else "Import",
                        icon="IMPORT").key = op.key
    else:
        layout.operator("fumaris.entry_image", icon="IMAGE_DATA").key = op.key
    layout.operator("fumaris.entry_changes", icon="SEQ_SPLITVIEW").key = op.key
    draw_entry_menu(layout, domain, op.key)


def _draw_entry_row(column, domain, entry, selected):
    from . import ui
    row = column.row(align=True)
    icon = "VOLUME_DATA" if entry["kind"] == "vdb" else "IMAGE_DATA"
    if ui._TIGHT:
        # The name gets the room; its frames are under the selected entry.
        name = row.row(align=True)
    else:
        split = row.split(factor=0.62, align=True)
        name = split.row(align=True)
        split.label(text="" if entry["kind"] == "image" else version_summary(entry),
                    icon="LOCKED" if entry["protected"] else "NONE")
    name.operator("fumaris.entry_select", text=entry["name"], icon=icon, depress=selected).key = entry["key"]
    row.operator("fumaris.entry_pin", text="", icon="PINNED" if entry["pinned"] else "UNPINNED",
                 emboss=False).key = entry["key"]
    if entry["kind"] == "vdb":
        shown = entry_shown(domain, entry)
        row.operator("fumaris.entry_show", text="", icon="HIDE_OFF" if shown else "HIDE_ON",
                     depress=shown).key = entry["key"]
    else:
        row.operator("fumaris.entry_image", text="", icon="IMAGE_DATA").key = entry["key"]


def draw_compare(layout, domain, props):
    entries = list_entries(domain)
    selected = props.compare_selected
    box = layout.box()
    column = box.column(align=True)
    if not entries:
        column.label(text="Bake, or save while previewing (Cache Preview)", icon="INFO")
    pinned = [entry for entry in entries if entry["pinned"]]
    others = [entry for entry in entries if not entry["pinned"]]
    for entry in pinned:
        _draw_entry_row(column, domain, entry, entry["key"] == selected)
    if pinned and others:
        column.separator(factor=0.6, type="LINE")
    for entry in others:
        _draw_entry_row(column, domain, entry, entry["key"] == selected)

    entry = next((entry for entry in entries if entry["key"] == selected), None)
    if entry is None:
        return
    from . import ui
    # Tight (Blender's default Properties width): the first button on its own row.
    column = layout.column(align=True)
    row = column.row(align=True)
    if entry["kind"] == "vdb":
        imported = bool(_entry_volumes(entry))
        row.operator("fumaris.entry_import", text="Remove" if imported else "Import",
                     icon="X" if imported else "IMPORT").key = entry["key"]
    else:
        row.operator("fumaris.entry_image", icon="IMAGE_DATA").key = entry["key"]
    if ui._TIGHT:
        row = column.row(align=True)
    changes = row.row(align=True)
    changes.enabled = entry["settings"]
    changes.operator("fumaris.entry_changes", icon="SEQ_SPLITVIEW").key = entry["key"]
    row.operator("fumaris.entry_menu", text="", icon="DOWNARROW_HLT").key = entry["key"]
    # What it is, on its own line (wraps in a narrow panel).
    parts = [version_summary(entry, size=True) if entry["kind"] == "vdb"
             else (f"Snapshot of frame {entry['frame']}" if entry.get("frame") is not None else "Snapshot")]
    if entry["modified"]:
        parts.append("saved " + time.strftime("%b %d %H:%M", time.localtime(entry["modified"])))
    if entry["protected"]:
        parts.append("protected")
    ui._wrapped(column, " · ".join(parts), "INFO")


CLASSES = (
    FUMARIS_OT_entry_select,
    FUMARIS_OT_entry_pin,
    FUMARIS_OT_entry_show,
    FUMARIS_OT_entry_import,
    FUMARIS_OT_entry_image,
    FUMARIS_OT_entry_changes,
    FUMARIS_OT_entry_load,
    FUMARIS_OT_entry_save_preset,
    FUMARIS_OT_entry_note,
    FUMARIS_OT_entry_delete,
    FUMARIS_OT_entry_menu,
    FUMARIS_OT_version_save,
    FUMARIS_OT_snapshot,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.UI_MT_button_context_menu.append(_button_context_menu)


def unregister():
    bpy.types.UI_MT_button_context_menu.remove(_button_context_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
