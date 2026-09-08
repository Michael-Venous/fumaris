from array import array

from mathutils import Vector

try:
    import numpy as np
except ImportError:
    np = None

from .participants import is_geometry_nodes as _is_geometry_nodes
from .participants import is_particles as _is_particles


MIN_POINT_RADIUS = 0.05


def _empty_point_arrays():
    return array("f"), array("f"), array("f"), _point_value_arrays()


def _attribute_scalar(value, default):
    if hasattr(value, "value"):
        return float(value.value)
    if hasattr(value, "vector"):
        return float(value.vector[0])
    if hasattr(value, "color"):
        return float(value.color[0])
    return default


def _effective_divergence_coupling(props, divergence):
    explicit = float(props.couple_rate_divergence)
    if explicit > 0.0:
        return explicit
    return max(0.0, float(props.couple_rate_smoke)) if abs(divergence) > 1e-6 else 0.0


def _point_arrays(obj, props, depsgraph):
    if _is_particles(props, "point_cloud"):
        return _particle_arrays(obj, props, depsgraph)
    if _is_geometry_nodes(props, "point_cloud"):
        return _gn_point_cloud_arrays(obj, props, depsgraph)
    return _mesh_point_arrays(obj, props, depsgraph)


def _gn_point_cloud_arrays(obj, props, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    data = getattr(evaluated, "data", None)
    arrays = _attribute_point_arrays(data, evaluated.matrix_world, props)
    if arrays is not None and arrays[0]:
        return arrays

    point_positions = _point_cloud_positions(data, evaluated.matrix_world)
    if point_positions is not None and point_positions[1] > 0:
        return _point_cloud_attribute_arrays(data, evaluated.matrix_world, point_positions, props)

    mesh = None
    try:
        mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        arrays = _attribute_point_arrays(mesh, evaluated.matrix_world, props)
        if arrays is not None and arrays[0]:
            return arrays
    finally:
        if mesh is not None:
            evaluated.to_mesh_clear()

    instance_points = _instance_point_arrays(obj, props, depsgraph, allow_instance_origins=False)
    if instance_points is not None and instance_points[0]:
        return instance_points

    return _empty_point_arrays()


def _point_radius(value):
    return max(float(value), MIN_POINT_RADIUS)


def _point_radius_from_attrs(attrs, props, aliases, index):
    scale = _scalar_attr(attrs, props.attr_radius, aliases, index, 1.0)
    return _point_radius(props.point_radius * max(0.0, scale))


def _attribute_point_arrays(data, matrix, props):
    attributes = getattr(data, "attributes", None)
    if attributes is None:
        return None

    attrs = _attribute_cache(data)
    position_attr = _find_attr(attrs, props.attr_position, ("position", "Position"))
    if position_attr is None or len(position_attr.data) == 0:
        return None

    count = len(position_attr.data)
    local_positions = _bulk_vector_attribute(position_attr, count)
    if local_positions is None:
        return None
    motion_attr = _find_attr(attrs, props.attr_velocity, ("velocity", "Velocity", "vel", "v"))
    motions = _bulk_vector_attribute(motion_attr, count)
    if motions is None:
        motions = array("f", [0.0]) * (count * 3)
    masks = _bulk_scalar_attribute(attrs, props.attr_mask, ("enabled", "mask", "emit"), count, 1.0)
    radii = _bulk_scalar_attribute(attrs, props.attr_radius, ("__physx_radius", "__physx_Radius", "__physx_width", "__physx_pscale", "radius", "Radius", "width", "pscale"), count, 1.0)
    point_values = _bulk_point_values(attrs, props, count)

    if np is not None:
        # Keep all channels aligned with the same mask and transform in bulk.
        active = np.logical_not(np.frombuffer(masks, dtype=np.float32) <= 0.0)
        local = np.frombuffer(local_positions, dtype=np.float32).reshape((-1, 3))[active]
        motion = np.frombuffer(motions, dtype=np.float32).reshape((-1, 3))[active]
        transform = np.asarray(matrix, dtype=np.float32)
        positions = local @ transform[:3, :3].T + transform[:3, 3]
        motion *= np.float32(props.motion_velocity_scale)
        motion += np.asarray(props.velocity, dtype=np.float32)
        velocities = (motion @ transform[:3, :3].T) * np.float32(props.velocity_scale)
        radii = np.frombuffer(radii, dtype=np.float32)[active]
        widths = np.maximum(
            np.fmax(radii, 0.0) * np.float32(props.point_radius), MIN_POINT_RADIUS
        )
        values = {
            name: _float_array(np.frombuffer(data, dtype=np.float32)[active])
            for name, data in point_values.items()
        }
        return _float_array(positions), _float_array(widths), _float_array(velocities), values

    positions = array("f")
    widths = array("f")
    velocities = array("f")
    values = _point_value_arrays()
    initial = Vector(props.velocity)
    vector_transform = matrix.to_3x3()
    motion_scale = float(props.motion_velocity_scale)
    velocity_scale = float(props.velocity_scale)
    for index in range(count):
        if masks[index] <= 0.0:
            continue
        source = index * 3
        position = matrix @ Vector(local_positions[source:source + 3])
        motion = Vector(motions[source:source + 3])
        velocity = (vector_transform @ (initial + motion * motion_scale)) * velocity_scale
        positions.extend((position.x, position.y, position.z))
        widths.append(_point_radius(props.point_radius * max(0.0, radii[index])))
        velocities.extend((velocity.x, velocity.y, velocity.z))
        for name, data in point_values.items():
            values[name].append(data[index])
    return positions, widths, velocities, values


def _float_array(values):
    result = array("f")
    result.frombytes(values.astype(np.float32, copy=False).tobytes())
    return result


def _bulk_vector_attribute(attribute, count):
    if attribute is None or len(attribute.data) < count:
        return None
    first = attribute.data[0] if count else None
    property_name = "vector" if first is not None and hasattr(first, "vector") else None
    if property_name is None:
        return None
    values = array("f", [0.0]) * (count * 3)
    try:
        attribute.data.foreach_get(property_name, values)
    except (AttributeError, TypeError, ValueError):
        return None
    return values


def _bulk_scalar_attribute(attrs, explicit, fallbacks, count, default):
    attribute = _find_attr(attrs, explicit, fallbacks)
    if attribute is None or len(attribute.data) < count:
        return array("f", [float(default)]) * count
    first = attribute.data[0] if count else None
    property_name = "value" if first is not None and hasattr(first, "value") else None
    if property_name is None:
        return array("f", (_attribute_scalar(attribute.data[index], default) for index in range(count)))
    values = array("f", [0.0]) * count
    try:
        attribute.data.foreach_get(property_name, values)
    except (AttributeError, TypeError, ValueError):
        return array("f", (_attribute_scalar(attribute.data[index], default) for index in range(count)))
    return values


def _bulk_point_values(attrs, props, count):
    divergences = _bulk_scalar_attribute(attrs, props.attr_divergence, ("divergence", "expansion", "pressure"), count, props.emitter_divergence)
    values = {
        "divergences": divergences,
        "temperatures": _bulk_scalar_attribute(attrs, props.attr_temperature, ("temperature", "temp", "heat"), count, props.emitter_temperature),
        "fuels": _bulk_scalar_attribute(attrs, props.attr_fuel, ("fuel", "combustible"), count, props.emitter_fuel),
        "burns": _bulk_scalar_attribute(attrs, props.attr_burn, ("burn", "flame", "fire"), count, props.emitter_burn),
        "smokes": _bulk_scalar_attribute(attrs, props.attr_smoke, ("smoke", "density", "mass"), count, props.emitter_smoke),
        "couple_rate_velocities": _bulk_scalar_attribute(attrs, props.attr_velocity_coupling, ("velocity_coupling", "force_strength", "strength"), count, props.couple_rate_velocity),
        "couple_rate_temperatures": _bulk_scalar_attribute(attrs, props.attr_temperature_coupling, ("temperature_coupling", "heat_coupling"), count, props.couple_rate_temperature),
        "couple_rate_fuels": array("f", [props.couple_rate_fuel]) * count,
        "couple_rate_burns": array("f", [props.couple_rate_burn]) * count,
        "couple_rate_smokes": _bulk_scalar_attribute(attrs, props.attr_smoke_coupling, ("smoke_coupling", "density_coupling"), count, props.couple_rate_smoke),
    }
    if np is not None:
        explicit = float(props.couple_rate_divergence)
        coupling = np.full(count, max(0.0, explicit), dtype=np.float32)
        if explicit <= 0.0:
            coupling[np.abs(np.frombuffer(divergences, dtype=np.float32)) > 1e-6] = max(
                0.0, float(props.couple_rate_smoke)
            )
        values["couple_rate_divergences"] = _float_array(coupling)
    else:
        values["couple_rate_divergences"] = array(
            "f", (_effective_divergence_coupling(props, value) for value in divergences),
        )
    return values


def _world_vector(matrix, value):
    vector = Vector(value)
    if matrix is not None and vector.length > 0.0:
        vector = matrix.to_3x3() @ vector
    return (vector.x, vector.y, vector.z)


def _point_velocity(attrs, index, matrix, props):
    motion = _vector_attr(attrs, props.attr_velocity, ("velocity", "Velocity", "vel", "v"), index, (0.0, 0.0, 0.0))
    velocity = Vector(props.velocity) + Vector(motion) * float(props.motion_velocity_scale)
    world = Vector(_world_vector(matrix, velocity)) * float(props.velocity_scale)
    return world.x, world.y, world.z


def _particle_arrays(obj, props, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    systems = list(evaluated.particle_systems)
    if props.particle_system_name:
        systems = [system for system in systems if system.name == props.particle_system_name]
    positions = array("f")
    widths = array("f")
    velocities = array("f")
    values = _point_value_arrays()
    for system in systems:
        for particle in system.particles:
            if getattr(particle, "alive_state", "ALIVE") != "ALIVE":
                continue
            location = Vector(particle.location[:])
            velocity = Vector(particle.velocity[:])
            initial_velocity = Vector(_world_vector(evaluated.matrix_world, props.velocity))
            exported_velocity = (
                initial_velocity + velocity * float(props.motion_velocity_scale)
            ) * float(props.velocity_scale)
            positions.extend((location.x, location.y, location.z))
            widths.append(_point_radius(props.point_radius))
            velocities.extend((exported_velocity.x, exported_velocity.y, exported_velocity.z))
            _append_point_defaults(values, props)
    return positions, widths, velocities, values


def _direct_point_arrays(evaluated, props):
    data = getattr(evaluated, "data", None)
    arrays = _attribute_point_arrays(data, evaluated.matrix_world, props)
    if arrays is not None:
        return arrays

    point_positions = _point_cloud_positions(data, evaluated.matrix_world)
    if point_positions is None:
        return None
    return _point_cloud_attribute_arrays(data, evaluated.matrix_world, point_positions, props)


def _point_cloud_positions(data, matrix):
    points = getattr(data, "points", None)
    if points is None:
        return None
    try:
        count = len(points)
    except TypeError:
        return None
    if count <= 0:
        return None

    local = array("f", [0.0]) * (count * 3)
    try:
        points.foreach_get("co", local)
    except Exception:
        return None

    positions = array("f")
    for index in range(count):
        position = matrix @ Vector(local[index * 3:index * 3 + 3])
        positions.extend((position.x, position.y, position.z))
    return positions, count


def _point_cloud_attribute_arrays(data, matrix, point_positions, props):
    source_positions, count = point_positions
    attrs = _attribute_cache(data) if getattr(data, "attributes", None) is not None else {}
    positions = array("f")
    widths = array("f")
    velocities = array("f")
    values = _point_value_arrays()

    for index in range(count):
        if _scalar_attr(attrs, props.attr_mask, ("enabled", "mask", "emit"), index, 1.0) <= 0.0:
            continue
        source = index * 3
        positions.extend(source_positions[source:source + 3])
        widths.append(_point_radius_from_attrs(attrs, props, ("__physx_radius", "__physx_Radius", "__physx_width", "__physx_pscale", "radius", "Radius", "width", "pscale"), index))
        velocities.extend(_point_velocity(attrs, index, matrix, props))
        _append_point_values(values, props, attrs, index)
    return positions, widths, velocities, values


def _instance_point_arrays(obj, props, depsgraph, *, allow_instance_origins=True):
    positions = array("f")
    widths = array("f")
    velocities = array("f")
    values = _point_value_arrays()

    def append_arrays(arrays):
        source_positions, source_widths, source_velocities, source_values = arrays
        positions.extend(source_positions)
        widths.extend(source_widths)
        velocities.extend(source_velocities)
        for name, data in source_values.items():
            values[name].extend(data)

    for instance in depsgraph.object_instances:
        if not getattr(instance, "is_instance", False):
            continue
        parent = getattr(instance, "parent", None)
        source = getattr(instance, "object", None)
        parent_name = getattr(parent, "name", "")
        source_name = getattr(source, "name", "")
        if parent_name != obj.name and source_name != obj.name:
            continue

        data = getattr(source, "data", None)
        if getattr(source, "type", None) == "POINTCLOUD":
            arrays = _attribute_point_arrays(data, instance.matrix_world, props)
            if arrays is None or not arrays[0]:
                point_positions = _point_cloud_positions(data, instance.matrix_world)
                if point_positions is not None:
                    arrays = _point_cloud_attribute_arrays(data, instance.matrix_world, point_positions, props)
            if arrays is not None and arrays[0]:
                append_arrays(arrays)
            continue

        if not allow_instance_origins:
            continue

        position = instance.matrix_world.translation
        positions.extend((position.x, position.y, position.z))
        widths.append(_point_radius(props.point_radius))
        velocity = Vector(_world_vector(instance.matrix_world, props.velocity)) * float(props.velocity_scale)
        velocities.extend((velocity.x, velocity.y, velocity.z))
        _append_point_defaults(values, props)
    if not positions:
        return None
    return positions, widths, velocities, values


def _mesh_point_arrays(obj, props, depsgraph):
    evaluated = obj.evaluated_get(depsgraph)
    direct = _direct_point_arrays(evaluated, props)
    if direct is not None:
        return direct

    instance_points = _instance_point_arrays(obj, props, depsgraph)
    if instance_points is not None:
        return instance_points

    mesh = evaluated.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
    positions = array("f")
    widths = array("f")
    velocities = array("f")
    values = _point_value_arrays()
    try:
        matrix = evaluated.matrix_world
        attrs = _attribute_cache(mesh)
        for index, vertex in enumerate(mesh.vertices):
            if _scalar_attr(attrs, props.attr_mask, ("enabled", "mask", "emit"), index, 1.0) <= 0.0:
                continue
            position = matrix @ vertex.co
            velocity = _point_velocity(attrs, index, matrix, props)
            positions.extend((position.x, position.y, position.z))
            widths.append(_point_radius_from_attrs(attrs, props, ("radius", "Radius", "width", "pscale"), index))
            velocities.extend(velocity)
            _append_point_values(values, props, attrs, index)
    finally:
        evaluated.to_mesh_clear()
    return positions, widths, velocities, values


def _point_value_arrays():
    return {name: array("f") for name in ("divergences", "temperatures", "fuels", "burns", "smokes", "couple_rate_velocities", "couple_rate_divergences", "couple_rate_temperatures", "couple_rate_fuels", "couple_rate_burns", "couple_rate_smokes")}


def _append_point_defaults(values, props, weight=1.0):
    divergence = props.emitter_divergence * weight
    values["divergences"].append(props.emitter_divergence * weight)
    values["temperatures"].append(props.emitter_temperature * weight)
    values["fuels"].append(props.emitter_fuel * weight)
    values["burns"].append(props.emitter_burn * weight)
    values["smokes"].append(props.emitter_smoke * weight)
    values["couple_rate_velocities"].append(props.couple_rate_velocity)
    values["couple_rate_divergences"].append(_effective_divergence_coupling(props, divergence))
    values["couple_rate_temperatures"].append(props.couple_rate_temperature)
    values["couple_rate_fuels"].append(props.couple_rate_fuel)
    values["couple_rate_burns"].append(props.couple_rate_burn)
    values["couple_rate_smokes"].append(props.couple_rate_smoke)


def _append_point_values(values, props, attrs, index):
    divergence = _scalar_attr(attrs, props.attr_divergence, ("divergence", "expansion", "pressure"), index, props.emitter_divergence)
    values["divergences"].append(divergence)
    values["temperatures"].append(_scalar_attr(attrs, props.attr_temperature, ("temperature", "temp", "heat"), index, props.emitter_temperature))
    values["fuels"].append(_scalar_attr(attrs, props.attr_fuel, ("fuel", "combustible"), index, props.emitter_fuel))
    values["burns"].append(_scalar_attr(attrs, props.attr_burn, ("burn", "flame", "fire"), index, props.emitter_burn))
    values["smokes"].append(_scalar_attr(attrs, props.attr_smoke, ("smoke", "density", "mass"), index, props.emitter_smoke))
    values["couple_rate_velocities"].append(_scalar_attr(attrs, props.attr_velocity_coupling, ("velocity_coupling", "force_strength", "strength"), index, props.couple_rate_velocity))
    values["couple_rate_divergences"].append(_effective_divergence_coupling(props, divergence))
    values["couple_rate_temperatures"].append(_scalar_attr(attrs, props.attr_temperature_coupling, ("temperature_coupling", "heat_coupling"), index, props.couple_rate_temperature))
    values["couple_rate_fuels"].append(props.couple_rate_fuel)
    values["couple_rate_burns"].append(props.couple_rate_burn)
    values["couple_rate_smokes"].append(_scalar_attr(attrs, props.attr_smoke_coupling, ("smoke_coupling", "density_coupling"), index, props.couple_rate_smoke))


def _attribute_cache(mesh):
    return {attribute.name: attribute for attribute in mesh.attributes if attribute.domain in {"POINT", "VERTEX", "INSTANCE"}}


def _find_attr(attrs, explicit, fallbacks):
    names = [explicit.strip()] if explicit and explicit.strip() else []
    names.extend(fallbacks)
    for name in names:
        if name in attrs:
            return attrs[name]
    return None


def _scalar_attr(attrs, explicit, fallbacks, index, default):
    attr = _find_attr(attrs, explicit, fallbacks)
    if attr is None or index >= len(attr.data):
        return default
    value = attr.data[index]
    if hasattr(value, "value"):
        return float(value.value)
    if hasattr(value, "vector"):
        return float(value.vector[0])
    if hasattr(value, "color"):
        return float(value.color[0])
    return default


def _vector_attr(attrs, explicit, fallbacks, index, default):
    attr = _find_attr(attrs, explicit, fallbacks)
    if attr is None or index >= len(attr.data):
        return default
    value = attr.data[index]
    if hasattr(value, "vector"):
        return value.vector[:]
    if hasattr(value, "color"):
        return value.color[:3]
    if hasattr(value, "value"):
        return (float(value.value), 0.0, 0.0)
    return default
