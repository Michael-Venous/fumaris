from array import array

from mathutils import Vector

from .runtime import geometry_revision


def _evaluated_mesh(obj, depsgraph, props, matrix):
    evaluated = obj.evaluated_get(depsgraph)
    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    try:
        mesh.calc_loop_triangles()
        positions = array("f", [0.0]) * (len(mesh.vertices) * 3)
        mesh.vertices.foreach_get("co", positions)

        raw_indices = []
        emission_weights = array("f")
        mask = _mesh_emission_mask(obj, mesh, props)
        for triangle in mesh.loop_triangles:
            weights = _triangle_mask_weights(mask, triangle)
            if (
                weights
                and sum(weights) / len(weights)
                < props.mesh_emission_mask_threshold
            ):
                continue
            raw_indices.extend(triangle.vertices)
            emission_weights.extend(weights)
        indices = array("i", raw_indices)
        velocities = _mesh_normal_velocities(positions, indices, props.normal_velocity, matrix)
    finally:
        evaluated.to_mesh_clear()

    if not positions or not indices:
        raise RuntimeError(f"{obj.name} evaluated to an empty mesh")
    return positions, indices, velocities, emission_weights


def _cached_or_evaluated_mesh(participant, obj, depsgraph, props, matrix, frame):
    key = _static_mesh_cache_key(obj, depsgraph, props)
    if key is not None and participant.get("static_mesh_key") == key:
        cached = participant.get("static_mesh")
        if cached is not None:
            positions, indices = cached
            version = int(participant.get("static_mesh_version", frame))
            return positions, indices, array("f"), array("f"), version, version, True

    positions, indices, velocities, emission_weights = _evaluated_mesh(
        obj,
        depsgraph,
        props,
        matrix,
    )
    if key is not None:
        participant["static_mesh_key"] = key
        participant["static_mesh"] = (positions, indices)
        participant["static_mesh_version"] = frame
        position_version = frame
        topology_version = frame
    else:
        participant.pop("static_mesh_key", None)
        participant.pop("static_mesh", None)
        participant.pop("static_mesh_version", None)
        position_version = frame
        topology_version = frame
    return (
        positions,
        indices,
        velocities,
        emission_weights,
        position_version,
        topology_version,
        False,
    )


def _static_mesh_cache_key(obj, depsgraph, props):
    if obj.type != "MESH" or obj.modifiers or obj.data.shape_keys:
        return None
    if abs(float(props.normal_velocity)) > 1e-6:
        return None
    if str(getattr(props, "mesh_emission_mask_attribute", "") or "").strip():
        return None
    mesh = obj.data
    evaluated_mesh = obj.evaluated_get(depsgraph).data
    return (
        mesh.as_pointer(),
        evaluated_mesh.as_pointer(),
        geometry_revision(mesh),
        len(mesh.vertices),
        len(mesh.polygons),
        len(mesh.edges),
    )


def _particle_mesh(obj, depsgraph, props):
    return _instance_mesh(obj, depsgraph, "particle mesh", allow_self=False)


def _geometry_nodes_mesh(obj, depsgraph, props):
    positions = array("f")
    indices = array("i")
    velocities = array("f")
    emission_weights = array("f")
    evaluated = obj.evaluated_get(depsgraph)
    matrix = evaluated.matrix_world.copy()

    try:
        (
            direct_positions,
            direct_indices,
            direct_velocities,
            direct_weights,
        ) = _evaluated_mesh(obj, depsgraph, props, matrix)
    except RuntimeError:
        direct_positions = direct_indices = direct_velocities = None
    if direct_positions:
        for offset in range(0, len(direct_positions), 3):
            position = matrix @ Vector(direct_positions[offset:offset + 3])
            positions.extend((position.x, position.y, position.z))
        indices.extend(direct_indices)
        velocities.extend(direct_velocities)
        emission_weights.extend(direct_weights)

    try:
        instance_positions, instance_indices, _instance_velocities = _instance_mesh(
            obj,
            depsgraph,
            "Geometry Nodes mesh",
            allow_self=True,
        )
    except RuntimeError:
        instance_positions = instance_indices = None
    if instance_positions:
        index_offset = len(positions) // 3
        positions.extend(instance_positions)
        indices.extend(index_offset + index for index in instance_indices)
        if velocities:
            velocities.extend((0.0 for _ in range(len(instance_positions))))
        if str(getattr(props, "mesh_emission_mask_attribute", "") or "").strip():
            emission_weights.extend((1.0 for _ in range(len(instance_indices))))

    if not positions or not indices:
        raise RuntimeError(f"{obj.name} did not provide Geometry Nodes mesh geometry")
    return positions, indices, velocities, emission_weights


