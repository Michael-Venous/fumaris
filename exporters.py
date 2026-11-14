import os
import re
import sys
from array import array
from dataclasses import dataclass

import math

import bpy
from mathutils import Matrix, Vector

try:
    import numpy as np
except ImportError:
    np = None

from . import detail_stack
from .diagnostics import console_log
from .memory_auto import AUTO_FRACTION
from .collection_export import collection_mesh_arrays, collection_point_arrays
from .mesh_export import (
    _add_mesh_initial_velocity,
    _add_mesh_motion_velocities,
    _cached_or_evaluated_mesh,
    world_surface,
    _geometry_nodes_mesh,
    _particle_mesh,
    _static_mesh_cache_key,
    uses_velocity_attribute,
)
from .participants import (
    collection_signature as _collection_signature,
    is_geometry_nodes as _is_geometry_nodes,
    is_particles as _is_particles,
    participant_shape as _participant_shape,
    participants as _participants,
)
from .point_export import _empty_point_arrays, _point_arrays, _world_vector
from .protocol import DISPLAY_LUT_KEY, pack_preview_payload, split_scene_depth
from .utils import (
    executable_path,
    flame_value,
    output_directory_for_object,
    physical_buoyancy,
    scene_file_path,
    simulation_frame_range,
)

_FRAME_ZERO_VERSION = 1 << 40
_SEQUENCE_NAME = re.compile(r"^(.*?)(-?)(\d+)(\.[^.]+)$")
# Bit order of Flow's density channels in the bridge's value_ops.
_SOURCE_OPERATION_CHANNELS = ("temperature", "fuel", "burn", "smoke")
# Coupling that reaches the target within any solver step: Replace.
_REPLACE_COUPLE_RATE = 1.0e6


@dataclass(frozen=True)
class FramePacket:
    header: dict
    payload: bytes | bytearray


_VDB_WRITERS = {"FASTEST": (0, 0), "BALANCED": (6, 0), "LOW": (4, 0), "ULTRA": (1, 1)}
# Export Resolution menus -> the writer's averaging factor.
_EXPORT_DOWNSAMPLE = {"FULL": 1, "HALF": 2, "QUARTER": 4}


