"""User presets: save and reuse the settings of a domain or a source.

A preset stores every setting of one role (domain, or
emitter/collider/effector), as JSON in the extension's user folder, so it
survives add-on updates and is shared between .blend files. Applying it
restores that state: settings the file does not hold (older presets kept only
non-default values) go back to their defaults.
"""
import json
import os
import re

import bpy

from .cache import _rna_values

# Settings that belong to sources rather than the domain (one property group
# holds both). Identity and per-object wiring are never stored.
_SOURCE_PREFIXES = ("emitter_", "couple_rate_", "source_op_", "normal_velocity", "mesh_emission_",
                    "point_", "attr_", "collider_", "effector_", "outflow_", "motion_",
                    "participant_enabled", "physics_", "particle_subtype", "sphere_",
                    "volume_file", "volume_frame_offset", "volume_grid_", "volume_use_")
_SOURCE_NAMES = {"velocity", "velocity_scale", "mesh_velocity_attribute"}
# The frame range belongs to the scene, not the look.
_NEVER = {"smoke_object_type", "participant_type", "particle_system_name", "gn_subtype",
          "cache_slot", "cache_id", "output_dir", "output_prefix", "simulation_state",
          "baked_frames", "bake_elapsed", "bake_peak_vram_mb", "bake_peak_ram_mb",
          "sim_start_frame", "sim_end_frame", "use_scene_frame_range"}


def preset_role(props):
    return "domain" if props.smoke_object_type == "domain" else "source"


def _belongs(name, role):
    if name in _NEVER or name.endswith("_version") or "collection" in name:
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


def save_preset(props, name):
    role = preset_role(props)
    values = {key: value for key, value in _rna_values(props).items() if _belongs(key, role)}
    path = os.path.join(presets_directory(role, create=True), _safe(name) + ".json")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump({"version": 2, "role": role, "settings": values}, stream, indent=1, sort_keys=True)
    return path, len(values)


def _default(prop):
    if prop.type == "ENUM":
        return set(prop.default_flag) if prop.is_enum_flag else prop.default
    if getattr(prop, "array_length", 0):
        return list(prop.default_array)
    return prop.default


def apply_preset(props, name):
    role = preset_role(props)
    path = os.path.join(presets_directory(role), _safe(name) + ".json")
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    settings = dict(data.get("settings", {}))
    for key in _rna_values(props):
        if _belongs(key, role) and key not in settings:
            settings[key] = _default(props.bl_rna.properties[key])
    applied = 0
    for key, value in settings.items():
        if not _belongs(key, role) or not hasattr(props, key):
            continue
        try:
            setattr(props, key, set(value) if isinstance(getattr(props, key), set) else value)
            applied += 1
        except (TypeError, ValueError, AttributeError):
            continue
    return applied


def delete_preset(props, name):
    path = os.path.join(presets_directory(preset_role(props)), _safe(name) + ".json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
