"""Versions: saved results of a domain, shown and compared in the viewport.

Every cache slot of a domain is a version: a folder of VDB frames (a bake, or
a paused preview's cache saved with Save Version), the settings that made it,
and any snapshot images. The current version is where bakes go. The eye on a
version shows it; with a second version's eye on, the two stand side by side
(the second moved beside the domain) and What Changed lists the settings that
differ. Right-click a version (or its menu) to load its settings, save them as
a preset, view its images or delete it.
"""
import json
import os
import shutil
import time

import bpy
import mathutils
from bpy.props import BoolProperty, StringProperty
from bpy.types import Menu, Operator

from .cache import (
    GUARD_FILE,
    SETTINGS_FILE,
    apply_settings_snapshot,
    cache_lock,
    is_protected,
    recover_cache_lock,
    require_unprotected,
    next_slot_name,
    read_settings_snapshot,
    write_settings_snapshot,
)
from .importers import VOLUME_MARKER, _frame_files, _frame_number, delete_generated_data, import_sequence
from .runtime import active_job, active_mode
from .utils import cache_slot_directory, output_directory_for_object, peek_output_directory_for_object

# On the volume showing slot B: the name of the domain it is compared for.
COMPARE_MARKER = "fumaris_compare_b"
NOTE_FILE = ".fumaris_version.json"
STATS_FILE = ".fumaris_bake_stats.json"
SNAPSHOT_DIRECTORY = "snapshots"
# Side by side: B sits this many domain widths along X.
SIDE_OFFSET = 1.15
# Background behind the live preview where no scene image is available
# (Blender's default viewport grey).
SNAPSHOT_BACKGROUND = 0.235
_SCAN_SECONDS = 2.0
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
    cached = _scan_cache.get(key)
    if cached and not fresh and now - cached[0] < _SCAN_SECONDS:
        return cached[1]
    try:
        with os.scandir(root) as entries:
            names = sorted(entry.name for entry in entries
                           if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."))
    except OSError:
        names = []
    if os.path.basename(current) not in names:
        names.append(os.path.basename(current))
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
        if path != current and not (files or images or protected):
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
        })
    _scan_cache[key] = (now, versions)
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

def _new_version(domain, base):
    prefix = _prefix(domain.fumaris)

    def taken(slot):
        directory = cache_slot_directory(domain, slot)
        return bool(is_protected(directory) or _frame_files(directory, prefix))

    slot = base if not taken(base) else next_slot_name(base, taken)
    directory = cache_slot_directory(domain, slot)
    os.makedirs(directory, exist_ok=True)
    return slot, directory


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
    out = scene.copy()
    out[..., :3] = smoke[..., :3] + (1.0 - smoke[..., 3:4]) * scene[..., :3]
    out[..., 3] = 1.0
    return np.clip(out, 0.0, 1.0)


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


# --- Showing versions --------------------------------------------------------

def _previewing(domain):
    job = active_job()
    return job is not None and getattr(job, "_domain_name", None) == domain.name and active_mode() == "previewing"


def current_shown(domain):
    """The current version's eye: its bake is visible (beside B when comparing)."""
    if compare_volumes(domain):
        return domain.fumaris.compare_view != "B"
    current = _current_directory(domain)
    return bool(current) and any(not obj.hide_viewport for obj in _slot_volumes(current))


def compared_name(domain):
    return domain.fumaris.compare_slot if compare_volumes(domain) else ""


def toggle_version(context, domain, name):
    """The eye on a version: show or hide it. Another version than the current
    one is shown beside the current one (or alone when the current is hidden)."""
    props = domain.fumaris
    current = os.path.basename(_current_directory(domain) or "")
    if name == current:
        if compare_volumes(domain):
            props.compare_view = "B" if props.compare_view != "B" else "SIDE"
            return
        volumes = _slot_volumes(_current_directory(domain))
        if not volumes:
            show_slot(context, domain)
            return
        shown = any(not obj.hide_viewport for obj in volumes)
        for obj in volumes:
            _set_shown(obj, not shown)
        return
    if compared_name(domain) == name:
        clear_compare(domain)
        props.compare_slot = ""
        return
    side = _previewing(domain) or current_shown(domain) or not _frame_files(_current_directory(domain), _prefix(props))
    props["compare_view"] = 2 if side else 1   # SIDE or B, without the update
    load_compare(domain, name)


