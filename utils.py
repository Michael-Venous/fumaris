import os
import sys
import hashlib
import shutil
import uuid
import stat
import tempfile
import math
import struct

import bpy

from .runtime import background_bake


# Wien's law at 550 nm (c2 / 550 nm): visible radiance ~ exp(-WIEN_KELVIN / T).
WIEN_KELVIN = 26160.0


def flame_temperature_range(start, full):
    """Keep the range ordered after Blender/native float32 conversion."""
    if not math.isfinite(start) or not math.isfinite(full):
        raise ValueError("Flame temperatures must be finite")
    maximum = struct.unpack("<f", struct.pack("<I", 0x7f7fffff))[0]
    start = struct.unpack("<f", struct.pack("<f", min(maximum, max(0.0, start))))[0]
    full = struct.unpack("<f", struct.pack("<f", min(maximum, max(0.0, full))))[0]
    if full <= start:
        bits = struct.unpack("<I", struct.pack("<f", start))[0]
        if bits == 0x7f7fffff:
            start = struct.unpack("<f", struct.pack("<I", bits - 1))[0]
            full = maximum
        else:
            full = struct.unpack("<f", struct.pack("<I", bits + 1))[0]
    return start, full


AMBIENT_AIR_KELVIN = 293.15

# Flame controls per Look: (Temperature look property, Pyro look property).
# The looks keep separate values; Colour Spread is Temperature-only and Thin
# Gas Pyro-only.
FLAME_VALUE_KEYS = {
    "brightness": ("shader_flame_brightness", "pyro_brightness"),
    "glow_temperature": ("shader_glow_temperature", "pyro_glow_temperature"),
    "contrast": ("shader_flame_contrast", "pyro_contrast"),
    "temperature_multiplier": ("shader_temperature_multiplier", "pyro_temperature_multiplier"),
    "flame_core": ("shader_flame_core", "pyro_flame_core"),
    "flame_tint": ("shader_flame_tint", "pyro_flame_tint"),
    "scatter_glow": ("shader_scatter_glow", "pyro_scatter_glow"),
    "glow_spread": ("shader_glow_spread", "pyro_glow_spread"),
    "glow_shadowing": ("shader_glow_shadowing", "pyro_glow_shadowing"),
}


def is_pyro_look(settings):
    return settings is not None and str(getattr(settings, "shader_look", "TEMPERATURE")) == "PYRO"


def glow_source_heat(settings):
    """Glow Source resolved: True = Warm Gas (Houdini pyro's scatter source),
    False = Fire Brightness. Auto follows the Look: Warm Gas for Pyro."""
    source = str(getattr(settings, "glow_source", "AUTO")) if settings is not None else "AUTO"
    return source == "HEAT" or (source == "AUTO" and is_pyro_look(settings))


def flame_value(settings, key, default, pyro=None):
    """A flame control of the active Look (or of the given one: pyro True/False)."""
    if settings is None:
        return default
    use_pyro = is_pyro_look(settings) if pyro is None else pyro
    return getattr(settings, FLAME_VALUE_KEYS[key][1 if use_pyro else 0], default)


def physical_buoyancy(props):
    """Temperature Buoyancy (in g, at Flame Temperature) from the gas law:
    hot gas is T / T_air times lighter, so a parcel lifts at g (T - T_air) / T_air."""
    return max(0.0, (props.flame_temperature - AMBIENT_AIR_KELVIN) / AMBIENT_AIR_KELVIN)


def addon_directory():
    return os.path.dirname(os.path.abspath(__file__))


def executable_path():
    name = "fumaris_bridge.exe" if sys.platform == "win32" else "fumaris_bridge"
    prefs = get_addon_preferences()
    if prefs and getattr(prefs, "executable_path", ""):
        return bpy.path.abspath(prefs.executable_path)
    return os.path.join(addon_directory(), "bin", name)


def required_flow_libraries():
    if sys.platform == "win32":
        return ("nvflow.dll", "nvflowext.dll")
    if sys.platform == "darwin":
        return ("libnvflow.dylib", "libnvflowext.dylib", "libMoltenVK.dylib")
    return ("libnvflow.so", "libnvflowext.so")


