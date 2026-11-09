"""Compare: versions of a domain's cache, A/B viewing, snapshots and preset swaps.

Every cache slot of a domain is a version: a folder of VDB frames and the
settings they were baked with. The current slot is A. One other slot, B, can
be shown in the viewport next to it (or instead of it), its settings listed
against A's, or swapped in. A paused preview's cache (Export Cache to VDB)
and single frames can be kept as versions of their own, so they import and
compare like any bake. Snapshots are images of the viewport, kept in a slot's folder, for
side-by-side stills. Two presets can be swapped in place to try a look.
"""
import json
import os
import shutil
import time

import bpy
import mathutils
from bpy.props import StringProperty
from bpy.types import Menu, Operator

from .cache import (
    apply_settings_snapshot,
    is_protected,
    next_slot_name,
    read_settings_snapshot,
    write_settings_snapshot,
)
from .importers import VOLUME_MARKER, _frame_files, _frame_number, import_sequence
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


# --- Keeping preview recordings and frames -----------------------------------

def _link_or_copy(source, target):
    try:
        os.link(source, target)
    except OSError:
        shutil.copy2(source, target)


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


def keep_frames(domain, files, source_prefix, base, note):
    """Link (or copy) frame files into a new version; returns its slot name."""
    slot, directory = _new_version(domain, base)
    prefix = _prefix(domain.fumaris)
    for path in files:
        frame = _frame_number(path, source_prefix)
        name = os.path.basename(path).replace(source_prefix, prefix, 1) if frame is not None else os.path.basename(path)
        _link_or_copy(path, os.path.join(directory, name))
    write_settings_snapshot(directory, domain, _participants_for_snapshot(domain))
    write_note(directory, note)
    forget_scan()
    return slot


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


# --- Preset A/B --------------------------------------------------------------

def _preset_text(props, side):
    name = getattr(props, f"compare_preset_{side.lower()}")
    return name or ("Current" if side == "A" else "Pick…")


def apply_preset_side(props, side):
    """Apply preset A or B. A left empty means the settings before the first swap."""
    from .presets import apply_data, apply_preset, capture
    if props.compare_preset_active not in ("B",) and side == "B" and not props.compare_preset_a:
        props.compare_stash = json.dumps(capture(props))
    name = getattr(props, f"compare_preset_{side.lower()}")
    if name:
        count = apply_preset(props, name)
    elif side == "A" and props.compare_stash:
        count = apply_data(props, json.loads(props.compare_stash))
    else:
        raise RuntimeError(f"Pick a preset for {side}")
    props.compare_preset_active = side
    return count


# --- Operators ---------------------------------------------------------------

class _DomainOperator:
    @classmethod
    def poll(cls, context):
        return _domain(context) is not None and active_mode() != "baking"


class FUMARIS_OT_version_use(_DomainOperator, Operator):
    bl_idname = "fumaris.version_use"
    bl_label = "Use Version"
    bl_description = "Make this cache slot the current one (A) and show its bake"

    name: StringProperty()

    @classmethod
    def description(cls, context, properties):
        domain = _domain(context)
        if domain is None:
            return cls.bl_description
        for version in list_versions(domain):
            if version["name"] == properties.name:
                parts = [f"Cache slot '{version['name']}': {version_summary(version, size=True)}"]
                if version["modified"]:
                    parts.append("Written " + time.strftime("%Y-%m-%d %H:%M", time.localtime(version["modified"])))
                if version["snapshots"]:
                    parts.append(f"{version['snapshots']} snapshot(s)")
                if version["note"]:
                    parts.append(version["note"])
                if not version["settings"]:
                    parts.append("No stored settings")
                return ".\n".join(parts) + ".\nClick to make it the current slot"
        return cls.bl_description

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