# --- What Changed -------------------------------------------------------------

_changes_cache = {}


def _settings_mtime(directory):
    try:
        return os.path.getmtime(os.path.join(directory, SETTINGS_FILE))
    except OSError:
        return 0.0


def what_changed(domain):
    """(title, lines) between what the current version shows and the compared
    one: the live preview's settings while previewing (or when the current
    version stored none), else the current version's stored settings."""
    name = compared_name(domain)
    if not name:
        return "", []
    props = domain.fumaris
    a_dir = output_directory_for_object(domain)
    b_dir = cache_slot_directory(domain, name)
    live = _previewing(domain) or read_settings_snapshot(a_dir) is None
    key = (domain.name, a_dir, b_dir, live, _settings_mtime(a_dir), _settings_mtime(b_dir))
    now = time.monotonic()
    cached = _changes_cache.get(domain.name)
    if cached and cached[0] == key and now - cached[1] < _SCAN_SECONDS:
        return cached[2]
    b = read_settings_snapshot(b_dir)
    if b is None:
        result = (f"'{name}' has no stored settings", [])
    else:
        a = _current_settings(domain) if live else read_settings_snapshot(a_dir)
        title = f"{'Current settings' if live else props.cache_slot} → {name}"
        result = (title, settings_differences(a, b, props))
    _changes_cache[domain.name] = (key, now, result)
    return result


# --- Deleting a version -------------------------------------------------------

def delete_version(domain, name):
    """Delete a version's frames, images, settings and note (a protected one is refused)."""
    props = domain.fumaris
    directory = cache_slot_directory(domain, name)
    require_unprotected(directory, "delete it")
    if compared_name(domain) == name or props.compare_slot == name:
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
    for leftover in (GUARD_FILE,):
        try:
            os.remove(os.path.join(directory, leftover))
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


# --- Operators ---------------------------------------------------------------

class _DomainOperator:
    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_mode() != "baking"


def _find_version(domain, name):
    return next((version for version in list_versions(domain) if version["name"] == name), None)


class FUMARIS_OT_version_use(_DomainOperator, Operator):
    bl_idname = "fumaris.version_use"
    bl_label = "Make Current"
    bl_description = "Make this the current version: new bakes go here"

    name: StringProperty()

    @classmethod
    def description(cls, context, properties):
        domain = _domain(context)
        version = _find_version(domain, properties.name) if domain else None
        if version is None:
            return cls.bl_description
        parts = [f"Version '{version['name']}': {version_summary(version, size=True)}"]
        if version["modified"]:
            parts.append("Written " + time.strftime("%Y-%m-%d %H:%M", time.localtime(version["modified"])))
        if version["snapshots"]:
            parts.append(f"{version['snapshots']} image(s)")
        if version["note"]:
            parts.append(version["note"])
        if not version["settings"]:
            parts.append("No stored settings")
        return ".\n".join(parts) + ".\nClick to make it the current version (new bakes go there). Right-click for more"

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_job() is None

    def execute(self, context):
        domain = _domain(context)
        domain.fumaris.cache_slot = self.name
        try:
            show_slot(context, domain)
        except RuntimeError as error:
            self.report({"WARNING"}, f"Could not import '{self.name}': {error}")
        forget_scan()
        return {"FINISHED"}


class FUMARIS_OT_version_show(_DomainOperator, Operator):
    bl_idname = "fumaris.version_show"
    bl_label = "Show"
    bl_description = "Show or hide this version in the viewport. With two shown, the second stands beside the domain"

    name: StringProperty()

    def execute(self, context):
        domain = _domain(context)
        try:
            toggle_version(context, domain, self.name)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class FUMARIS_OT_version_menu(_DomainOperator, Operator):
    bl_idname = "fumaris.version_menu"
    bl_label = "Version"
    bl_description = "Load this version's settings, save them as a preset, view its images, compare or delete it"

    name: StringProperty()

    def invoke(self, context, _event):
        name = self.name

        def draw(menu, _context):
            draw_version_menu(menu.layout, _domain(_context), name)
        context.window_manager.popup_menu(draw, title=f"Version '{name}'", icon="FILE_FOLDER")
        return {"FINISHED"}

    def execute(self, context):
        return {"FINISHED"}