def runtime_validation_error():
    executable = executable_path()
    if not executable or not os.path.isfile(executable):
        return f"Fumaris bridge executable is missing: {executable}"
    prefs = get_addon_preferences()
    if prefs and getattr(prefs, "executable_path", ""):
        if sys.platform != "win32" and not os.access(executable, os.X_OK):
            return f"Custom Fumaris bridge is not executable: {executable}"
        return ""
    permission_error = _repair_bundled_executable(executable)
    if permission_error:
        return permission_error
    libs = os.path.join(addon_directory(), "bin", "libs")
    missing = [
        name for name in required_flow_libraries()
        if not os.path.isfile(os.path.join(libs, name))
    ]
    if missing:
        return "Fumaris runtime is missing: " + ", ".join(missing)
    return ""


def _repair_bundled_executable(executable):
    if sys.platform == "win32" or os.access(executable, os.X_OK):
        return ""
    try:
        mode = os.stat(executable).st_mode
        os.chmod(executable, mode | stat.S_IXUSR)
    except OSError as error:
        return f"Fumaris could not make its bridge executable: {error}"
    if not os.access(executable, os.X_OK):
        return (
            "Fumaris bridge execution is blocked by the filesystem: "
            f"{executable}"
        )
    return ""


def process_environment(executable=None):
    env = os.environ.copy()
    binary = (
        os.path.dirname(os.path.abspath(executable))
        if executable
        else os.path.join(addon_directory(), "bin")
    )
    libs = os.path.join(binary, "libs")
    variable = (
        "PATH" if sys.platform == "win32" else
        "DYLD_LIBRARY_PATH" if sys.platform == "darwin" else "LD_LIBRARY_PATH"
    )
    previous = env.get(variable, "")
    paths = os.pathsep.join((libs, binary))
    env[variable] = paths if not previous else paths + os.pathsep + previous
    env["FUMARIS_PARENT_PID"] = str(os.getpid())
    if sys.platform.startswith("linux"):
        from .linux_runtime import prefer_system_gcc
        env = prefer_system_gcc(executable or os.path.join(binary, "fumaris_bridge"), env)
    return env


def _safe_cache_slot(value):
    value = (value or "main").strip()
    value = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    ).strip("._")
    return value or "main"


def base_output_directory_for_object(obj):
    configured = obj.fumaris.output_dir.strip()
    if not configured:
        configured = "//fumaris_cache/"
    return _absolute_cache_directory(configured, "fumaris_cache")


def _absolute_cache_directory(configured, fallback_name, *, create=True):
    # Blender can turn // paths into absolute paths even without a .blend
    # file (including an unwritable installation/root directory on Windows).
    # Decide whether the input needs a project anchor before resolving it.
    if not bpy.data.filepath and (
        configured.startswith("//") or not os.path.isabs(configured)
    ):
        try:
            user_cache = bpy.utils.user_resource(
                "DATAFILES",
                path=fallback_name,
                create=create,
            )
        except OSError:
            user_cache = ""
        if user_cache and os.path.isabs(user_cache):
            return os.path.normpath(user_cache)
        return os.path.join(tempfile.gettempdir(), fallback_name)
    resolved = bpy.path.abspath(configured)
    return os.path.normpath(os.path.abspath(resolved))


def scene_file_path(path):
    """Absolute path of a "//" path stored in a Fumaris setting. A background
    bake reads a snapshot in another folder: there the path is still relative
    to the scene's own .blend file."""
    background = background_bake()
    if background is not None:
        return bpy.path.abspath(path, start=background["blend_directory"])
    return bpy.path.abspath(path)