def _velocity_downsample(props):
    """Factor on the stored velocity grid, which is at half resolution unless
    Full-Resolution Velocity is on; Half and Quarter are relative to density."""
    target = _EXPORT_DOWNSAMPLE.get(getattr(props, "export_velocity_resolution", "HALF") or "HALF", 2)
    native = 1 if getattr(props, "full_resolution_velocity", False) else 2
    return max(1, target // native)


def bridge_executable():
    executable = executable_path()
    return executable if executable and os.path.isfile(executable) else None


def simulation_memory_mb(props):
    """Simulation Memory in MiB: the bridge sizes the block pool from it."""
    return float(props.simulation_memory) * 1024.0


def _preview_cache_budget_mb():
    """Preview Cache RAM (preferences) in MB; 0 lets the bridge choose."""
    from .utils import get_addon_preferences
    prefs = get_addon_preferences()
    return max(0.0, float(getattr(prefs, "preview_cache_memory_gb", 0.0) or 0.0)) * 1024.0


def build_session(
    context,
    start_frame=None,
    end_frame=None,
    domain=None,
    *,
    write_vdb=True,
    preview_enabled=True,
    resolution_scale=1.0,
    log_participants=False,
):
    scene = context.scene
    domain = domain or context.object
    sim_start, sim_end = simulation_frame_range(domain)
    participants = _participants(domain.fumaris)
    if not any(item["role"] == "emitter" for item in participants):
        from .cache import RefusedError
        raise RefusedError("Assign an emitter collection containing at least one Fumaris emitter")

    if log_participants:
        _log_participants(domain, participants)

    settings = {
        "schema_version": 1,
        "start_frame": sim_start if start_frame is None else start_frame,
        "end_frame": sim_end if end_frame is None else end_frame,
        "fps": scene.render.fps / scene.render.fps_base,
        "output_directory": output_directory_for_object(domain),
        "output_prefix": domain.fumaris.output_prefix or "fumaris_",
        "vdb_compression": getattr(domain.fumaris, "vdb_compression", "active_mask"),
        "vdb_precision": getattr(domain.fumaris, "vdb_precision", "half"),
        # VDB Writers: (most writers, frames converted at once; 0 = no limit).
        **dict(zip(("vdb_writer_limit", "vdb_writer_frames"),
                   _VDB_WRITERS.get(getattr(domain.fumaris, "vdb_writers", "BALANCED"), (6, 0)))),
        "write_vdb": bool(write_vdb),
        "preview_enabled": bool(preview_enabled),
        # Preview cache: a live preview keeps its frames for scrubbing.
        "preview_cache": bool(preview_enabled and not write_vdb and getattr(domain.fumaris, "preview_cache", False)),
        # Cache Every: a fixed step copies each kept frame whole; Auto paces it.
        "preview_cache_every_frame": getattr(domain.fumaris, "preview_cache_step", "1") != "AUTO",
        "preview_cache_step": (0 if getattr(domain.fumaris, "preview_cache_step", "1") == "AUTO"
                               else int(getattr(domain.fumaris, "preview_cache_step", "1"))),
        "preview_cache_budget_mb": _preview_cache_budget_mb(),
        # Velocity compresses poorly: kept only for velocity export and the
        # velocity-based preview views.
        "preview_cache_velocity": bool(getattr(domain.fumaris, "export_velocity_vdb", False)
                                       or getattr(domain.fumaris, "preview_field_view", "look") in {"speed", "expansion"}),
        "flow_profile_enabled": os.environ.get("FUMARIS_FLOW_PROFILE", "").strip().lower()
        in {"1", "true", "yes", "on"},
        "initial_domain": _domain_state(domain.fumaris, resolution_scale),
    }
    # Effectors add a velocity scratch texture after the pressure solve.
    settings["initial_domain"]["effector_sources"] = any(item["kind"] == "effector" for item in participants)
    settings["initial_domain"]["density_channels"] = density_channels(domain.fumaris, participants)
    return settings, participants


def density_channels(domain_props, participants):
    """Density channels the bake stores: 1 smoke, 2 smoke + temperature, 4 all.

    Fuel, burning emitters or Full-Resolution Velocity need all four; heat
    alone needs temperature; otherwise only smoke is kept (more capacity per GB).
    """
    if getattr(domain_props, "density_storage", "auto") != "auto":
        return 4
    # Full-Resolution Velocity shares Flow's density level, where narrow
    # storage would also narrow velocity (the bridge keeps four channels).
    if getattr(domain_props, "full_resolution_velocity", False):
        return 4
    # Colour Mixing's attribute field shares the density level's layout.
    if getattr(domain_props, "color_mixing", False):
        return 4
    heat = False
    for participant in participants:
        if participant.get("role") != "emitter":
            continue
        obj = participant["object"]
        props = obj.fumaris
        # Keyed or driven values count as present: the channels are fixed
        # for the session, and one that starts at 0 would never simulate.
        animated = _animated_paths(obj)
        if (props.emitter_fuel > 0.0 or props.emitter_burn > 0.0
                or getattr(props, "emitter_burning", False)
                or animated & {"fumaris.emitter_fuel", "fumaris.emitter_burn", "fumaris.emitter_burning"}):
            return 4
        if props.emitter_temperature != 0.0 or "fumaris.emitter_temperature" in animated:
            heat = True
    return 2 if heat else 1


def _animated_paths(obj):
    """Data paths of obj that have F-Curves or drivers."""
    paths = set()
    anim = getattr(obj, "animation_data", None)
    if anim is None:
        return paths
    paths.update(driver.data_path for driver in anim.drivers)
    action = anim.action
    if action is None:
        return paths
    # Layered actions (Blender 4.4+): every channelbag; older: action.fcurves.
    fcurve_sets = []
    for layer in getattr(action, "layers", ()):
        for strip in layer.strips:
            fcurve_sets.extend(bag.fcurves for bag in getattr(strip, "channelbags", ()))
    if not fcurve_sets and hasattr(action, "fcurves"):
        fcurve_sets.append(action.fcurves)
    for fcurves in fcurve_sets:
        paths.update(fcurve.data_path for fcurve in fcurves)
    return paths


def session_structure_signature(domain):
    props = domain.fumaris
    sim_start, sim_end = simulation_frame_range(domain)
    participants = _participants(props)
    return (
        # Channel storage is fixed per session: a change (an emitter gaining
        # heat or fuel) starts a new one instead of dropping the channel.
        density_channels(props, participants),
        simulation_memory_mb(props),
        bool(getattr(props, "simulation_memory_auto", False)),
        bool(getattr(props, "full_resolution_velocity", False)),
        bool(getattr(props, "advection_reflection", False)),
        bool(props.follow_object),
        sim_start,
        sim_end,
        _collection_signature(props.emitter_collection),
        _collection_signature(props.collider_collection),
        _collection_signature(props.effector_collection),
        _collection_signature(props.outflow_collection),
        tuple(
            (
                item["role"],
                item["kind"],
                item["collection"],
                item["object"].name,
                item["object"].type,
                _participant_shape(item["object"].fumaris),
                getattr(item["object"].fumaris, "gn_subtype", ""),
                getattr(item["object"].fumaris, "particle_subtype", ""),
                getattr(item["object"].fumaris, "collider_type", ""),
                (
                    _collection_signature(item["object"].fumaris.source_collection),
                    item["object"].fumaris.collection_point_center,
                ) if _participant_shape(item["object"].fumaris) in {"collection_points", "collection_mesh"} else None,
            )
            for item in participants
        ),
    )


def build_frame(*args, **kwargs):
    global _BURN_DOMAIN, _RESOLUTION_SCALE
    try:
        return _build_frame(*args, **kwargs)
    finally:
        # Held only while this frame is built: a removed domain's settings
        # read later through a stale pointer crash Blender (SIGSEGV,
        # 2026-11-14).
        _BURN_DOMAIN = None
        _RESOLUTION_SCALE = 1.0


def _build_frame(
    context,
    frame,
    participants,
    domain=None,
    resolution_scale=1.0,
    volume_stage_dir=None,
    preview_enabled=True,
    volume_preview=None,
):
    global _BURN_DOMAIN, _RESOLUTION_SCALE
    owner = domain or getattr(context, "object", None)
    _BURN_DOMAIN = getattr(owner, "fumaris", None)
    _RESOLUTION_SCALE = max(1e-12, float(resolution_scale))
    depsgraph = context.evaluated_depsgraph_get()
    payload = bytearray()
    preview_request = volume_preview or {"valid": False}
    preview_mode = "volume" if preview_enabled and preview_request.get("valid", False) else "none"
    if preview_mode == "volume":
        preview_request = pack_preview_payload(preview_request, payload)
    else:
        preview_request, _depth = split_scene_depth(preview_request)
        preview_request = {key: value for key, value in preview_request.items() if key != DISPLAY_LUT_KEY}
    meshes = []
    boxes = []
    spheres = []
    sphere_clouds = []
    effectors = []
    volumes = []

    # Motion measured per frame is in scene time; at Simulation Speed s a frame
    # spans s frames of simulated time, so it is divided by s.
    speed = max(1e-4, float(getattr(getattr(owner, "fumaris", None), "simulation_speed", 1.0) or 1.0))
    for participant in participants:
        participant["simulation_speed"] = speed
        kind = participant["kind"]
        if kind == "mesh":
            meshes.append(_mesh_state(participant, depsgraph, payload, frame))
        elif kind == "box":
            boxes.append(_box_state(participant, depsgraph))
        elif kind == "sphere":
            spheres.append(_sphere_state(participant, depsgraph))
        elif kind == "point_spheres":
            sphere_clouds.append(_sphere_cloud_state(participant, depsgraph, payload, frame))
        elif kind == "effector":
            effectors.append(_effector_state(participant, depsgraph))
        elif kind == "volume":
            volumes.append(
                _volume_state(
                    context,
                    participant,
                    depsgraph,
                    frame,
                    volume_stage_dir,
                )
            )
    _warn_extreme_motion(domain or context.object, participants, resolution_scale)

    return FramePacket(
        {
            "frame": frame,
            "preview_mode": preview_mode,
            "volume_preview": preview_request,
            "domain": {
                **_domain_state((domain or context.object).fumaris, resolution_scale),
                **_domain_frame(domain or context.object, depsgraph),
            },
            "meshes": meshes,
            "boxes": boxes,
            "spheres": spheres,
            "sphere_clouds": sphere_clouds,
            "points": [],
            "effectors": effectors,
            "volumes": volumes,
        },
        payload,
    )



# Particle Mesh mode sends every instance's triangles every frame and Flow
# rasterizes each one; above this the emitter pass dominates the step.
HEAVY_PARTICLE_TRIANGLES = 1_000_000

# A source jumping farther than this many voxels in one frame is usually a
# teleport or keyframe slip: it smears smoke along the jump and costs many
# motion stamps. Named once per session in the Diagnostics log.
EXTREME_MOTION_VOXELS = 32.0


def _warn_extreme_motion(domain, participants, resolution_scale=1.0):
    """Diagnostics warnings for sources that jump or are very heavy (once each)."""
    props = getattr(domain, "fumaris", None)
    if props is None:
        return
    voxel = 32.0 / max(1.0, float(props.resolution) * float(resolution_scale))
    for participant in participants:
        triangles = participant.get("instanced_triangles", 0)
        if triangles > HEAVY_PARTICLE_TRIANGLES and not participant.get("cost_warned"):
            participant["cost_warned"] = True
            from . import diagnostics
            diagnostics.add_log(
                domain,
                f"Warning: {participant['object'].name} sends {triangles / 1e6:.1f} million particle "
                "mesh triangles per frame; Point Cloud mode or a simpler instance mesh is much faster")
        travel = participant.pop("frame_travel", 0.0)
        if travel <= EXTREME_MOTION_VOXELS * voxel or participant.get("motion_warned"):
            continue
        participant["motion_warned"] = True
        from . import diagnostics
        name = participant["object"].name
        diagnostics.add_log(
            domain,
            f"Warning: {name} moved {travel / voxel:.0f} voxels in one frame; "
            "check for a jump in its animation (it smears smoke and slows the simulation)")


def _log_participants(domain, participants):
    console_log(f"Fumaris participants for {domain.name}:")
    for item in participants:
        obj = item["object"]
        console_log(
            "  "
            f"#{item['id']} {item['role']}/{item['kind']} "
            f"{obj.name} collection={item.get('collection', '')} "
            f"enabled={bool(obj.fumaris.participant_enabled)}"
        )


def _domain_frame(domain, depsgraph):
    """Where the simulation grid is this frame when it moves with its object."""
    props = domain.fumaris
    if not getattr(props, "follow_object", False):
        return {}
    matrix = domain.evaluated_get(depsgraph).matrix_world
    # Location and rotation only: a scaled grid would change the voxel size.
    rigid = matrix.to_quaternion().to_matrix().to_4x4()
    rigid.translation = matrix.translation
    return {
        "follow_object": True,
        "carry_air": min(1.0, max(0.0, float(props.carry_air))),
        "frame_matrix": [value for row in rigid for value in row],
    }


def _domain_state(props, resolution_scale=1.0):
    # The bridge protocol stores resolution as a signed 32-bit integer.
    resolution = max(1, min(2147483647, int(round(props.resolution * max(0.0, float(resolution_scale))))))
    generated_material = props.volume_material is None
    return {
        "resolution": resolution,
        "simulation_speed": props.simulation_speed,
        "simulation_scale": float(getattr(props, "simulation_scale", 1.0)),
        "num_sub_steps": props.num_sub_steps,
        # Cells per Step 0 is off.
        "adaptive_sub_steps": props.adaptive_cfl > 0.0,
        "adaptive_cfl": props.adaptive_cfl,
        "full_resolution_velocity": props.full_resolution_velocity,
        "color_mixing": bool(getattr(props, "color_mixing", False)),
        "simulation_memory_mb": simulation_memory_mb(props),
        "simulation_memory_auto": bool(getattr(props, "simulation_memory_auto", False)),
        "simulation_memory_auto_fraction": AUTO_FRACTION,
        "physics_convex_collision": props.physics_convex_collision,
        "sparse_block_min_lifetime": props.sparse_block_min_lifetime,
        "auto_cell_size": props.auto_cell_size,
        "allocation_smoke_threshold": props.allocation_smoke_threshold,
        "allocation_speed_threshold": props.allocation_speed_threshold,
        "air_margin": max(0.0, float(props.air_margin)),
        "gravity": list(props.gravity),
        "wind": list(props.wind),
        "buoyancy_per_temp": physical_buoyancy(props) if props.physical_buoyancy else props.buoyancy_per_temp,
        "buoyancy_per_smoke": props.buoyancy_per_smoke,
        "vorticity": props.vorticity,
        "vorticity_mask": props.vorticity_mask,
        # Detail & Shaping stack (detail_stack.py).
        "detail_layers": detail_stack.export_layers(props),
        "wisp_cooling": props.wisp_cooling,
        "fuel_threshold": props.fuel_threshold,
        "advection_reflection": props.advection_reflection,
        "gas_expansion": props.gas_expansion,
        "soot_radiation": props.soot_radiation,
        "temperature_diffusion": props.temperature_diffusion,
        "conserve_smoke": bool(getattr(props, "conserve_smoke", True)),
        "trace_steps": int(getattr(props, "trace_steps", 0)),
        "collider_face_flux": bool(getattr(props, "collider_face_flux", False)),
        "oxygen_limit": props.oxygen_limit,
        "oxygen_field": bool(props.oxygen_field),
        "oxygen_use": float(props.oxygen_use),
        "oxygen_mixing": float(props.oxygen_mixing),
        "smoke_detail": getattr(props, "smoke_detail", "standard"),
        "edge_sharpness": float(getattr(props, "edge_sharpness", 0.9)),
        "projection_filter": float(getattr(props, "projection_filter", 1.0)),
        "open_collider_wall": float(getattr(props, "open_collider_wall", 1.0)),
        "closed_collider_margin": float(getattr(props, "closed_collider_margin", 0.0)),
        "fuel_energy": float(getattr(props, "fuel_energy", 1.0)),
        "dissipation": props.dissipation,
        "smoke_decay": props.smoke_decay,
        "velocity_damping": props.velocity_damping,
        "flame_temperature": props.flame_temperature,
        "burn_time": props.burn_time,
        "cooling_time": props.cooling_time,
        "ignition_temperature_k": props.ignition_temperature_k,
        "smoke_per_burn": props.smoke_per_burn,
        "export_temperature_vdb": props.export_temperature_vdb or generated_material,
        "export_fuel_vdb": props.export_fuel_vdb
        or (generated_material and props.shader_flame_enabled
            and float(flame_value(props, "flame_core", 0.0)) > 0.0),
        # The generated material no longer reads burn (Burn look removed).
        "export_burn_vdb": props.export_burn_vdb,
        "export_velocity_vdb": props.export_velocity_vdb,
        "export_glow_vdb": bool(getattr(props, "export_glow_vdb", True)),
        "export_density_vdb": bool(getattr(props, "export_density_vdb", True)) or generated_material,
        **{f"export_{name}_downsample": _EXPORT_DOWNSAMPLE.get(getattr(props, f"export_{name}_resolution", "FULL"), 1)
           for name in ("density", "temperature", "fuel", "burn", "glow")},
        "export_velocity_downsample": _velocity_downsample(props),
        "glow_radius": max(0.0, float(props.glow_radius)),
    }



def _mesh_state(participant, depsgraph, payload, frame):
    obj = participant["object"]
    props = obj.fumaris
    initial_velocity = props.velocity
    enabled = bool(props.participant_enabled)
    if not enabled:
        participant.pop("collection_mesh_history", None)
        participant.pop("previous_positions", None)
        participant.pop("previous_matrix", None)
        track_deformation = False
        matrix = Matrix.Identity(4) if _is_particles(props, "mesh") else obj.evaluated_get(depsgraph).matrix_world.copy()
        positions = array("f")
        indices = array("i")
        velocities = array("f")
        emission_weights = array("f")
        reuse_geometry = False
    elif participant["role"] == "emitter" and props.participant_type == "collection_mesh":
        track_deformation = False
        matrix = Matrix.Identity(4)
        positions, indices, velocities, emission_weights = collection_mesh_arrays(
            participant, depsgraph, frame,
            bpy.context.scene.render.fps / bpy.context.scene.render.fps_base / participant.get("simulation_speed", 1.0),
        )
        initial_velocity = _world_vector(obj.evaluated_get(depsgraph).matrix_world, props.velocity)
        position_version = topology_version = frame
        reuse_geometry = False
    elif _is_particles(props, "mesh"):
        track_deformation = False
        matrix = Matrix.Identity(4)
        positions, indices, velocities = _particle_mesh(obj, depsgraph, props)
        participant["instanced_triangles"] = len(indices) // 3
        emission_weights = array("f")
        position_version = frame
        topology_version = frame
        reuse_geometry = False
    elif _is_geometry_nodes(props, "mesh"):
        track_deformation = not uses_velocity_attribute(props)
        matrix = Matrix.Identity(4)
        positions, indices, velocities, emission_weights = _geometry_nodes_mesh(
            obj,
            depsgraph,
            props,
        )
        position_version = frame
        topology_version = frame
        reuse_geometry = False
    else:
        track_deformation = (
            _static_mesh_cache_key(
                obj, depsgraph, props, participant.get("closed_surface") is not None) is None
            and not uses_velocity_attribute(props)
        )
        matrix = obj.evaluated_get(depsgraph).matrix_world.copy()
        (
            positions,
            indices,
            velocities,
            emission_weights,
            position_version,
            topology_version,
            reuse_geometry,
        ) = _cached_or_evaluated_mesh(
            participant, obj, depsgraph, props, matrix, frame
        )
    if not enabled:
        position_version = frame
        topology_version = frame
    if enabled and participant["role"] == "emitter":
        if track_deformation:
            _add_mesh_motion_velocities(
                velocities,
                positions,
                participant.get("previous_positions"),
                matrix,
                props.motion_velocity_scale,
                context_fps=bpy.context.scene.render.fps / bpy.context.scene.render.fps_base
                / participant.get("simulation_speed", 1.0),
            )
            participant["previous_positions"] = array("f", positions)
        else:
            participant.pop("previous_positions", None)
        if velocities and uses_velocity_attribute(props) and participant.get("simulation_speed", 1.0) != 1.0:
            # A velocity attribute is per second of scene time (copy: the array may be cached).
            inverse = 1.0 / participant["simulation_speed"]
            velocities = array("f", (value * inverse for value in velocities))
        _add_mesh_initial_velocity(velocities, positions, matrix, initial_velocity)
    if velocities and len(velocities) != len(positions):
        raise RuntimeError(
            f"{obj.name} produced {len(velocities) // 3} mesh velocities "
            f"for {len(positions) // 3} vertices"
        )
    if emission_weights and len(emission_weights) != len(indices):
        raise RuntimeError(
            f"{obj.name} produced {len(emission_weights)} mesh emission weights "
            f"for {len(indices)} face corners"
        )
    position_section = _reuse_section() if reuse_geometry else _append_array(payload, positions)
    index_section = _reuse_section() if reuse_geometry else _append_array(payload, indices)
    velocity_section = _append_array(payload, velocities) if velocities else None
    weight_section = (
        _append_array(payload, emission_weights) if emission_weights else None
    )
    minimum, maximum = _mesh_distances(props, participant["role"])
    channels = (
        _participant_channels(
            props,
            participant["role"],
            # Always evaluate the move so the stored matrix stays current.
            velocity_active=_transform_moved(participant, matrix) or bool(velocities),
        )
        if enabled
        else _disabled_channels()
    )
    if enabled and participant["role"] == "emitter":
        channels.update(_closed_surface_source(participant, props, matrix))
        if (props.participant_type == "mesh" and participant.get("closed_surface") is None
                and abs(props.normal_velocity) > 1e-6 and channels.get("couple_rate_velocity", 0.0) > 0.0):
            # An open surface cannot add volume, so Normal Velocity makes it a
            # fan: air passes through at that speed. At the default coupling
            # (8/s) a 4 m plane at 2 m/s moved air at only 1.16-1.40 m/s
            # (Resolution 128-512); at 40/s, 1.75-1.84 m/s at all three
            # (2026-11-06, output/review-fixes/plane_normal_velocity.py).
            channels["couple_rate_velocity"] = max(
                channels["couple_rate_velocity"], OPEN_SURFACE_FAN_COUPLING)

    if enabled and participant["role"] == "emitter":
        _scale_emitter_channels(channels, props)
    effective_enabled = enabled and (participant["role"] != "emitter" or _emitter_strength(props) > 0.0)

    state = {
        "id": participant["id"],
        "role": participant["role"],
        "enabled": effective_enabled,
        "local_to_world": [value for row in matrix for value in row],
        "min_distance": minimum,
        "max_distance": maximum,
        "is_collision": participant["role"] == "collider",
        "collision_solid": bool(getattr(props, "collider_solid", True)),
        # Open (non-manifold) collider meshes have no inside (Open Collider Wall).
        "open_surface": participant["role"] == "collider" and participant.get("closed_surface") is None,
        "is_outflow": participant["role"] == "outflow",
        "motion_velocity_scale": 0.0 if participant["role"] != "emitter" else props.motion_velocity_scale,
        "physics_velocity_scale": props.collider_velocity_influence if participant["role"] == "collider" else props.motion_velocity_scale,
        "motion_substeps": props.motion_substeps,
        **channels,
        "positions": {**position_section, "version": _data_version(position_version)},
        "indices": {**index_section, "version": _data_version(topology_version)},
    }
    if velocity_section:
        state["velocities"] = {**velocity_section, "version": _data_version(position_version)}
    if props.participant_type == "collection_mesh" and participant.get("collection_mesh_layout_changed"):
        state["motion_substeps"] = 1
    if weight_section:
        state["emission_weights"] = {**weight_section, "version": _data_version(frame)}
    return state


def _sphere_state(participant, depsgraph):
    obj = participant["object"]
    props = obj.fumaris
    matrix = obj.evaluated_get(depsgraph).matrix_world
    position = matrix.translation
    if participant["role"] == "collider":
        radius = props.collider_radius
    else:
        radius = props.emitter_radius
    radius *= max(abs(value) for value in matrix.to_scale())
    enabled = bool(props.participant_enabled)
    channels = (
        _participant_channels(
            props,
            participant["role"],
            matrix=matrix,
            velocity_active=_transform_moved(participant, matrix),
        )
        if enabled
        else _disabled_channels()
    )
    if not enabled:
        participant.pop("previous_matrix", None)
    elif participant["role"] == "emitter":
        r = max(radius, 1e-12)
        channels.update(_primitive_surface_source(
            props, 4.0 * math.pi * r * r, 4.0 / 3.0 * math.pi * r ** 3))

    if enabled and participant["role"] == "emitter":
        _scale_emitter_channels(channels, props)
    effective_enabled = enabled and (participant["role"] != "emitter" or _emitter_strength(props) > 0.0)
    return {
        "id": participant["id"],
        "role": participant["role"],
        "enabled": effective_enabled,
        "position": [position.x, position.y, position.z],
        "radius": max(radius, 1e-12),
        "is_collision": participant["role"] == "collider",
        "collision_solid": bool(getattr(props, "collider_solid", True)),
        "physics_velocity_scale": props.collider_velocity_influence if participant["role"] == "collider" else props.motion_velocity_scale,
        "motion_substeps": props.motion_substeps,
        "multisample": props.sphere_multisample,
        "trace_samples": props.sphere_trace_samples,
        **channels,
    }


def _box_state(participant, depsgraph):
    obj = participant["object"]
    props = obj.fumaris
    matrix, half_size = _evaluated_box(obj.evaluated_get(depsgraph))
    wall_half_size = list(half_size)
    if participant["role"] == "collider":
        scales = [max(abs(value), 1e-12) for value in matrix.to_scale()]
        half_size = [
            value + props.collider_margin / scale
            for value, scale in zip(half_size, scales)
        ]
    enabled = bool(props.participant_enabled)
    channels = (
        _participant_channels(
            props,
            participant["role"],
            matrix=matrix,
            velocity_active=_transform_moved(participant, matrix),
        )
        if enabled
        else _disabled_channels()
    )
    if not enabled:
        participant.pop("previous_matrix", None)
    elif participant["role"] == "emitter":
        scale = matrix.to_scale()
        x, y, z = (2.0 * abs(h * s) for h, s in zip(half_size, scale))
        channels.update(_primitive_surface_source(
            props, 2.0 * (x * y + y * z + z * x), x * y * z))
    if enabled and participant["role"] == "emitter":
        _scale_emitter_channels(channels, props)
    effective_enabled = enabled and (participant["role"] != "emitter" or _emitter_strength(props) > 0.0)
    return {
        "id": participant["id"],
        "role": participant["role"],
        "enabled": effective_enabled,
        "local_to_world": [value for row in matrix for value in row],
        "half_size": half_size,
        "wall_half_size": wall_half_size,
        "is_collision": participant["role"] == "collider",
        "collision_solid": bool(getattr(props, "collider_solid", True)),
        "physics_velocity_scale": props.collider_velocity_influence if participant["role"] == "collider" else props.motion_velocity_scale,
        **channels,
    }


def _evaluated_box(obj):
    corners = [Vector(corner) for corner in obj.bound_box]
    if not corners:
        return obj.matrix_world.copy(), [1.0, 1.0, 1.0]
    minimum = Vector(tuple(min(corner[axis] for corner in corners) for axis in range(3)))
    maximum = Vector(tuple(max(corner[axis] for corner in corners) for axis in range(3)))
    if (maximum - minimum).length_squared == 0.0:
        return obj.matrix_world.copy(), [1.0, 1.0, 1.0]
    center = (minimum + maximum) * 0.5
    half_size = [max(1e-12, value) for value in ((maximum - minimum) * 0.5)]
    return obj.matrix_world @ Matrix.Translation(center), half_size


def _volume_state(context, participant, depsgraph, frame, volume_stage_dir):
    obj = participant["object"]
    props = obj.fumaris
    matrix = obj.evaluated_get(depsgraph).matrix_world
    enabled = bool(props.participant_enabled)
    disabled = {
        "id": participant["id"],
        "enabled": False,
        "filepath": "",
        "file_version": 0,
        "local_to_world": [value for row in matrix for value in row],
        **_disabled_channels(),
    }
    if not enabled:
        return disabled
    if participant["role"] == "emitter" and _emitter_strength(props) == 0.0:
        return disabled
    if _is_geometry_nodes(props, "volume"):
        filepath = _stage_gn_volume(obj, depsgraph, frame, volume_stage_dir)
    else:
        filepath = scene_file_path(props.volume_filepath)
        if props.volume_use_sequence:
            filepath = _sequence_frame_path(filepath, frame + props.volume_frame_offset)
            if not os.path.isfile(filepath):
                # Outside the sequence's range this emitter contributes nothing.
                return disabled

    if not filepath or not os.path.isfile(filepath):
        raise RuntimeError(f"{obj.name} did not provide a volume VDB")

    file_version = _volume_file_version(filepath)

    channels = _participant_channels(
        props, participant["role"],
        velocity_active=bool(getattr(props, "volume_use_velocity", False)),
    )
    if participant["role"] == "emitter":
        _scale_emitter_channels(channels, props)
    return {
        "id": participant["id"],
        "enabled": True,
        "filepath": filepath,
        "file_version": file_version,
        "grids": _volume_grid_names(props),
        "local_to_world": [value for row in matrix for value in row],
        **channels,
    }


def _volume_grid_names(props):
    return {
        name: (str(getattr(props, f"volume_grid_{name}", "") or "").strip() or name)
        for name in ("density", "temperature", "fuel", "burn", "velocity")
    }


def _sequence_frame_path(filepath, frame):
    """Replace the trailing frame number of a VDB path, keeping its padding."""
    directory, name = os.path.split(filepath)
    match = _SEQUENCE_NAME.match(name)
    if match is None:
        return filepath
    head, sign, digits, extension = match.groups()
    # Fumaris writes negative frames as prefix-0001; elsewhere '-' separates.
    if sign and not (head.endswith(("_", ".")) or not head):
        head, sign = head + sign, ""
    width = len(digits)
    if frame < 0:
        number = f"-{-frame:0{width}d}"
    else:
        number = f"{frame:0{width}d}"
    return os.path.join(directory, f"{head}{number}{extension}")


def _stage_gn_volume(obj, depsgraph, frame, directory):
    if not directory:
        raise RuntimeError("Geometry Nodes volume staging is unavailable")
    evaluated = obj.evaluated_get(depsgraph)
    geometry = evaluated.evaluated_geometry()
    volume = getattr(geometry, "volume", None)
    if volume is None:
        data = getattr(evaluated, "data", None)
        volume = data if hasattr(data, "grids") else None
    if volume is None:
        raise RuntimeError(f"{obj.name} has no evaluated volume output")

    grids = getattr(volume, "grids", None)
    if grids is None:
        raise RuntimeError(f"{obj.name} has no volume grids")
    grids.load()
    if len(grids) == 0:
        raise RuntimeError(f"{obj.name} has no volume grids")

    os.makedirs(directory, exist_ok=True)
    filepath = os.path.join(directory, f"volume_{obj.as_pointer()}.vdb")
    temporary = filepath + ".tmp.vdb"
    if os.path.exists(temporary):
        os.remove(temporary)
    if not grids.save(temporary):
        raise RuntimeError(f"Blender failed to save volume grids from {obj.name}")
    os.replace(temporary, filepath)
    return filepath


def _volume_file_version(filepath):
    stat = os.stat(filepath)
    version = (
        (stat.st_mtime_ns & ((1 << 63) - 1))
        ^ ((stat.st_ctime_ns & ((1 << 63) - 1)) << 1)
        ^ (stat.st_size << 7)
    ) & ((1 << 64) - 1)
    return version or 1


def _effector_state(participant, depsgraph):
    obj = participant["object"]
    props = obj.fumaris
    matrix = obj.evaluated_get(depsgraph).matrix_world
    position = matrix.translation
    local_to_world = matrix.to_quaternion().to_matrix().to_4x4()
    local_to_world.translation = position

    radius = max(1e-12, props.effector_radius)
    is_vortex = props.effector_type == "vortex"
    if (
        not is_vortex
        and props.effector_use_max_distance
        and props.effector_max_distance > 0.0
    ):
        radius = max(1e-12, props.effector_max_distance)
    minimum = 0.0
    if not is_vortex and props.effector_use_min_distance:
        minimum = min(radius, max(0.0, props.effector_min_distance))

    return {
        "id": participant["id"],
        "enabled": bool(props.participant_enabled),
        "type": _effector_type_id(props.effector_type),
        "local_to_world": [value for row in local_to_world for value in row],
        "strength": props.effector_strength,
        "radius": radius,
        "height": max(1e-12, props.effector_vortex_height),
        "core_radius": min(radius, max(1e-12, props.effector_vortex_core_radius)),
        "inflow": max(0.0, props.effector_vortex_inflow),
        "lift": props.effector_vortex_lift,
        "coupling": max(0.0, props.effector_coupling),
        "falloff_power": max(0.0, props.effector_falloff_power),
        "min_distance": minimum,
        "z_direction": _effector_z_direction_id(props.effector_z_direction),
        "noise_amount": max(0.0, props.effector_noise_amount),
        "noise_size": max(1e-12, props.effector_noise_size),
        "noise_w": props.effector_noise_w,
        "noise_seed": props.effector_noise_seed,
        "samples": max(8, min(256, props.effector_samples)),
        **_effector_mask(props),
    }


def _effector_mask(props):
    channel = _EFFECTOR_MASK_CHANNELS.get(getattr(props, "effector_mask_field", "none"), 0)
    if not channel:
        return {"mask_channel": 0}
    start, full = float(props.effector_mask_min), float(props.effector_mask_max)
    if props.effector_mask_invert:
        # A reversed range fades the force out as the field rises.
        start, full = full, start
    return {"mask_channel": channel, "mask_min": start, "mask_max": full}


_EFFECTOR_MASK_CHANNELS = {"temperature": 1, "fuel": 2, "burn": 3, "smoke": 4, "speed": 5}


def _effector_type_id(name):
    return {
        "force": 0,
        "wind": 1,
        "vortex": 2,
        "turbulence": 3,
        "drag": 4,
    }.get(name, 1)


def _effector_z_direction_id(name):
    return {"positive": 1, "negative": -1}.get(name, 0)


class _MotionTimeScaled:
    """An emitter's settings with motion velocity rescaled to simulated time
    (point velocity attributes are per second of scene time)."""

    def __init__(self, props, factor):
        object.__setattr__(self, "_props", props)
        object.__setattr__(self, "motion_velocity_scale", float(props.motion_velocity_scale) * factor)

    def __getattr__(self, name):
        return getattr(self._props, name)


def _sphere_cloud_state(participant, depsgraph, payload, frame):
    obj = participant["object"]
    props = obj.fumaris
    enabled = bool(props.participant_enabled)
    if props.participant_type == "collection_points":
        positions, radii, velocities, values = collection_point_arrays(
            participant, depsgraph, frame,
            bpy.context.scene.render.fps / bpy.context.scene.render.fps_base / participant.get("simulation_speed", 1.0),
        )
    elif enabled:
        positions, radii, velocities, values = _point_arrays(
            obj, _MotionTimeScaled(props, 1.0 / participant.get("simulation_speed", 1.0)), depsgraph)
    else:
        positions, radii, velocities, values = _empty_point_arrays()

    if not positions:
        positions, radii, velocities, values = _empty_point_arrays()

    _disable_inactive_point_velocity_coupling(velocities, values)
    value_ops, replaced = _source_operations(props)
    _point_normal_velocity(props, radii, values)
    for name in replaced:
        key = "couple_rate_velocities" if name == "velocity" else f"couple_rate_{name}s"
        rates = values.get(key)
        if rates:
            values[key] = array(
                "f", (_REPLACE_COUPLE_RATE if rate > 0.0 else 0.0 for rate in rates))

    if getattr(props, "emitter_influence", False):
        value_ops = _influence_point_values(values, value_ops)
    elif getattr(props, "emitter_burning", False):
        value_ops = _burning_point_values(values, props, _BURN_DOMAIN, value_ops)

    strength = _emitter_strength(props)
    _scale_point_emitter_values(values, value_ops, strength, props)
    effective_enabled = enabled and strength > 0.0

    sections = {
        "positions": _versioned(payload, positions, frame),
        "radii": _versioned(payload, radii, frame),
        "velocities": _versioned(payload, velocities, frame),
    }
    for name, data in values.items():
        sections[name] = _versioned(payload, data, frame)

    return {
        "id": participant["id"],
        "enabled": effective_enabled,
        "global_influence": strength,
        "multisample": bool(
            _is_particles(props, "point_cloud") and props.point_enable_interpolation
        ),
        # Flow's sphere trace follows the velocity field and is not a visible
        # point-cloud control. Point motion is carried explicitly per point.
        "trace_samples": 0,
        "motion_substeps": props.motion_substeps,
        "apply_post_pressure": props.emitter_apply_post_pressure,
        "value_ops": value_ops,
        "color": [float(v) for v in props.emitter_color],
        **sections,
    }


def _disabled_channels():
    return _channel_values(
        None,
        velocity=[0.0, 0.0, 0.0],
        divergence=0.0,
        temperature=0.0,
        fuel=0.0,
        burn=0.0,
        smoke=0.0,
        couple_rate_velocity=0.0,
        couple_rate_divergence=0.0,
        couple_rate_temperature=0.0,
        couple_rate_fuel=0.0,
        couple_rate_burn=0.0,
        couple_rate_smoke=0.0,
        apply_post_pressure=False,
    )


def _channels(props, role, matrix=None):
    if role == "collider":
        return _channel_values(props, smoke=0.0, temperature=0.0, fuel=0.0, burn=0.0)
    if role == "outflow":
        return _channel_values(
            props,
            smoke=0.0,
            temperature=0.0,
            fuel=0.0,
            burn=0.0,
            couple_rate_smoke=props.outflow_coupling,
            couple_rate_temperature=props.outflow_coupling,
            couple_rate_fuel=props.outflow_coupling,
            couple_rate_burn=props.outflow_coupling,
        )
    if matrix is not None:
        return _channel_values(props, velocity=_world_vector(matrix, props.velocity))
    return _channel_values(props)


def _participant_channels(props, role, matrix=None, *, velocity_active=False):
    channels = _channels(props, role, matrix)
    if (
        role == "emitter"
        and not velocity_active
        and not _vector_active(channels["velocity"])
    ):
        # A zero target with a positive Flow coupling rate is a drag field.
        # Static sources should emit no velocity rather than pinning nearby fluid.
        channels["couple_rate_velocity"] = 0.0
    if role == "emitter":
        value_ops, replaced = _source_operations(props)
        channels["value_ops"] = value_ops
        for name in replaced:
            if channels[f"couple_rate_{name}"] > 0.0:
                channels[f"couple_rate_{name}"] = _REPLACE_COUPLE_RATE
        if getattr(props, "emitter_influence", False):
            _influence_channels(channels)
        elif getattr(props, "emitter_burning", False):
            channels["value_ops"] = _burning_channels(channels, props, _BURN_DOMAIN)
    return channels


# Domain settings of the frame being built (Burning emitters follow its combustion)
# and its Preview Resolution scale.
_BURN_DOMAIN = None
_RESOLUTION_SCALE = 1.0


def _burning_channels(channels, props, domain):
    """Burning emitter: heat, smoke and expansion of fuel burning at Burn Rate.

    No fuel is emitted and no reaction runs: temperature is pulled up to Flame
    Temperature (Maximum, never cools), smoke is added at Smoke Amount per unit
    burnt and the gas expands by Gas Expansion per unit burnt (1/s).
    """
    rate = max(0.0, float(props.emitter_burn_rate))
    smoke_amount = float(getattr(domain, "smoke_per_burn", 1.0)) if domain else 1.0
    expansion = float(getattr(domain, "gas_expansion", 0.5)) if domain else 0.5
    channels["fuel"] = 0.0
    channels["couple_rate_fuel"] = 0.0
    channels["burn"] = 0.0
    channels["couple_rate_burn"] = 0.0
    channels["temperature"] = 1.0
    channels["couple_rate_temperature"] = 2.0 * rate
    channels["smoke"] = smoke_amount
    channels["couple_rate_smoke"] = rate
    channels["divergence"] = expansion * rate
    channels["couple_rate_divergence"] = max(float(channels.get("couple_rate_divergence", 0.0)), 1.0)
    ops = int(channels.get("value_ops", 0)) | EXPANSION_RATE_OP
    temperature = _SOURCE_OPERATION_CHANNELS.index("temperature")
    smoke = _SOURCE_OPERATION_CHANNELS.index("smoke")
    ops &= ~(1 << temperature)
    ops |= 1 << (4 + temperature)  # Maximum
    ops &= ~(1 << (4 + smoke))
    ops |= 1 << smoke  # Add
    return ops


def _emitter_strength(props):
    return max(0.0, float(getattr(props, "emitter_global_influence", 1.0)))


def _scaled_emitter_rate(rate, strength, replace):
    # Replace normally uses an effectively instantaneous coupling. Scaling that
    # sentinel directly would still be instantaneous even at 0.001 influence.
    # Below 1, blend the selected fraction over one scene frame instead.
    if replace and rate == _REPLACE_COUPLE_RATE and strength < 1.0:
        if strength <= 0.0:
            return 0.0
        scene = bpy.context.scene
        fps = scene.render.fps / max(1e-6, scene.render.fps_base)
        speed = max(1e-4, float(getattr(_BURN_DOMAIN, "simulation_speed", 1.0)))
        return -math.log1p(-strength) * fps / speed
    return rate * strength


def _scale_emitter_channels(channels, props):
    strength = _emitter_strength(props)
    channels["global_influence"] = strength
    if strength == 1.0:
        return
    for name in ("smoke", "temperature", "fuel", "burn", "velocity"):
        key = f"couple_rate_{name}"
        channels[key] = _scaled_emitter_rate(
            channels[key], strength, getattr(props, f"source_op_{name}", "blend") == "replace")
    if channels.get("value_ops", 0) & EXPANSION_BLEND_OP:
        channels["couple_rate_divergence"] = _scaled_emitter_rate(
            channels["couple_rate_divergence"], strength,
            getattr(props, "source_op_divergence", "max") == "replace")
    else:
        channels["divergence"] *= strength
        if "surface_flux" in channels:
            channels["surface_flux"] *= strength


def _scale_point_emitter_values(values, value_ops, strength, props):
    if strength == 1.0:
        return
    keys = {"couple_rate_velocities": "velocity", "couple_rate_temperatures": "temperature",
            "couple_rate_fuels": "fuel", "couple_rate_burns": "burn", "couple_rate_smokes": "smoke"}
    keys["couple_rate_divergences" if value_ops & EXPANSION_BLEND_OP else "divergences"] = "divergence"
    for key, channel in keys.items():
        if key not in values:
            continue
        replace = (key != "divergences" and strength < 1.0 and
                   getattr(props, f"source_op_{channel}", "blend") == "replace")
        replacement = _scaled_emitter_rate(_REPLACE_COUPLE_RATE, strength, True) if replace else 0.0
        if np is not None:
            scaled = array("f", values[key])
            samples = np.frombuffer(scaled, dtype=np.float32)
            replace_mask = samples == _REPLACE_COUPLE_RATE if replace else None
            samples *= strength
            if replace:
                samples[replace_mask] = replacement
            values[key] = scaled
        else:
            values[key] = array("f", (replacement if replace and value == _REPLACE_COUPLE_RATE
                                      else value * strength for value in values[key]))


def _influence_channels(channels):
    """Influence Only: leave smoke, fuel and burn alone; heat only upward.

    Flow blends each coupled channel toward its target, so a zero smoke target
    would drain the smoke inside the emitter. Velocity keeps its blend (a pull
    toward the target without overshoot) and Expansion its source; temperature
    is coupled only to a positive target in Maximum mode, so it never cools.
    """
    for name in ("smoke", "fuel", "burn"):
        channels[f"couple_rate_{name}"] = 0.0
    if channels.get("temperature", 0.0) > 0.0:
        channel = _SOURCE_OPERATION_CHANNELS.index("temperature")
        ops = int(channels.get("value_ops", 0)) & ~(1 << channel)
        channels["value_ops"] = ops | (1 << (4 + channel))
        if channels["couple_rate_temperature"] == _REPLACE_COUPLE_RATE:
            channels["couple_rate_temperature"] = 10.0
    else:
        channels["couple_rate_temperature"] = 0.0


def _point_normal_velocity(props, radii, values):
    """Normal Velocity of a point emitter: each point is a small sphere pushing
    out at that speed, an expansion of 3 v / r (surface flux over its volume;
    points smaller than a velocity cell spread it over the cell)."""
    speed = float(getattr(props, "normal_velocity", 0.0))
    divergences = values.get("divergences")
    if abs(speed) < 1e-6 or not divergences or not radii:
        return
    domain = _BURN_DOMAIN
    resolution = max(1, int(getattr(domain, "resolution", 256))) if domain else 256
    # The simulated resolution (Preview Resolution scales it); velocity cells
    # are two density cells unless Full-Resolution Velocity is on.
    resolution = max(1.0, resolution * _RESOLUTION_SCALE)
    velocity_cells = 1.0 if getattr(domain, "full_resolution_velocity", False) else 2.0
    # Flow's spherical footprint stamps at least a sphere of ~1.29 velocity
    # cells (measured 2026-11-05: point sources match v r^2 / d^2 within 3%).
    cell = 1.29 * velocity_cells * 32.0 / resolution
    rates = values.setdefault("couple_rate_divergences", array("f", [0.0] * len(divergences)))
    for index, radius in enumerate(radii):
        if index >= len(divergences):
            break
        r = max(float(radius), 1e-12)
        divergences[index] += 3.0 * speed * r * r / max(r, cell) ** 3
        if index < len(rates) and rates[index] <= 0.0:
            rates[index] = 1.0


def _influence_point_values(values, value_ops):
    """Influence Only for point emitters (per-point arrays), as _influence_channels."""
    for name in ("smoke", "fuel", "burn"):
        rates = values.get(f"couple_rate_{name}s")
        if rates:
            values[f"couple_rate_{name}s"] = array("f", (0.0 for _ in rates))
    temperatures = values.get("temperatures")
    rates = values.get("couple_rate_temperatures")
    if rates:
        values["couple_rate_temperatures"] = array("f", (
            (10.0 if rate == _REPLACE_COUPLE_RATE else rate) if temperatures and temperature > 0.0 else 0.0
            for rate, temperature in zip(rates, temperatures or [0.0] * len(rates))))
    channel = _SOURCE_OPERATION_CHANNELS.index("temperature")
    return (int(value_ops) & ~(1 << channel)) | (1 << (4 + channel))


def _burning_point_values(values, props, domain, value_ops):
    """Burning for point emitters (per-point arrays), as _burning_channels.

    Every point burns at Burn Rate; until 2026-11-10 point clouds ignored
    Burning and emitted their plain values instead (the Candle source never
    reached flame temperature)."""
    channels = {"value_ops": value_ops}
    ops = _burning_channels(channels, props, domain)
    count = len(values.get("temperatures") or ())
    for name, key in (("fuel", "fuels"), ("burn", "burns"), ("temperature", "temperatures"),
                      ("smoke", "smokes"), ("divergence", "divergences")):
        values[key] = array("f", [float(channels[name])]) * count
        values[f"couple_rate_{key}"] = array("f", [float(channels[f"couple_rate_{name}"])]) * count
    return ops


def _closed_surface_source(participant, props, matrix):
    """Normal Velocity and volume change of a closed mesh as a volume source.

    Outward flow through a closed surface is curl-free, so the pressure solve
    removes it as velocity; Flow keeps it as expansion. The bridge spreads
    surface_flux (m^3/s: Normal Velocity x area + dV/dt) over the emitting
    shell: area x inside thickness (at most the enclosed volume) plus area x
    outside thickness, and at least one velocity cell thick.
    """
    surface = participant.get("closed_surface")
    if surface is None:
        participant.pop("previous_volume", None)
        return {}
    area, volume = world_surface(surface, matrix)
    if area <= 0.0:
        return {}
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base / participant.get("simulation_speed", 1.0)
    previous = participant.get("previous_volume")
    participant["previous_volume"] = volume
    volume_rate = 0.0 if previous is None else (volume - previous) * fps * props.motion_velocity_scale
    minimum, maximum = _mesh_distances(props, "emitter")
    return {
        "surface_flux": float(props.normal_velocity) * area + volume_rate,
        "surface_area": area,
        "enclosed_volume": abs(volume),
        "shell_inside": max(0.0, -minimum),
        "shell_outside": max(0.0, maximum),
    }


def _primitive_surface_source(props, area, volume):
    """Normal Velocity of a box or sphere emitter as a volume source: they fill
    their whole volume, so the flux v x area spreads over it."""
    if area <= 0.0 or volume <= 0.0:
        return {}
    if abs(float(props.normal_velocity)) < 1e-6:
        return {}
    return {
        "surface_flux": float(props.normal_velocity) * area,
        "surface_area": area,
        "enclosed_volume": volume,
        "shell_inside": 1.0e9,
        "shell_outside": 0.0,
    }


# Flow NV_FLOW_EMITTER_OP_EXPANSION_RATE: Expansion is in 1/s, applied exactly.
EXPANSION_RATE_OP = 1 << 16
# NV_FLOW_EMITTER_OP_EXPANSION_BLEND: pull divergence toward Expansion at the coupling rate.
EXPANSION_BLEND_OP = 1 << 18
# NV_FLOW_EMITTER_OP_VELOCITY_ADD / _MAX for velocity x, y, z.
VELOCITY_ADD_OPS = (1 << 8) | (1 << 9) | (1 << 10)
VELOCITY_MAX_OPS = (1 << 12) | (1 << 13) | (1 << 14)


def _source_operations(props):
    """Flow value-op bits and the channels that are in Replace mode."""
    value_ops = EXPANSION_RATE_OP
    replaced = []
    for channel, name in enumerate(_SOURCE_OPERATION_CHANNELS):
        operation = getattr(props, f"source_op_{name}", "blend")
        if operation == "add":
            value_ops |= 1 << channel
        elif operation == "max":
            value_ops |= 1 << (4 + channel)
        elif operation == "replace":
            replaced.append(name)
    # Expansion (velocity-pass w): exact by default; Blend pulls toward it at
    # the coupling rate, Replace at an immediate one.
    operation = getattr(props, "source_op_divergence", "max")
    if operation in {"blend", "replace"}:
        value_ops |= EXPANSION_BLEND_OP
    if operation == "replace":
        replaced.append("divergence")
    # Velocity, per axis (Flow's velocity-pass channels x, y, z).
    operation = getattr(props, "source_op_velocity", "blend")
    if operation == "add":
        value_ops |= VELOCITY_ADD_OPS
    elif operation == "max":
        value_ops |= VELOCITY_MAX_OPS
    elif operation == "replace":
        replaced.append("velocity")
    return value_ops, replaced


def _transform_moved(participant, matrix):
    previous = participant.get("previous_matrix")
    participant["previous_matrix"] = matrix.copy()
    if previous is None:
        return False
    participant["frame_travel"] = (matrix.translation - previous.translation).length
    return any(
        abs(matrix[row][column] - previous[row][column]) > 1e-6
        for row in range(4)
        for column in range(4)
    )


def _vector_active(values):
    return any(abs(value) > 1e-6 for value in values)


def _disable_inactive_point_velocity_coupling(velocities, values):
    coupling = values.get("couple_rate_velocities")
    if coupling is None:
        return
    count = min(len(coupling), len(velocities) // 3)
    if np is not None:
        vectors = np.frombuffer(velocities, dtype=np.float32, count=count * 3)
        active = np.any(
            np.abs(vectors.astype(np.float64)).reshape((-1, 3)) > 1e-6, axis=1)
        np.frombuffer(coupling, dtype=np.float32, count=count)[~active] = 0.0
        return
    for index in range(count):
        offset = index * 3
        if not _vector_active(velocities[offset:offset + 3]):
            coupling[index] = 0.0


def _channel_values(props, **overrides):
    divergence = props.emitter_divergence if props is not None else 0.0
    divergence_coupling = _effective_divergence_coupling(props, divergence)
    data = {
        "velocity": list(props.velocity) if props is not None else [0.0, 0.0, 0.0],
        "divergence": divergence,
        "temperature": props.emitter_temperature if props is not None else 0.0,
        "fuel": props.emitter_fuel if props is not None else 0.0,
        "burn": props.emitter_burn if props is not None else 0.0,
        "smoke": props.emitter_smoke if props is not None else 0.0,
        "color": [float(v) for v in props.emitter_color] if props is not None else [1.0, 1.0, 1.0],
        "couple_rate_velocity": props.couple_rate_velocity if props is not None else 0.0,
        "couple_rate_divergence": divergence_coupling,
        "couple_rate_temperature": props.couple_rate_temperature if props is not None else 0.0,
        "couple_rate_fuel": props.couple_rate_fuel if props is not None else 0.0,
        "couple_rate_burn": props.couple_rate_burn if props is not None else 0.0,
        "couple_rate_smoke": props.couple_rate_smoke if props is not None else 0.0,
        "apply_post_pressure": props.emitter_apply_post_pressure if props is not None else False,
    }
    data.update(overrides)
    return data


# Velocity coupling (1/s) for open-mesh Normal Velocity (see _mesh_state).
OPEN_SURFACE_FAN_COUPLING = 40.0


def _effective_divergence_coupling(props, divergence):
    # Coupling 0 turns Expansion off, as it does for every other channel.
    if props is None:
        return 0.0
    return max(0.0, float(props.couple_rate_divergence))


def _mesh_distances(props, role):
    if role == "collider":
        distance = max(0.0, props.collider_margin)
        return -distance, distance
    distance = max(0.0, props.mesh_emission_distance)
    if props.mesh_emission_mode == "volume" or role == "outflow":
        return -distance, 0.0
    return -distance, distance


def _data_version(frame):
    # Flow and the collider cache treat version 0 as never cached, which
    # would re-upload a static mesh every frame of a simulation begun there.
    return int(frame) or _FRAME_ZERO_VERSION


def _versioned(payload, values, frame):
    return {**_append_array(payload, values), "version": _data_version(frame)}


def _append_array(payload, values):
    offset = len(payload)
    if sys.byteorder != "little":
        encoded = array(values.typecode, values)
        encoded.byteswap()
        payload.extend(encoded.tobytes())
    else:
        payload.extend(memoryview(values).cast("B"))
    return {"offset": offset, "count": len(values)}


def _reuse_section():
    return {"offset": 0, "count": 0, "reuse": True}