class FUMARIS_OT_version_load_settings(_DomainOperator, Operator):
    bl_idname = "fumaris.version_load_settings"
    bl_label = "Load Settings"
    bl_description = "Set the domain and its sources to the settings this version was made with (sources are matched by name)"
    bl_options = {"REGISTER", "UNDO"}

    name: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_mode() != "baking"

    def execute(self, context):
        domain = _domain(context)
        data = read_settings_snapshot(cache_slot_directory(domain, self.name))
        if data is None:
            self.report({"ERROR"}, f"'{self.name}' has no stored settings")
            return {"CANCELLED"}
        changed, _restored, missing = apply_settings_snapshot(domain, data, bpy.data.objects)
        message = f"Loaded {changed} settings from '{self.name}'"
        if missing:
            self.report({"WARNING"}, message + f"; not found: {', '.join(missing[:4])}")
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class FUMARIS_OT_version_save_preset(_DomainOperator, Operator):
    bl_idname = "fumaris.version_save_preset"
    bl_label = "Save Settings as Preset"
    bl_description = "Save the domain settings this version was made with as a preset, available in every file"

    name: StringProperty()
    preset: StringProperty(name="Preset Name")

    def invoke(self, context, _event):
        self.preset = self.name
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        from .presets import save_preset_data
        data = read_settings_snapshot(cache_slot_directory(_domain(context), self.name))
        if data is None:
            self.report({"ERROR"}, f"'{self.name}' has no stored settings")
            return {"CANCELLED"}
        _path, count = save_preset_data(data, self.preset or self.name)
        self.report({"INFO"}, f"Saved preset '{self.preset}' ({count} settings)")
        return {"FINISHED"}


class FUMARIS_OT_version_image(_DomainOperator, Operator):
    bl_idname = "fumaris.version_image"
    bl_label = "View Image"
    bl_description = "Show this snapshot in an Image Editor"

    path: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        image = next((image for image in bpy.data.images
                      if os.path.normpath(bpy.path.abspath(image.filepath)) == os.path.normpath(self.path)), None)
        if image is None:
            image = bpy.data.images.load(self.path, check_existing=True)
        else:
            image.reload()
        if not show_image(context, image):
            self.report({"INFO"}, f"Image '{image.name}' ({self.path})")
        return {"FINISHED"}


class FUMARIS_OT_version_images_side_by_side(_DomainOperator, Operator):
    bl_idname = "fumaris.version_images_side_by_side"
    bl_label = "Image Beside the Current Version's"
    bl_description = "Put this version's latest snapshot beside the current version's latest in an Image Editor"

    name: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        domain = _domain(context)
        current = output_directory_for_object(domain)
        mine = snapshots(cache_slot_directory(domain, self.name))
        theirs = snapshots(current)
        if not mine or not theirs:
            self.report({"ERROR"}, "Needs a snapshot in this version and in the current one")
            return {"CANCELLED"}
        pixels = side_by_side(load_png(theirs[-1]), load_png(mine[-1]))
        root = os.path.join(os.path.dirname(current), ".compare")
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, f"{domain.fumaris.cache_slot}_vs_{self.name}.png")
        save_png(pixels, path)
        title = f"Fumaris {domain.fumaris.cache_slot} | {self.name}"
        image = bpy.data.images.get(title)
        if image is None:
            image = bpy.data.images.load(path, check_existing=False)
            image.name = title
        else:
            image.filepath = path
            image.reload()
        if not show_image(context, image):
            self.report({"INFO"}, f"Image '{title}' ({path})")
        return {"FINISHED"}