def cache_identity_for_object(obj):
    props = obj.fumaris
    identity = props.cache_id.strip()
    # A duplicated domain copies cache_id. Only a newer copy takes a fresh
    # identity, so the original keeps its bake whichever is queried first.
    duplicate = any(
        other is not obj
        and hasattr(other, "fumaris")
        and getattr(other.fumaris, "cache_id", "").strip() == identity
        and getattr(other, "session_uid", 0) < getattr(obj, "session_uid", 0)
        for other in bpy.data.objects
    ) if identity else False
    if not identity or duplicate:
        # Random, not a hash of the name: two .blend files in one folder (or
        # unsaved files, which share the profile's folder) with a domain of the
        # same name got the same cache folder, and Replace in one deleted the
        # other's bake (beta 3 review, 2026-11-19). An identity is stored once
        # and kept, so existing caches are still found.
        name = getattr(obj, "name_full", obj.name)
        identity = hashlib.sha256(f"{name}|{uuid.uuid4().hex}".encode("utf-8")).hexdigest()[:24]
        props.cache_id = identity
    return identity


_CACHE_OWNER = ".fumaris_owner"
_UNSAVED_OWNER = uuid.uuid4().hex


def cache_project_key():
    if not bpy.data.filepath:
        return _UNSAVED_OWNER
    path = os.path.normcase(os.path.realpath(bpy.data.filepath))
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def _read_cache_owner(root):
    try:
        with open(os.path.join(root, _CACHE_OWNER), encoding="ascii") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


def owns_cache(directory):
    """Does this file have write access to this already-resolved slot?"""
    return _read_cache_owner(os.path.dirname(directory)) == cache_project_key()


def _repoint_cache_volumes(old, new):
    from .importers import VOLUME_MARKER

    def mapped(path):
        source = os.path.normcase(os.path.abspath(path))
        if source.startswith(os.path.normcase(old) + os.sep):
            return os.path.join(new, os.path.relpath(path, old))
        return None

    for obj in bpy.data.objects:
        marker = obj.get(VOLUME_MARKER)
        if not marker or obj.type != "VOLUME" or obj.library is not None:
            continue
        new_marker = mapped(marker)
        if new_marker is None:
            continue
        obj[VOLUME_MARKER] = new_marker
        volume = obj.data
        original = volume.filepath
        path = mapped(bpy.path.abspath(original, library=volume.library))
        if path is not None:
            if original.startswith("//"):
                try:
                    path = bpy.path.relpath(path)
                except ValueError:
                    pass
            volume.filepath = path


