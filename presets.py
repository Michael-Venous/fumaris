"""User presets: save and reuse the settings of a domain or a source.

A preset stores every setting of one role (domain, or
emitter/collider/effector), as JSON in the extension's user folder, so it
survives add-on updates and is shared between .blend files. Applying it
restores that state: settings the file does not hold go back to their defaults.
"""
import json
import os
import re

import bpy

from . import detail_stack
from .cache import _rna_values

# Settings that belong to sources rather than the domain (one property group
# holds both). Identity and per-object wiring are never stored.
_SOURCE_PREFIXES = ("emitter_", "couple_rate_", "source_op_", "normal_velocity", "mesh_emission_",
                    "point_", "attr_", "collider_", "effector_", "outflow_", "motion_",
                    "participant_enabled", "physics_", "particle_subtype", "sphere_",
                    "volume_file", "volume_frame_offset", "volume_grid_", "volume_use_")
_SOURCE_NAMES = {"velocity", "mesh_velocity_attribute"}
# The frame range belongs to the scene, not the look.
_NEVER = {"smoke_object_type", "participant_type", "particle_system_name", "gn_subtype",
          "cache_slot", "cache_id", "output_dir", "output_prefix", "simulation_state",
          "baked_frames", "bake_elapsed", "bake_peak_vram_mb", "bake_peak_ram_mb",
          "sim_start_frame", "sim_end_frame", "use_scene_frame_range",
          # Full-Resolution Channels' old name (full_resolution_channels holds it)
          "full_resolution_velocity"}
# Compare section state (which presets and slots are being compared).
_NEVER_PREFIXES = ("compare_",)


def preset_role(props):
    return "domain" if props.smoke_object_type == "domain" else "source"


def _belongs(name, role):
    if name in _NEVER or "collection" in name or name.startswith(_NEVER_PREFIXES):
        return False
    source = name in _SOURCE_NAMES or name.startswith(_SOURCE_PREFIXES)
    return source if role == "source" else not source


def presets_directory(role, create=False):
    base = bpy.utils.extension_path_user(__package__, path="presets", create=create)
    path = os.path.join(base, role)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def list_presets(role):
    directory = presets_directory(role)
    if not os.path.isdir(directory):
        return []
    return sorted(name[:-5] for name in os.listdir(directory) if name.endswith(".json"))


def _safe(name):
    return re.sub(r"[^A-Za-z0-9 _.-]", "_", name).strip() or "Preset"


def capture(props):
    """This object's preset settings as plain data (what a preset file holds)."""
    role = preset_role(props)
    values = {key: value for key, value in _rna_values(props).items() if _belongs(key, role)}
    data = {"version": 2, "role": role, "settings": values}
    if role == "domain":
        data["detail_layers"] = detail_stack.to_list(props)
    return data


def _write(data, name):
    path = os.path.join(presets_directory(data["role"], create=True), _safe(name) + ".json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=1, sort_keys=True)
    return path, len(data["settings"])


def save_preset(props, name):
    return _write(capture(props), name)


def save_preset_data(snapshot, name):
    """A domain preset from a version's stored settings (cache.write_settings_snapshot)."""
    settings = {key: value for key, value in snapshot.get("settings", {}).items() if _belongs(key, "domain")}
    data = {"version": 2, "role": "domain", "settings": settings,
            "detail_layers": snapshot.get("detail_layers", [])}
    return _write(data, name)


def load_preset(role, name):
    path = os.path.join(presets_directory(role), _safe(name) + ".json")
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def _default(prop):
    if prop.type == "ENUM":
        return set(prop.default_flag) if prop.is_enum_flag else prop.default
    if getattr(prop, "array_length", 0):
        return list(prop.default_array)
    return prop.default


def apply_preset(props, name):
    return apply_data(props, load_preset(preset_role(props), name))


def apply_data(props, data):
    role = preset_role(props)
    saved = dict(data.get("settings", {}))
    # Presets from before Full-Resolution Velocity became Full-Resolution Channels
    if "full_resolution_velocity" in saved and "full_resolution_channels" not in saved:
        saved["full_resolution_channels"] = saved["full_resolution_velocity"]
    settings = dict(saved)
    for key in _rna_values(props):
        if _belongs(key, role) and key not in settings:
            settings[key] = _default(props.bl_rna.properties[key])
    applied = 0
    retry = []
    for key, value in settings.items():
        if not _belongs(key, role) or not hasattr(props, key):
            continue
        if _set(props, key, value):
            applied += 1
        else:
            retry.append((key, value))
    # Choices that depend on another setting (Velocity Resolution offers Full
    # only with Full-Resolution Channels) are accepted once it is applied.
    for key, value in retry:
        if _set(props, key, value):
            applied += 1
        elif key not in saved:
            # A dynamic enum has no default value to set; reset it.
            props.property_unset(key)
            applied += 1
    if role == "domain":
        detail_stack.from_list(props, data.get("detail_layers", []))
        applied += 1
    return applied


def _set(props, key, value):
    try:
        setattr(props, key, set(value) if isinstance(getattr(props, key), set) else value)
        return True
    except (TypeError, ValueError, AttributeError):
        return False


def delete_preset(props, name):
    path = os.path.join(presets_directory(preset_role(props)), _safe(name) + ".json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
