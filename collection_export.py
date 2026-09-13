"""Collection sources with shared channels and per-object motion history."""

from array import array
from mathutils import Vector

from .mesh_export import _cached_or_evaluated_mesh
from .point_export import _append_point_defaults, _empty_point_arrays, _point_radius


def collection_point_arrays(participant, depsgraph, frame, fps):
    host = participant["object"]
    props = host.fumaris
    result = _empty_point_arrays()
    if not props.participant_enabled:
        participant.pop("collection_point_history", None)
        return result
    collection = props.source_collection
    if collection is None:
        raise RuntimeError(f"{host.name}: assign a Source Collection for Collection Points")

    # Identity, not enumeration order, must survive renames and collection edits.
    signature = (collection.session_uid, props.collection_point_center)
    old_signature, old_frame, previous = participant.get(
        "collection_point_history", (None, frame, {})
    )
    delta_frames = float(frame) - float(old_frame)
    if signature != old_signature or delta_frames <= 0.0:
        previous = {}
    inverse_dt = float(fps) / delta_frames if delta_frames > 0.0 else 0.0
    initial = host.evaluated_get(depsgraph).matrix_world.to_3x3() @ Vector(props.velocity)
    positions, radii, velocities, values = result
    current = {}
    for obj in collection.all_objects:
        if obj == host or obj.type != "MESH":
            continue
        evaluated = obj.evaluated_get(depsgraph)
        matrix = evaluated.matrix_world
        center = matrix.translation.copy()
        radius = props.point_radius
        if props.collection_point_center == "bounds" or props.collection_radius_mode == "bounds":
            bounds = evaluated.bound_box
            minimum = Vector(tuple(min(corner[axis] for corner in bounds) for axis in range(3)))
            maximum = Vector(tuple(max(corner[axis] for corner in bounds) for axis in range(3)))
            if props.collection_point_center == "bounds":
                center = matrix @ ((minimum + maximum) * 0.5)
            if props.collection_radius_mode == "bounds":
                extent = (maximum - minimum) * 0.5
                transform = matrix.to_3x3()
                radius = max(extent[axis] * transform.col[axis].length for axis in range(3))
                radius *= props.collection_radius_scale

        identity = obj.session_uid
        old_center = previous.get(identity)
        velocity = initial.copy()
        if old_center is not None:
            velocity += (center - old_center) * (inverse_dt * props.motion_velocity_scale)
        velocity *= props.velocity_scale
        positions.extend(center)
        radii.append(_point_radius(radius))
        velocities.extend(velocity)
        _append_point_defaults(values, props)
        current[identity] = center

    participant["collection_point_history"] = (signature, frame, current)
    return result


def collection_mesh_arrays(participant, depsgraph, frame, fps):
    host = participant["object"]
    props = host.fumaris
    collection = props.source_collection
    if collection is None:
        raise RuntimeError(f"{host.name}: assign a Source Collection for Collection Mesh")
    old_collection, old_frame, previous = participant.get(
        "collection_mesh_history", (None, frame, {})
    )
    delta_frames = float(frame) - float(old_frame)
    if old_collection != collection.session_uid or delta_frames <= 0.0:
        previous = {}
    inverse_dt = float(fps) / delta_frames if delta_frames > 0.0 else 0.0
    positions, indices, velocities, weights = array("f"), array("i"), array("f"), array("f")
    current = {}
    any_velocity = False
    layout_changed = False
    # Stable ordering also keeps interpolation aligned when object names change.
    objects = sorted(
        (obj for obj in collection.all_objects if obj != host and obj.type == "MESH"),
        key=lambda obj: obj.session_uid,
    )
    for obj in objects:
        identity = obj.session_uid
        old = previous.get(identity, {})
        member = dict(old)
        matrix = obj.evaluated_get(depsgraph).matrix_world.copy()
        local, faces, normals, emission_weights, _, _, _ = _cached_or_evaluated_mesh(
            member, obj, depsgraph, props, matrix, frame
        )
        world = array("f")
        for offset in range(0, len(local), 3):
            world.extend(matrix @ Vector(local[offset:offset + 3]))
        # Vertex correspondence is only safe when topology matches for this object.
        old_world = old.get("world") if old.get("faces") == faces else None
        layout_changed = layout_changed or old_world is None
        if old_world is not None and len(old_world) != len(world):
            old_world = None
            layout_changed = True
        offset = len(positions) // 3
        positions.extend(world)
        flipped = matrix.to_3x3().determinant() < 0.0
        layout_changed = layout_changed or old.get("flipped", flipped) != flipped
        if flipped:
            # Baking a reflection into vertices reverses winding and mask corners.
            for corner in range(0, len(faces), 3):
                indices.extend(faces[corner + index] + offset for index in (0, 2, 1))
                if emission_weights:
                    weights.extend(emission_weights[corner + index] for index in (0, 2, 1))
        else:
            indices.extend(index + offset for index in faces)
            weights.extend(emission_weights)
        for index, position in enumerate(world):
            velocity = normals[index] if normals else 0.0
            if old_world is not None:
                velocity += (position - old_world[index]) * inverse_dt * props.motion_velocity_scale
            velocities.append(velocity)
            any_velocity = any_velocity or abs(velocity) > 1e-12
        member["world"] = world
        member["faces"] = faces
        member["flipped"] = flipped
        current[identity] = member

    if not any_velocity:
        velocities = array("f")
    participant["collection_mesh_history"] = (collection.session_uid, frame, current)
    participant["collection_mesh_layout_changed"] = layout_changed or set(previous) != set(current)
    return positions, indices, velocities, weights