def ensure_cache_owner(domain):
    """Isolate disk writes from a Save As or a separately copied .blend.

    Reads can continue to use the inherited cache. The first write copies its
    versions and then rebinds the scene to the new cache identity. A populated
    legacy cache has no verifiable owner, so it is also copied before writing.
    """
    from .cache import GUARD_FILE, LOCK_FILE, RefusedError, cache_lock
    from .cache_revision import JOURNAL, WORK

    if background_bake() is not None:
        return  # The parent selected and reserved the worker's output.
    directory = output_directory_for_object(domain)
    root = os.path.dirname(directory)
    owner = cache_project_key()
    if _read_cache_owner(root) == owner:
        return

    infrastructure = {GUARD_FILE, LOCK_FILE, _CACHE_OWNER, WORK}
    with cache_lock(root):
        previous = _read_cache_owner(root)
        if previous == owner:
            return
        if not previous and not any(
            name not in infrastructure
            for _path, _dirs, names in os.walk(root) for name in names
        ):
            with open(os.path.join(root, _CACHE_OWNER), "w", encoding="ascii") as stream:
                stream.write(owner)
            return

        # The new identity becomes visible only after the complete copy.
        identity = uuid.uuid4().hex[:24]
        new = os.path.join(os.path.dirname(root), identity)
        with tempfile.TemporaryDirectory(dir=os.path.dirname(root), prefix=".fumaris_fork_") as scratch:
            stage = os.path.join(scratch, "cache")
            os.mkdir(stage)

            def ignored(_folder, names):
                return [name for name in names if name in infrastructure]

            with os.scandir(root) as entries:
                for entry in entries:
                    if entry.name in infrastructure:
                        continue
                    dest = os.path.join(stage, entry.name)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.startswith("."):  # snapshots, playblasts
                            shutil.copytree(entry.path, dest, ignore=ignored, symlinks=True)
                        else:
                            with cache_lock(entry.path):
                                if os.path.isfile(os.path.join(entry.path, WORK, JOURNAL)):
                                    raise RefusedError(
                                        f"Version '{entry.name}' has an interrupted replacement. "
                                        "Open its original Blender file to recover it before copying.")
                                shutil.copytree(entry.path, dest, ignore=ignored, symlinks=True)
                    elif entry.is_file(follow_symlinks=False):
                        shutil.copy2(entry.path, dest)
            # Import-only sequence links in older caches can point at the
            # original frame by absolute path. Rebase those links; avoid
            # carrying references to files outside the cache being copied.
            for folder, dirs, files in os.walk(stage):
                for name in (*dirs, *files):
                    path = os.path.join(folder, name)
                    if os.path.islink(path):
                        source = os.path.join(root, os.path.relpath(path, stage))
                        linked = os.path.abspath(os.path.join(os.path.dirname(source), os.readlink(path)))
                        os.unlink(path)
                        if linked.startswith(root + os.sep):
                            clone = os.path.join(stage, os.path.relpath(linked, root))
                            os.symlink(os.path.relpath(clone, folder), path)
            with open(os.path.join(stage, _CACHE_OWNER), "w", encoding="ascii") as stream:
                stream.write(owner)
            os.rename(stage, new)
    domain.fumaris.cache_id = identity
    _repoint_cache_volumes(root, new)
    key = domain.fumaris.compare_selected
    kind, _, path = key.partition(":")
    if kind in {"img", "blast"} and path.startswith(root + os.sep):
        domain.fumaris.compare_selected = kind + ":" + os.path.join(new, os.path.relpath(path, root))
    from .compare import forget_scan
    from .particles import follow_bakes as follow_particles
    from .sparks import follow_bakes as follow_sparks
    follow_particles()
    follow_sparks()
    forget_scan()


def output_directory_for_object(obj):
    return os.path.join(
        base_output_directory_for_object(obj),
        ".fumaris_caches",
        cache_identity_for_object(obj),
        _safe_cache_slot(getattr(obj.fumaris, "cache_slot", "main")),
    )


def cache_slot_directory(obj, slot):
    """Folder of another slot of this domain's cache."""
    return os.path.join(os.path.dirname(output_directory_for_object(obj)), _safe_cache_slot(slot))


def peek_output_directory_for_object(obj):
    """output_directory_for_object for draw code: creates nothing, writes no property.

    Returns None for a domain that has never been given a cache identity,
    which also means it has nothing baked.
    """
    props = obj.fumaris
    identity = props.cache_id.strip()
    if not identity:
        return None
    configured = props.output_dir.strip() or "//fumaris_cache/"
    return os.path.join(
        _absolute_cache_directory(configured, "fumaris_cache", create=False),
        ".fumaris_caches",
        identity,
        _safe_cache_slot(getattr(props, "cache_slot", "main")),
    )


def _domain_scene(domain):
    scenes = getattr(domain, "users_scene", ())
    current = bpy.context.scene
    if current is not None and (not scenes or current in scenes):
        return current
    return scenes[0] if scenes else current


def simulation_frame_range(domain):
    props = domain.fumaris
    if getattr(props, "use_scene_frame_range", False):
        # Follow the timeline, starting early enough that the smoke is
        # already moving on the first visible frame.
        scene = _domain_scene(domain)
        warmup = max(0, int(getattr(props, "warmup_frames", 0)))
        start = int(scene.frame_start) - warmup
        end = int(scene.frame_end)
    else:
        start = int(getattr(props, "sim_start_frame", 1))
        end = int(getattr(props, "sim_end_frame", start))
    return start, max(start, end)


def get_addon_preferences():
    background = background_bake()
    if background is not None:
        # A background bake's Blender runs with factory settings: the main
        # process passed the preferences a bake reads.
        return background["preferences"]
    addon_name = __package__
    addon = bpy.context.preferences.addons.get(addon_name)
    return addon.preferences if addon else None
