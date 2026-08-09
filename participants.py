from .diagnostics import console_log


def collection_signature(collection):
    if collection is None:
        return None
    return tuple(
        (path, tuple(obj.name for obj in objects))
        for path, objects in collection_tree(collection)
    )


def participants(domain_props):
    if domain_props.emitter_collection is None:
        raise RuntimeError("Assign an emitter collection before baking")

    result = []
    skipped = []
    seen = set()

    def add_collection(collection, role):
        if collection is None:
            return
        for path, objects in collection_tree(collection):
            for obj in objects:
                key = obj.as_pointer()
                if key in seen or not hasattr(obj, "fumaris"):
                    continue
                if obj.fumaris.smoke_object_type != role:
                    continue
                seen.add(key)
                item = participant_for_object(
                    obj,
                    role,
                    len(result) + 1,
                    path,
                )
                if item is None:
                    skipped.append(f"{path}/{obj.name} ({role})")
                else:
                    result.append(item)

    add_collection(domain_props.emitter_collection, "emitter")
    add_collection(domain_props.collider_collection, "collider")
    add_collection(domain_props.effector_collection, "effector")
    add_collection(domain_props.outflow_collection, "outflow")

    if skipped:
        console_log(
            "Fumaris skipped unsupported participants: " + ", ".join(skipped)
        )
    return result


def collection_tree(collection, parent_path=""):
    path = f"{parent_path}/{collection.name}" if parent_path else collection.name
    objects = tuple(sorted(collection.objects, key=lambda item: item.name))
    yield path, objects
    for child in sorted(collection.children, key=lambda item: item.name):
        yield from collection_tree(child, path)


def participant_for_object(obj, role, participant_id, collection_name):
    props = obj.fumaris

    def item(kind):
        return {
            "id": participant_id,
            "object": obj,
            "role": role,
            "kind": kind,
            "collection": collection_name,
        }

    shape = participant_shape(props)
    if role == "collider":
        collider_type = getattr(props, "collider_type", "mesh")
        if collider_type == "box":
            return item("box")
        if collider_type == "sphere":
            return item("sphere")
        return item("mesh") if obj.type == "MESH" else None
    if role == "effector":
        return item("effector")
    if role == "outflow":
        return item("sphere") if shape == "sphere" or obj.type != "MESH" else item("mesh")
    if shape == "mesh" and obj.type == "MESH":
        return item("mesh")
    if shape == "box":
        return item("box")
    if shape == "sphere":
        return item("sphere")
    if is_particles(props, "point_cloud"):
        return item("point_spheres")
    if is_particles(props, "mesh") and obj.type == "MESH":
        return item("mesh")
    if is_geometry_nodes(props, "point_cloud"):
        return item("point_spheres")
    if is_geometry_nodes(props, "mesh") and obj.type == "MESH":
        return item("mesh")
    if shape == "openvdb" or is_geometry_nodes(props, "volume"):
        return item("volume")
    return None


def participant_shape(props):
    shape = props.participant_type
    if shape in {"gn_point_cloud", "gn_volume"}:
        return "geometry_nodes"
    return shape


def gn_subtype(props):
    shape = props.participant_type
    if shape == "gn_point_cloud":
        return "point_cloud"
    if shape == "gn_volume":
        return "volume"
    return getattr(props, "gn_subtype", "point_cloud")


def is_geometry_nodes(props, subtype=None):
    if participant_shape(props) != "geometry_nodes":
        return False
    return subtype is None or gn_subtype(props) == subtype


def particle_subtype(props):
    return getattr(props, "particle_subtype", "point_cloud")


def is_particles(props, subtype=None):
    if participant_shape(props) != "particles":
        return False
    return subtype is None or particle_subtype(props) == subtype