class FUMARIS_OT_version_details(_DomainOperator, Operator):
    bl_idname = "fumaris.version_details"
    bl_label = "Compare Details"
    bl_description = "Every setting that differs from the current version, and measures of both at this frame (smoke voxels, extent, top, file size) and what each bake cost"

    name: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def invoke(self, context, _event):
        domain = _domain(context)
        props = domain.fumaris
        b = read_settings_snapshot(cache_slot_directory(domain, self.name))
        if b is None:
            self.report({"ERROR"}, f"'{self.name}' has no stored settings")
            return {"CANCELLED"}
        a_dir = output_directory_for_object(domain)
        a = read_settings_snapshot(a_dir)
        source = props.cache_slot
        if a is None or _previewing(domain):
            a, source = _current_settings(domain), "current settings"
        _last_diff["title"] = f"{source}  →  {self.name}"
        _last_diff["lines"] = settings_differences(a, b, props)
        previous, props.compare_slot = props.compare_slot, self.name
        try:
            _last_diff["measures"] = measured_differences(domain, context.scene.frame_current)
        except Exception as error:
            print(f"Fumaris Compare: measures unavailable: {error}")
            _last_diff["measures"] = []
        finally:
            props.compare_slot = previous
        return context.window_manager.invoke_popup(self, width=520)

    def draw(self, _context):
        layout = self.layout
        layout.label(text=_last_diff["title"], icon="SEQ_SPLITVIEW")
        measures = _last_diff.get("measures") or []
        if measures:
            grid = layout.grid_flow(row_major=True, columns=3, even_columns=False, align=True)
            for name, a, b in measures:
                grid.label(text=name)
                grid.label(text=a)
                grid.label(text=b)
            layout.separator()
        lines = _last_diff["lines"]
        if not lines:
            layout.label(text="Same settings")
            return
        grid = layout.grid_flow(row_major=True, columns=3, even_columns=False, align=True)
        for name, a, b in lines[:60]:
            grid.label(text=name)
            grid.label(text=a)
            grid.label(text=b)
        if len(lines) > 60:
            layout.label(text=f"… and {len(lines) - 60} more")

    def execute(self, _context):
        return {"FINISHED"}


class FUMARIS_OT_version_note(_DomainOperator, Operator):
    bl_idname = "fumaris.version_note"
    bl_label = "Note…"
    bl_description = "Describe this version (shown beside its name)"

    name: StringProperty()
    note: StringProperty(name="Note")

    def invoke(self, context, _event):
        domain = _domain(context)
        self.note = read_note(cache_slot_directory(domain, self.name))
        return context.window_manager.invoke_props_dialog(self, title=f"Note for '{self.name}'")

    def execute(self, context):
        write_note(cache_slot_directory(_domain(context), self.name), self.note)
        forget_scan()
        return {"FINISHED"}


class FUMARIS_OT_version_delete(_DomainOperator, Operator):
    bl_idname = "fumaris.version_delete"
    bl_label = "Delete Version"
    bl_description = "Delete this version's frames, images and stored settings. The files cannot be recovered"

    name: StringProperty()

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_job() is None

    def invoke(self, context, event):
        version = _find_version(_domain(context), self.name)
        detail = version_summary(version, size=True) if version else ""
        return context.window_manager.invoke_confirm(
            self, event, title="Delete Version?",
            message=f"Delete version '{self.name}' ({detail})? The files cannot be recovered.",
            confirm_text="Delete", icon="WARNING")

    def execute(self, context):
        try:
            delete_version(_domain(context), self.name)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Deleted version '{self.name}'")
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
    cache = preview_cache(job)
    first = cache["first"] if first is None else first
    last = cache["last"] if last is None else last
    slot, directory = _new_version(domain, base)
    job.export_cache(context, directory, slot, first, last)
    return slot


def _preview_save_blocker(context):
    """Why the live preview cannot be saved now (None: it can)."""
    domain = _domain(context)
    job = active_job()
    if domain is None or job is None or active_mode() != "previewing":
        return "Turn on Cache Preview, then play a live preview"
    if not job._worker._session.get("preview_cache"):
        return "This preview keeps no cache: turn on Cache Preview (under Play) and play again"
    if getattr(job, "_export_pending", None):
        return "Already saving"
    if preview_cache(job).get("frames", 0) <= 0:
        return "Nothing cached yet"
    return None


