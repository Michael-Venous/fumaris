import os
import sys
from array import array
from dataclasses import dataclass

import bpy
from mathutils import Matrix, Vector

from .diagnostics import console_log
from .mesh_export import (
    _add_mesh_initial_velocity,
    _add_mesh_motion_velocities,
    _cached_or_evaluated_mesh,
    _geometry_nodes_mesh,
    _particle_mesh,
    _static_mesh_cache_key,
)
from .participants import (
    collection_signature as _collection_signature,
    is_geometry_nodes as _is_geometry_nodes,
    is_particles as _is_particles,
    participant_shape as _participant_shape,
    participants as _participants,
)
from .point_export import _empty_point_arrays, _point_arrays, _world_vector
from .utils import executable_path, output_directory_for_object, simulation_frame_range


@dataclass(frozen=True)
class FramePacket:
    header: dict
    payload: bytes


def bridge_executable():
    executable = executable_path()
    return executable if executable and os.path.isfile(executable) else None


def build_session(
    context,
    start_frame=None,
    end_frame=None,
    domain=None,
    *,
    write_vdb=True,
    preview_enabled=True,
    preview_max_points=None,
    resolution_scale=1.0,
    log_participants=False,
):
    scene = context.scene
    domain = domain or context.object
    sim_start, sim_end = simulation_frame_range(domain)
    participants = _participants(domain.fumaris)
    if not any(item["role"] == "emitter" for item in participants):
        raise RuntimeError("Assign an emitter collection containing at least one Fumaris emitter")

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
        "write_vdb": bool(write_vdb),
        "preview_enabled": bool(preview_enabled),
        "preview_mode": (
            getattr(domain.fumaris, "preview_mode", "points")
            if preview_enabled
            else "none"
        ),
        "flow_profile_enabled": (
            os.environ.get("FUMARIS_FLOW_PROFILE")
            or os.environ.get("PLUME_FORGE_FLOW_PROFILE", "")
        ).strip().lower() in {"1", "true", "yes", "on"},
        "preview_max_points": _preview_max_points(domain.fumaris, preview_max_points),
        "initial_domain": _domain_state(domain.fumaris, resolution_scale),
    }
    return settings, participants


def _preview_max_points(props, override=None):
    if override is not None:
        return max(0, int(override))
    resolution = max(1, int(props.resolution))
    multiplier = max(0.0, float(getattr(props, "preview_dot_resolution", 1.0)))
    limit = max(512, int(getattr(props, "preview_max_points", 500000)))
    return max(512, min(limit, int(resolution * resolution * 0.25 * multiplier)))


def session_structure_signature(domain):
    props = domain.fumaris
    sim_start, sim_end = simulation_frame_range(domain)
    participants = _participants(props)
    return (
        int(props.sparse_block_capacity),
        bool(props.small_sparse_blocks),
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
            )
            for item in participants
        ),
    )


def build_frame(
    context,
    frame,
    participants,
    domain=None,
    resolution_scale=1.0,
    volume_stage_dir=None,
    preview_enabled=True,
    volume_preview=None,
):
    depsgraph = context.evaluated_depsgraph_get()
    payload = bytearray()
    preview_request = volume_preview or {"valid": False}
    preview_mode = (
        getattr((domain or context.object).fumaris, "preview_mode", "points")
        if preview_enabled
        else "none"
    )
    if preview_mode == "volume" and not preview_request.get("valid", False):
        preview_mode = "points"
    meshes = []
    boxes = []
    spheres = []
    sphere_clouds = []
    effectors = []
    volumes = []

    for participant in participants:
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

    return FramePacket(
        {
            "frame": frame,
            "preview_max_points": _preview_max_points((domain or context.object).fumaris),
            "preview_mode": preview_mode,
            "volume_preview": preview_request,
            "domain": _domain_state((domain or context.object).fumaris, resolution_scale),
            "meshes": meshes,
            "boxes": boxes,
            "spheres": spheres,
            "sphere_clouds": sphere_clouds,
            "points": [],
            "effectors": effectors,
            "volumes": volumes,
        },
        bytes(payload),
    )


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