def _instance_mesh(obj, depsgraph, label, *, allow_self):
    positions = array("f")
    indices = array("i")
    velocities = array("f")

    for instance in depsgraph.object_instances:
        if not getattr(instance, "is_instance", False):
            continue
        parent = getattr(instance, "parent", None)
        source = getattr(instance, "object", None)
        if getattr(parent, "name", "") != obj.name:
            continue
        if (
            source is None
            or (source.name == obj.name and not allow_self)
            or getattr(source, "type", None) != "MESH"
        ):
            continue

        mesh = source.data
        mesh.calc_loop_triangles()
        offset = len(positions) // 3
        for vertex in mesh.vertices:
            position = instance.matrix_world @ vertex.co
            positions.extend((position.x, position.y, position.z))
        for triangle in mesh.loop_triangles:
            indices.extend(offset + vertex_index for vertex_index in triangle.vertices)

    if not positions or not indices:
        raise RuntimeError(f"{obj.name} did not provide {label} instances")
    return positions, indices, velocities


def _mesh_emission_mask(obj, mesh, props):
    name = str(getattr(props, "mesh_emission_mask_attribute", "") or "").strip()
    if not name:
        return None

    attribute = mesh.attributes.get(name)
    if attribute is not None:
        if attribute.domain == "FACE":
            return "FACE", array("f", (_mask_weight(value) for value in attribute.data))
        if attribute.domain in {"POINT", "VERTEX"}:
            return "POINT", array("f", (_mask_weight(value) for value in attribute.data))
        if attribute.domain == "CORNER":
            return "CORNER", array("f", (_mask_weight(value) for value in attribute.data))

    group = obj.vertex_groups.get(name)
    if group is None:
        raise RuntimeError(f'{obj.name} has no mesh attribute or vertex group named "{name}"')
    weights = array("f", [0.0]) * len(mesh.vertices)
    for vertex in mesh.vertices:
        weight = 0.0
        for membership in vertex.groups:
            if membership.group == group.index:
                weight = membership.weight
                break
        weights[vertex.index] = max(0.0, min(1.0, weight))
    return "POINT", weights


def _triangle_mask_weights(mask, triangle):
    if mask is None:
        return ()
    domain, values = mask
    if domain == "FACE":
        return (values[triangle.polygon_index],) * len(triangle.vertices)
    indices = triangle.loops if domain == "CORNER" else triangle.vertices
    return tuple(values[index] for index in indices)


def _mask_weight(value):
    return max(0.0, min(1.0, _attribute_scalar(value, 1.0)))


def _attribute_scalar(value, default):
    if hasattr(value, "value"):
        return float(value.value)
    if hasattr(value, "vector"):
        return float(value.vector[0])
    if hasattr(value, "color"):
        return float(value.color[0])
    return default


def _mesh_normal_velocities(positions, indices, normal_velocity, matrix):
    normal_velocity = float(normal_velocity)
    if abs(normal_velocity) < 1e-6:
        return array("f")

    normals = [Vector((0.0, 0.0, 0.0)) for _ in range(len(positions) // 3)]
    for cursor in range(0, len(indices), 3):
        i0, i1, i2 = indices[cursor], indices[cursor + 1], indices[cursor + 2]
        p0 = Vector(positions[i0 * 3:i0 * 3 + 3])
        p1 = Vector(positions[i1 * 3:i1 * 3 + 3])
        p2 = Vector(positions[i2 * 3:i2 * 3 + 3])
        normal = (p1 - p0).cross(p2 - p0)
        normals[i0] += normal
        normals[i1] += normal
        normals[i2] += normal

    velocities = array("f")
    for normal in normals:
        if normal.length > 1e-6:
            normal.normalize()
            world_normal = matrix.to_3x3() @ normal
            if world_normal.length > 1e-6:
                world_normal.normalize()
            velocities.extend((world_normal.x * normal_velocity, world_normal.y * normal_velocity, world_normal.z * normal_velocity))
        else:
            velocities.extend((0.0, 0.0, 0.0))
    return velocities


def _add_mesh_motion_velocities(velocities, positions, previous_positions, matrix, scale, context_fps):
    scale = float(scale)
    if abs(scale) < 1e-6 or previous_positions is None:
        return
    if len(previous_positions) != len(positions):
        return
    velocity_scale = max(0.0, context_fps) * scale
    rotation_scale = matrix.to_3x3()
    deformation_velocities = array("f", [0.0]) * len(positions)
    changed = False
    for offset in range(0, len(positions), 3):
        deformation = Vector(positions[offset:offset + 3]) - Vector(previous_positions[offset:offset + 3])
        velocity = (rotation_scale @ deformation) * velocity_scale
        deformation_velocities[offset] = velocity.x
        deformation_velocities[offset + 1] = velocity.y
        deformation_velocities[offset + 2] = velocity.z
        changed = changed or velocity.length_squared > 1e-12
    if not changed:
        return
    if not velocities:
        velocities.extend(deformation_velocities)
        return
    for offset, value in enumerate(deformation_velocities):
        velocities[offset] += value


def _add_mesh_initial_velocity(velocities, positions, matrix, initial_velocity):
    initial = matrix.to_3x3() @ Vector(initial_velocity)
    if initial.length_squared <= 1e-12:
        return
    if not velocities:
        velocities.extend((0.0 for _ in range(len(positions))))
    for offset in range(0, len(positions), 3):
        velocities[offset] += initial.x
        velocities[offset + 1] += initial.y
        velocities[offset + 2] += initial.z