class FUMARIS_OT_version_save(_DomainOperator, Operator):
    bl_idname = "fumaris.version_save"
    bl_label = "Save Version"
    bl_description = "Save the frames the live preview has cached as a new version: VDB files with the settings that made them, which import, render (Cycles included) and compare like a bake. Uses the Output settings (grids, resolutions, Glow), at the preview's resolution"

    frame_only: BoolProperty(name="This Frame Only", default=False)

    @classmethod
    def description(cls, _context, properties):
        if properties.frame_only:
            return "Save only the current frame of the live preview's cache as a new version"
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
        base = (domain.fumaris.cache_slot.strip() or "main") + "_preview"
        if self.frame_only:
            frame = context.scene.frame_current
            if not cache["first"] <= frame <= cache["last"]:
                self.report({"ERROR"}, f"Frame {frame} is not in the cache ({cache['first']}-{cache['last']})")
                return {"CANCELLED"}
            slot = start_cache_export(context, domain, f"{base}_f{frame}", frame, frame)
            self.report({"INFO"}, f"Saving frame {frame} as version '{slot}'")
        else:
            slot = start_cache_export(context, domain, base)
            self.report({"INFO"}, f"Saving cached frames {cache['first']}-{cache['last']} as version '{slot}'")
        forget_scan()
        return {"FINISHED"}


class FUMARIS_OT_snapshot(_DomainOperator, Operator):
    bl_idname = "fumaris.snapshot"
    bl_label = "Snapshot"
    bl_description = "Save what the 3D Viewport shows (the live preview included) as an image in the current version, with the settings if it has none stored"

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
        directory = output_directory_for_object(domain)
        folder = os.path.join(directory, SNAPSHOT_DIRECTORY)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}_f{context.scene.frame_current:04d}.png")
        save_png(pixels, path)
        if read_settings_snapshot(directory) is None:
            write_settings_snapshot(directory, domain, _participants_for_snapshot(domain))
        forget_scan()
        self.report({"INFO"}, f"Snapshot saved in version '{domain.fumaris.cache_slot}'")
        return {"FINISHED"}


class FUMARIS_MT_save_version(Menu):
    bl_label = "Save"
    bl_idname = "FUMARIS_MT_save_version"

    def draw(self, _context):
        self.layout.operator("fumaris.version_save", text="Save This Frame Only", icon="IMAGE_PLANE").frame_only = True


# --- Panel -------------------------------------------------------------------

def draw_version_menu(layout, domain, name):
    """A version's actions (its menu button and right-click)."""
    if domain is None:
        return
    version = _find_version(domain, name)
    current = os.path.basename(_current_directory(domain) or "")
    if name != current:
        layout.operator("fumaris.version_use", text="Make Current", icon="RADIOBUT_ON").name = name
    settings = layout.column()
    settings.enabled = bool(version and version["settings"])
    settings.operator("fumaris.version_load_settings", icon="LOOP_BACK").name = name
    settings.operator("fumaris.version_save_preset", icon="PRESET_NEW").name = name
    if name != current:
        settings.operator("fumaris.version_details", text="Compare Details…", icon="SEQ_SPLITVIEW").name = name
    images = snapshots(cache_slot_directory(domain, name))
    if images:
        layout.separator()
        for path in images[-8:][::-1]:
            layout.operator("fumaris.version_image", text=os.path.basename(path), icon="IMAGE_DATA").path = path
        if name != current:
            layout.operator("fumaris.version_images_side_by_side", text="Image Beside the Current Version's",
                            icon="IMAGE_REFERENCE").name = name
    layout.separator()
    layout.operator("fumaris.version_note", icon="GREASEPENCIL").name = name
    layout.operator("fumaris.version_delete", text="Delete…", icon="TRASH").name = name