def _domain_state(props, resolution_scale=1.0):
    resolution = max(1, int(round(props.resolution * max(0.0, float(resolution_scale)))))
    generated_material = props.volume_material is None
    return {
        "resolution": resolution,
        "sparse_block_capacity": props.sparse_block_capacity,
        "simulation_speed": props.simulation_speed,
        "num_sub_steps": props.num_sub_steps,
        "small_sparse_blocks": props.small_sparse_blocks,
        "physics_convex_collision": props.physics_convex_collision,
        "sparse_block_min_lifetime": props.sparse_block_min_lifetime,
        "auto_cell_size": props.auto_cell_size,
        "allocation_smoke_threshold": props.allocation_smoke_threshold,
        "allocation_speed_threshold": props.allocation_speed_threshold,
        "allocation_speed_min_smoke": props.allocation_speed_min_smoke,
        "allocate_neighbor_blocks": props.allocate_neighbor_blocks,
        "boundary_safe_advection": props.boundary_safe_advection,
        "gravity": list(props.gravity),
        "buoyancy_per_temp": props.buoyancy_per_temp,
        "buoyancy_per_smoke": props.buoyancy_per_smoke,
        "vorticity": props.vorticity,
        "dissipation": props.dissipation,
        "temperature_input_scale": props.temperature_input_scale,
        "ignition_temperature": props.ignition_temperature,
        "burn_per_temp": props.burn_per_temp,
        "fuel_per_burn": props.fuel_per_burn,
        "temp_per_burn": props.temp_per_burn,
        "smoke_per_burn": props.smoke_per_burn,
        "divergence_per_burn": props.divergence_per_burn,
        "cooling_rate": props.cooling_rate,
        "export_temperature_vdb": props.export_temperature_vdb or generated_material,
        "export_fuel_vdb": props.export_fuel_vdb,
        "export_burn_vdb": props.export_burn_vdb or generated_material,
        "export_velocity_vdb": props.export_velocity_vdb,
    }


def _mesh_state(participant, depsgraph, payload, frame):
    obj = participant["object"]
    props = obj.fumaris
    enabled = bool(props.participant_enabled)
    if not enabled:
        participant.pop("previous_positions", None)
        participant.pop("previous_matrix", None)
        track_deformation = False
        matrix = Matrix.Identity(4) if _is_particles(props, "mesh") else obj.evaluated_get(depsgraph).matrix_world.copy()
        positions = array("f")
        indices = array("i")
        velocities = array("f")
        emission_weights = array("f")
        reuse_geometry = False
    elif _is_particles(props, "mesh"):
        track_deformation = False
        matrix = Matrix.Identity(4)
        positions, indices, velocities = _particle_mesh(obj, depsgraph, props)
        emission_weights = array("f")
        position_version = frame
        topology_version = frame
        reuse_geometry = False
    elif _is_geometry_nodes(props, "mesh"):
        track_deformation = True
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
        track_deformation = _static_mesh_cache_key(obj, depsgraph, props) is None
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
                context_fps=bpy.context.scene.render.fps / bpy.context.scene.render.fps_base,
            )
            participant["previous_positions"] = array("f", positions)
        else:
            participant.pop("previous_positions", None)
        _add_mesh_initial_velocity(velocities, positions, matrix, props.velocity)
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
            velocity_active=bool(velocities) or _transform_moved(participant, matrix),
        )
        if enabled
        else _disabled_channels()
    )

    state = {
        "id": participant["id"],
        "role": participant["role"],
        "enabled": enabled,
        "local_to_world": [value for row in matrix for value in row],
        "min_distance": minimum,
        "max_distance": maximum,
        "is_collision": participant["role"] == "collider",
        "is_outflow": participant["role"] == "outflow",
        "motion_velocity_scale": 0.0 if participant["role"] != "emitter" else props.motion_velocity_scale,
        "physics_velocity_scale": props.collider_velocity_influence if participant["role"] == "collider" else props.motion_velocity_scale,
        "motion_substeps": props.motion_substeps,
        **channels,
        "positions": {**position_section, "version": position_version},
        "indices": {**index_section, "version": topology_version},
    }
    if velocity_section:
        state["velocities"] = {**velocity_section, "version": position_version}
    if weight_section:
        state["emission_weights"] = {**weight_section, "version": frame}
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

    return {
        "id": participant["id"],
        "role": participant["role"],
        "enabled": enabled,
        "position": [position.x, position.y, position.z],
        "radius": max(radius, 0.001),
        "is_collision": participant["role"] == "collider",
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
    if participant["role"] == "collider":
        scales = [max(abs(value), 1e-6) for value in matrix.to_scale()]
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
    return {
        "id": participant["id"],
        "role": participant["role"],
        "enabled": enabled,
        "local_to_world": [value for row in matrix for value in row],
        "half_size": half_size,
        "is_collision": participant["role"] == "collider",
        "physics_velocity_scale": props.collider_velocity_influence if participant["role"] == "collider" else props.motion_velocity_scale,
        **channels,
    }


def _evaluated_box(obj):
    corners = [Vector(corner) for corner in obj.bound_box]
    if not corners:
        return obj.matrix_world.copy(), [1.0, 1.0, 1.0]
    minimum = Vector(tuple(min(corner[axis] for corner in corners) for axis in range(3)))
    maximum = Vector(tuple(max(corner[axis] for corner in corners) for axis in range(3)))
    if (maximum - minimum).length_squared <= 1e-12:
        return obj.matrix_world.copy(), [1.0, 1.0, 1.0]
    center = (minimum + maximum) * 0.5
    half_size = [max(0.001, value) for value in ((maximum - minimum) * 0.5)]
    return obj.matrix_world @ Matrix.Translation(center), half_size


def _volume_state(context, participant, depsgraph, frame, volume_stage_dir):
    obj = participant["object"]
    props = obj.fumaris
    matrix = obj.evaluated_get(depsgraph).matrix_world
    enabled = bool(props.participant_enabled)
    if not enabled:
        return {
            "id": participant["id"],
            "enabled": False,
            "filepath": "",
            "file_version": 0,
            "local_to_world": [value for row in matrix for value in row],
            **_disabled_channels(),
        }

    filepath = (
        _stage_gn_volume(obj, depsgraph, frame, volume_stage_dir)
        if _is_geometry_nodes(props, "volume")
        else bpy.path.abspath(props.volume_filepath)
    )

    if not filepath or not os.path.isfile(filepath):
        raise RuntimeError(f"{obj.name} did not provide a volume VDB")

    file_version = _volume_file_version(filepath)

    return {
        "id": participant["id"],
        "enabled": True,
        "filepath": filepath,
        "file_version": file_version,
        "local_to_world": [value for row in matrix for value in row],
        **_participant_channels(props, participant["role"]),
    }


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

    radius = max(0.001, props.effector_radius)
    is_vortex = props.effector_type == "vortex"
    if (
        not is_vortex
        and props.effector_use_max_distance
        and props.effector_max_distance > 0.0
    ):
        radius = max(0.001, props.effector_max_distance)
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
        "height": max(0.001, props.effector_vortex_height),
        "core_radius": min(radius, max(0.001, props.effector_vortex_core_radius)),
        "inflow": max(0.0, props.effector_vortex_inflow),
        "lift": props.effector_vortex_lift,
        "coupling": max(0.0, props.effector_coupling),
        "falloff_power": max(0.0, props.effector_falloff_power),
        "min_distance": minimum,
        "z_direction": _effector_z_direction_id(props.effector_z_direction),
        "noise_amount": max(0.0, props.effector_noise_amount),
        "noise_size": max(0.001, props.effector_noise_size),
        "noise_w": props.effector_noise_w,
        "noise_seed": props.effector_noise_seed,
        "samples": max(8, min(64, props.effector_samples)),
    }


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