class FUMARIS_OT_version_compare(_DomainOperator, Operator):
    bl_idname = "fumaris.version_compare"
    bl_label = "Compare"
    bl_description = "Compare this cache slot (B) with the current one (A) in the viewport"

    name: StringProperty()

    def execute(self, context):
        domain = _domain(context)
        if domain.fumaris.compare_slot == self.name and compare_volumes(domain):
            clear_compare(domain)
            domain.fumaris.compare_slot = ""
            return {"FINISHED"}
        try:
            load_compare(domain, self.name)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class FUMARIS_OT_compare_clear(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_clear"
    bl_label = "Stop Comparing"
    bl_description = "Hide B and put its volume back in place (its files are kept)"

    def execute(self, context):
        domain = _domain(context)
        clear_compare(domain)
        domain.fumaris.compare_slot = ""
        return {"FINISHED"}


class FUMARIS_OT_compare_swap(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_swap"
    bl_label = "Swap A and B"
    bl_description = "Make B the current cache slot and compare the old current slot as B"

    @classmethod
    def poll(cls, context):
        domain = _domain(context)
        return domain is not None and active_job() is None and bool(domain.fumaris.compare_slot)

    def execute(self, context):
        domain = _domain(context)
        props = domain.fumaris
        old = os.path.basename(output_directory_for_object(domain))
        props.cache_slot = props.compare_slot
        show_slot(context, domain)
        try:
            load_compare(domain, old)
        except RuntimeError as error:
            self.report({"WARNING"}, str(error))
        forget_scan()
        return {"FINISHED"}


def _snapshot_pair(domain):
    current = output_directory_for_object(domain)
    slot = domain.fumaris.compare_slot
    a = snapshots(current)
    if slot:
        b = snapshots(cache_slot_directory(domain, slot))
        return (a[-1] if a else None), (b[-1] if b else None)
    return (a[-1] if a else None), (a[-2] if len(a) > 1 else None)


class FUMARIS_OT_compare_diff(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_diff"
    bl_label = "Settings Differences"
    bl_description = "List the settings that differ between A and B (as baked; A uses the current settings if it has none stored)"

    @classmethod
    def poll(cls, context):
        domain = _domain(context)
        return domain is not None and bool(domain.fumaris.compare_slot)

    def invoke(self, context, _event):
        domain = _domain(context)
        props = domain.fumaris
        b = read_settings_snapshot(cache_slot_directory(domain, props.compare_slot))
        if b is None:
            self.report({"ERROR"}, f"'{props.compare_slot}' has no stored settings")
            return {"CANCELLED"}
        a = read_settings_snapshot(output_directory_for_object(domain))
        source = "baked"
        if a is None:
            a, source = _current_settings(domain), "current settings"
        _last_diff["title"] = f"A '{props.cache_slot}' ({source})  vs  B '{props.compare_slot}'"
        _last_diff["lines"] = settings_differences(a, b, props)
        try:
            _last_diff["measures"] = measured_differences(domain, context.scene.frame_current)
        except Exception as error:
            print(f"Fumaris Compare: measures unavailable: {error}")
            _last_diff["measures"] = []
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


class FUMARIS_OT_compare_use_b_settings(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_use_b_settings"
    bl_label = "Use B's Settings"
    bl_description = "Set the domain and its sources to the settings B was baked with (sources are matched by name)"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        domain = _domain(context)
        return domain is not None and active_job() is None and bool(domain.fumaris.compare_slot)

    def execute(self, context):
        domain = _domain(context)
        data = read_settings_snapshot(cache_slot_directory(domain, domain.fumaris.compare_slot))
        if data is None:
            self.report({"ERROR"}, "B has no stored settings")
            return {"CANCELLED"}
        changed, _restored, missing = apply_settings_snapshot(domain, data, bpy.data.objects)
        message = f"Applied {changed} settings from '{domain.fumaris.compare_slot}'"
        if missing:
            self.report({"WARNING"}, message + f"; not found: {', '.join(missing[:4])}")
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class FUMARIS_OT_version_note(_DomainOperator, Operator):
    bl_idname = "fumaris.version_note"
    bl_label = "Version Note"
    bl_description = "Describe this cache slot (shown in its tooltip and the version list)"

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
    """Write the paused preview's cache (or frames first-last of it) into a
    new cache slot; returns its name. The preview reports when it is done."""
    job = active_job()
    cache = preview_cache(job)
    first = cache["first"] if first is None else first
    last = cache["last"] if last is None else last
    slot, directory = _new_version(domain, base)
    job.export_cache(context, directory, slot, first, last)
    return slot


class FUMARIS_OT_preview_cache_export(_DomainOperator, Operator):
    bl_idname = "fumaris.preview_cache_export"
    bl_label = "Export Cache to VDB"
    bl_description = "Write the frames the paused live preview has cached as VDB files, into a new cache slot (a version): they import, render and compare like a bake. Uses the Output settings (grids, resolutions, Glow), at the preview's resolution"

    @classmethod
    def poll(cls, context):
        domain = _domain(context)
        job = active_job()
        if domain is None or job is None or active_mode() != "previewing":
            cls.poll_message_set("Turn on Cache Preview (under Play), play a live preview, then pause it"
                                 if domain is not None and not domain.fumaris.preview_cache
                                 else "Play a live preview, then pause it")
            return False
        if not job._worker._session.get("preview_cache"):
            cls.poll_message_set("This preview keeps no cache: turn on Cache Preview (under Play) and play again")
            return False
        if not getattr(job, "_pause_requested", False):
            cls.poll_message_set("Pause the live preview first")
            return False
        if getattr(job, "_export_pending", None):
            cls.poll_message_set("Already writing the cache")
            return False
        if preview_cache(job).get("frames", 0) <= 0:
            cls.poll_message_set("Nothing cached yet")
            return False
        return True

    def execute(self, context):
        domain = _domain(context)
        cache = preview_cache()
        base = f"{domain.fumaris.cache_slot.strip() or 'main'}_preview"
        slot = start_cache_export(context, domain, base)
        self.report({"INFO"}, f"Writing cached frames {cache['first']}-{cache['last']} to cache slot '{slot}'")
        return {"FINISHED"}


class FUMARIS_OT_version_keep_frame(_DomainOperator, Operator):
    bl_idname = "fumaris.version_keep_frame"
    bl_label = "Keep Frame"
    bl_description = "Keep this frame of the current bake (or of the paused live preview's cache) as a one-frame cache slot, to compare stills"

    def execute(self, context):
        domain = _domain(context)
        frame = context.scene.frame_current
        base = f"{domain.fumaris.cache_slot.strip() or 'main'}_f{frame}"
        cache = preview_cache()
        job = active_job()
        if (cache.get("frames", 0) > 0 and getattr(job, "_pause_requested", False)
                and not getattr(job, "_export_pending", None) and cache["first"] <= frame <= cache["last"]):
            slot = start_cache_export(context, domain, base, frame, frame)
            self.report({"INFO"}, f"Writing frame {frame} of the preview cache to cache slot '{slot}'")
            return {"FINISHED"}
        directory = output_directory_for_object(domain)
        prefix = _prefix(domain.fumaris)
        files = [path for path in _frame_files(directory, prefix) if _frame_number(path, prefix) == frame]
        if not files:
            self.report({"ERROR"}, f"Frame {frame} is neither baked in this slot nor in a paused preview's cache")
            return {"CANCELLED"}
        slot = keep_frames(domain, files, prefix, base, f"frame {frame} of {domain.fumaris.cache_slot}")
        self.report({"INFO"}, f"Kept frame {frame} as cache slot '{slot}'")
        return {"FINISHED"}


class FUMARIS_OT_snapshot(_DomainOperator, Operator):
    bl_idname = "fumaris.snapshot"
    bl_label = "Snapshot"
    bl_description = "Save what the 3D Viewport shows (live preview included) as an image in the current cache slot, for side-by-side comparison"

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
        folder = os.path.join(output_directory_for_object(domain), SNAPSHOT_DIRECTORY)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"{time.strftime('%Y%m%d-%H%M%S')}_f{context.scene.frame_current:04d}.png")
        save_png(pixels, path)
        forget_scan()
        self.report({"INFO"}, f"Snapshot saved: {path}")
        return {"FINISHED"}


class FUMARIS_OT_compare_snapshots(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_snapshots"
    bl_label = "Compare Snapshots"
    bl_description = "Put the latest snapshot of A and of B (or A's last two, without B) side by side in an Image Editor"

    @classmethod
    def poll(cls, context):
        return _domain(context) is not None

    def execute(self, context):
        domain = _domain(context)
        a, b = _snapshot_pair(domain)
        if a is None or b is None:
            self.report({"ERROR"}, "Needs a snapshot of A and of B (or two of A)")
            return {"CANCELLED"}
        pixels = side_by_side(load_png(a), load_png(b))
        root = os.path.join(os.path.dirname(output_directory_for_object(domain)), ".compare")
        os.makedirs(root, exist_ok=True)
        name = f"{domain.fumaris.cache_slot}_vs_{domain.fumaris.compare_slot or 'earlier'}.png"
        path = os.path.join(root, name)
        save_png(pixels, path)
        title = f"Fumaris A|B {domain.name}"
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


class FUMARIS_OT_compare_preset_pick(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_preset_pick"
    bl_label = "Pick Preset"
    bl_description = "Use this preset as A or B"

    side: StringProperty()
    name: StringProperty()

    def execute(self, context):
        props = _domain(context).fumaris
        setattr(props, f"compare_preset_{self.side.lower()}", self.name)
        return {"FINISHED"}


class FUMARIS_OT_compare_preset_apply(_DomainOperator, Operator):
    bl_idname = "fumaris.compare_preset_apply"
    bl_label = "Apply"
    bl_description = "Apply preset A or B to the domain. A left as Current returns to the settings from before the first switch to B"
    bl_options = {"REGISTER", "UNDO"}

    side: StringProperty()

    def execute(self, context):
        props = _domain(context).fumaris
        side = self.side or ("A" if props.compare_preset_active == "B" else "B")
        try:
            count = apply_preset_side(props, side)
        except (OSError, ValueError, RuntimeError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"{side}: {_preset_text(props, side)} ({count} settings)")
        return {"FINISHED"}


class _ComparePresetMenu:
    side = "A"

    def draw(self, context):
        from .presets import list_presets
        layout = self.layout
        if self.side == "A":
            layout.operator("fumaris.compare_preset_pick", text="Current").side = "A"
        names = list_presets("domain")
        if not names:
            layout.label(text="No saved domain presets: save one with + above")
        for name in names:
            row = layout.row(align=True)
            op = row.operator("fumaris.compare_preset_pick", text=name)
            op.side = self.side
            op.name = name
            row.operator("fumaris.preset_delete", text="", icon="X").name = name


class FUMARIS_MT_compare_preset_a(_ComparePresetMenu, Menu):
    bl_label = "Preset A"
    bl_idname = "FUMARIS_MT_compare_preset_a"
    side = "A"


class FUMARIS_MT_compare_preset_b(_ComparePresetMenu, Menu):
    bl_label = "Preset B"
    bl_idname = "FUMARIS_MT_compare_preset_b"
    side = "B"


# --- Panel -------------------------------------------------------------------

def draw_compare(layout, domain, props):
    idle = active_job() is None
    column = layout.column(align=True)

    # Presets: two to swap between, Current standing for unsaved settings.
    header = column.row(align=True)
    header.label(text="Presets")
    header.operator("fumaris.compare_preset_apply", text="Swap", icon="ARROW_LEFTRIGHT").side = ""
    header.operator("fumaris.preset_save", text="", icon="ADD")
    for side in ("A", "B"):
        row = column.row(align=True)
        pick = row.split(factor=0.8, align=True)
        pick.menu(f"FUMARIS_MT_compare_preset_{side.lower()}", text=f"{side}: {_preset_text(props, side)}")
        op = pick.operator("fumaris.compare_preset_apply", text="Apply",
                           depress=props.compare_preset_active == side)
        op.side = side

    # Versions: every cache slot of this domain.
    column.separator(factor=0.8)
    header = column.row(align=True)
    header.label(text="Versions")
    new = header.row(align=True)
    new.enabled = idle
    new.operator("fumaris.cache_slot_new", text="", icon="ADD")
    header.operator("fumaris.version_keep_frame", text="", icon="IMAGE_PLANE")
    column.operator("fumaris.preview_cache_export", text="Export Cache to VDB", icon="EXPORT")
    versions = list_versions(domain)
    current = os.path.basename(_current_directory(domain) or "")
    if not versions:
        column.label(text="Nothing baked yet")
    for version in versions:
        is_a = version["name"] == current
        is_b = version["name"] == props.compare_slot and not is_a
        split = column.split(factor=0.45, align=True)
        use = split.row(align=True)
        use.enabled = idle
        label = version["name"] + (f" — {version['note']}" if version["note"] else "")
        use.operator("fumaris.version_use", text=label,
                     icon="RADIOBUT_ON" if is_a else "RADIOBUT_OFF", depress=is_a).name = version["name"]
        split = split.split(factor=0.62, align=True)
        split.label(text=version_summary(version), icon="LOCKED" if version["protected"] else "BLANK1")
        buttons = split.row(align=True)
        b = buttons.row(align=True)
        b.enabled = not is_a and version["frames"] > 0
        b.operator("fumaris.version_compare", text="B", depress=is_b).name = version["name"]
        buttons.operator("fumaris.version_note", text="", icon="GREASEPENCIL").name = version["name"]

    # A against B.
    if props.compare_slot:
        column.separator(factor=0.8)
        row = column.row(align=True)
        row.label(text=f"A {current}  vs  B {props.compare_slot}", icon="SEQ_SPLITVIEW")
        row.operator("fumaris.compare_clear", text="", icon="X")
        column.row(align=True).prop(props, "compare_view", expand=True)
        row = column.row(align=True)
        row.operator("fumaris.compare_swap", text="Swap", icon="ARROW_LEFTRIGHT")
        row.operator("fumaris.compare_diff", text="Diff", icon="PRESET")
        row.operator("fumaris.compare_use_b_settings", text="Use B", icon="LOOP_BACK")
        if not compare_volumes(domain):
            column.label(text="Click B on a version to show it", icon="INFO")

    # Stills.
    column.separator(factor=0.8)
    row = column.row(align=True)
    row.operator("fumaris.snapshot", text="Snapshot", icon="RESTRICT_RENDER_OFF")
    row.operator("fumaris.compare_snapshots", text="A|B Image", icon="IMAGE_REFERENCE")


CLASSES = (
    FUMARIS_OT_version_use,
    FUMARIS_OT_version_compare,
    FUMARIS_OT_compare_clear,
    FUMARIS_OT_compare_swap,
    FUMARIS_OT_compare_diff,
    FUMARIS_OT_compare_use_b_settings,
    FUMARIS_OT_version_note,
    FUMARIS_OT_preview_cache_export,
    FUMARIS_OT_version_keep_frame,
    FUMARIS_OT_snapshot,
    FUMARIS_OT_compare_snapshots,
    FUMARIS_OT_compare_preset_pick,
    FUMARIS_OT_compare_preset_apply,
    FUMARIS_MT_compare_preset_a,
    FUMARIS_MT_compare_preset_b,
)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