def _button_context_menu(self, context):
    """Right-click on a version's name or eye: its actions."""
    op = getattr(context, "button_operator", None)
    try:
        identifier = op.bl_rna.identifier if op is not None else ""
    except (AttributeError, ReferenceError):
        return
    if identifier not in {"FUMARIS_OT_version_use", "FUMARIS_OT_version_show", "FUMARIS_OT_version_menu"}:
        return
    domain = _domain(context)
    if domain is None or not op.name:
        return
    self.layout.separator()
    self.layout.label(text=f"Version '{op.name}'", icon="FILE_FOLDER")
    draw_version_menu(self.layout, domain, op.name)


def draw_compare(layout, domain, props):
    idle = active_job() is None
    column = layout.column(align=True)

    # Presets: settings to reuse in any file.
    row = column.row(align=True)
    row.menu("FUMARIS_MT_presets", text="Presets", icon="PRESET")
    row.operator("fumaris.preset_save", text="", icon="ADD")

    # Versions: every saved result of this domain.
    column.separator(factor=0.8)
    header = column.row(align=True)
    header.label(text="Versions")
    new = header.row(align=True)
    new.enabled = idle
    new.operator("fumaris.cache_slot_new", text="", icon="ADD")
    versions = list_versions(domain)
    current = os.path.basename(_current_directory(domain) or "")
    compared = compared_name(domain)
    for version in versions:
        name = version["name"]
        is_current = name == current
        row = column.row(align=True)
        split = row.split(factor=0.6, align=True)
        use = split.row(align=True)
        use.enabled = idle
        label = name + (f" — {version['note']}" if version["note"] else "")
        use.operator("fumaris.version_use", text=label,
                     icon="RADIOBUT_ON" if is_current else "RADIOBUT_OFF", depress=is_current).name = name
        split.label(text=version_summary(version),
                    icon="LOCKED" if version["protected"] else ("IMAGE_DATA" if version["snapshots"] else "BLANK1"))
        eye = row.row(align=True)
        if is_current:
            eye.enabled = version["frames"] > 0 and not _previewing(domain)
            shown = eye.enabled and current_shown(domain)
        else:
            eye.enabled = version["frames"] > 0
            shown = compared == name
        eye.operator("fumaris.version_show", text="", icon="HIDE_OFF" if shown else "HIDE_ON",
                     depress=shown).name = name
        row.operator("fumaris.version_menu", text="", icon="DOWNARROW_HLT").name = name
    if len(versions) == 1 and not versions[0]["frames"]:
        column.label(text="Bake, or Save Version while previewing", icon="INFO")

    # What Changed between the two shown.
    if compared:
        title, lines = what_changed(domain)
        column.separator(factor=0.8)
        box = column.box()
        row = box.row(align=True)
        row.prop(props, "show_compare_changes", text=f"What Changed ({len(lines)})" if lines else "What Changed",
                 icon="DOWNARROW_HLT" if props.show_compare_changes else "RIGHTARROW", emboss=False)
        row.operator("fumaris.version_details", text="", icon="ZOOM_IN").name = compared
        if props.show_compare_changes:
            box.label(text=title)
            if not lines:
                box.label(text="Same settings")
            for setting, before, after in lines[:12]:
                split = box.split(factor=0.5, align=True)
                split.label(text=setting)
                split.label(text=f"{before} → {after}")
            if len(lines) > 12:
                box.label(text=f"… and {len(lines) - 12} more (magnifier for all)")


CLASSES = (
    FUMARIS_OT_version_use,
    FUMARIS_OT_version_show,
    FUMARIS_OT_version_menu,
    FUMARIS_OT_version_load_settings,
    FUMARIS_OT_version_save_preset,
    FUMARIS_OT_version_image,
    FUMARIS_OT_version_images_side_by_side,
    FUMARIS_OT_version_details,
    FUMARIS_OT_version_note,
    FUMARIS_OT_version_delete,
    FUMARIS_OT_version_save,
    FUMARIS_OT_snapshot,
    FUMARIS_MT_save_version,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.UI_MT_button_context_menu.append(_button_context_menu)


def unregister():
    bpy.types.UI_MT_button_context_menu.remove(_button_context_menu)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