def _sphere_cloud_state(participant, depsgraph, payload, frame):
    obj = participant["object"]
    props = obj.fumaris
    enabled = bool(props.participant_enabled)
    if enabled:
        positions, radii, velocities, values = _point_arrays(obj, props, depsgraph)
    else:
        positions, radii, velocities, values = _empty_point_arrays()

    if not positions:
        positions, radii, velocities, values = _empty_point_arrays()

    _disable_inactive_point_velocity_coupling(velocities, values)

    sections = {
        "positions": _versioned(payload, positions, frame),
        "radii": _versioned(payload, radii, frame),
        "velocities": _versioned(payload, velocities, frame),
    }
    for name, data in values.items():
        sections[name] = _versioned(payload, data, frame)

    return {
        "id": participant["id"],
        "enabled": enabled,
        "multisample": bool(
            _is_particles(props, "point_cloud") and props.point_enable_interpolation
        ),
        # Flow's sphere trace follows the velocity field and is not a visible
        # point-cloud control. Point motion is carried explicitly per point.
        "trace_samples": 0,
        "motion_substeps": props.motion_substeps,
        "apply_post_pressure": props.emitter_apply_post_pressure,
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
    return channels


def _transform_moved(participant, matrix):
    previous = participant.get("previous_matrix")
    participant["previous_matrix"] = matrix.copy()
    if previous is None:
        return False
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
    for index in range(min(len(coupling), len(velocities) // 3)):
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


def _effective_divergence_coupling(props, divergence):
    if props is None:
        return 0.0
    coupling = props.couple_rate_divergence
    if coupling == 0.0 and abs(divergence) > 1e-6:
        return max(2.0, props.couple_rate_smoke)
    return coupling


def _mesh_distances(props, role):
    if role == "collider":
        distance = max(0.0, props.collider_margin)
        return -distance, distance
    distance = max(0.0, props.mesh_emission_distance)
    if props.mesh_emission_mode == "volume" or role == "outflow":
        return -distance, 0.0
    return -distance, distance


def _versioned(payload, values, frame):
    return {**_append_array(payload, values), "version": frame}


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
