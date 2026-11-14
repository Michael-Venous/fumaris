"""Sparks and embers driven by a baked Fumaris volume (Geometry Nodes).

A point-cloud object with a Simulation Zone: each frame it spawns points in
voxels hotter than Spawn Temperature, moves every spark through the baked
velocity (drag toward the gas velocity, gravity), keeps it hot while it sits in
hot gas and lets it cool otherwise, and removes it after its lifetime or once
it has cooled. A blackbody emission material colours sparks by temperature.
Needs a bake with the velocity and temperature grids (Output > Velocity).

One node group and one material serve every sparks object; each object keeps
its own values on its modifier. Its Volume input follows the bake its domain
shows (follow_bakes), through re-bakes and version switches.
"""
import os

import bpy

from .importers import VOLUME_MARKER
from .utils import peek_output_directory_for_object

GROUP_NAME = "Fumaris Sparks"
MATERIAL_NAME = "Fumaris Spark"
# Bump when the node group or the material changes: sparks already in a file
# keep the group they were made with, new ones get the new group.
VERSION = 2
VERSION_KEY = "fumaris_sparks_version"
DOMAIN_MARKER = "fumaris_sparks_domain"   # on a sparks object: its domain


def _link(tree, a, b):
    tree.links.new(a, b)


def _math(tree, operation, a=None, b=None, location=(0, 0)):
    node = tree.nodes.new("ShaderNodeMath")
    node.operation = operation
    node.location = location
    if a is not None:
        if isinstance(a, (int, float)):
            node.inputs[0].default_value = a
        else:
            _link(tree, a, node.inputs[0])
    if b is not None:
        if isinstance(b, (int, float)):
            node.inputs[1].default_value = b
        else:
            _link(tree, b, node.inputs[1])
    return node.outputs[0]


def _vmath(tree, operation, a, b=None, location=(0, 0), output=0):
    node = tree.nodes.new("ShaderNodeVectorMath")
    node.operation = operation
    node.location = location
    _link(tree, a, node.inputs[0])
    if b is not None:
        if isinstance(b, (int, float)):
            node.inputs["Scale"].default_value = b
        else:
            _link(tree, b, node.inputs[1] if operation != "SCALE" else node.inputs["Scale"])
    return node.outputs[output]


def _socket(tree, name, kind, default=None, minimum=None):
    item = tree.interface.new_socket(name, in_out="INPUT", socket_type=kind)
    if default is not None:
        item.default_value = default
    if minimum is not None and hasattr(item, "min_value"):
        item.min_value = minimum
    return item


def build_group():
    """The shared Sparks node group (built once per file)."""
    tree = next((group for group in bpy.data.node_groups
                 if group.bl_idname == "GeometryNodeTree" and group.get(VERSION_KEY) == VERSION), None)
    if tree is not None:
        return tree
    tree = bpy.data.node_groups.new(GROUP_NAME, "GeometryNodeTree")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    _socket(tree, "Volume", "NodeSocketObject")
    _socket(tree, "Spawn Temperature", "NodeSocketFloat", 1500.0, 0.0)
    _socket(tree, "Sparks per Frame", "NodeSocketFloat", 200.0, 0.0)
    _socket(tree, "Lifetime", "NodeSocketFloat", 1.5, 0.0)
    _socket(tree, "Drag", "NodeSocketFloat", 3.0, 0.0)
    _socket(tree, "Gravity", "NodeSocketFloat", 9.81)
    _socket(tree, "Cooling Time", "NodeSocketFloat", 0.6, 0.01)
    _socket(tree, "Spread", "NodeSocketFloat", 0.1, 0.0)
    _socket(tree, "Radius", "NodeSocketFloat", 0.02, 0.0)
    _socket(tree, "Seed", "NodeSocketInt", 0)

    nodes = tree.nodes
    group_in = nodes.new("NodeGroupInput")
    group_in.location = (-1600, 0)
    group_out = nodes.new("NodeGroupOutput")
    group_out.location = (1600, 0)

    info = nodes.new("GeometryNodeObjectInfo")
    info.transform_space = "RELATIVE"
    info.location = (-1400, 200)
    _link(tree, group_in.outputs["Volume"], info.inputs["Object"])
    temp_grid = nodes.new("GeometryNodeGetNamedGrid")
    temp_grid.data_type = "FLOAT"
    temp_grid.inputs["Name"].default_value = "temperature"
    temp_grid.location = (-1200, 300)
    _link(tree, info.outputs["Geometry"], temp_grid.inputs["Volume"])
    vel_grid = nodes.new("GeometryNodeGetNamedGrid")
    vel_grid.data_type = "VECTOR"
    vel_grid.inputs["Name"].default_value = "velocity"
    vel_grid.location = (-1200, 0)
    _link(tree, info.outputs["Geometry"], vel_grid.inputs["Volume"])

    sim_in = nodes.new("GeometryNodeSimulationInput")
    sim_out = nodes.new("GeometryNodeSimulationOutput")
    sim_in.pair_with_output(sim_out)
    sim_in.location = (-900, -200)
    sim_out.location = (1300, 0)
    dt = sim_in.outputs["Delta Time"]

    # --- move existing sparks -------------------------------------------
    position = nodes.new("GeometryNodeInputPosition")
    position.location = (-700, -500)
    get_v = nodes.new("GeometryNodeInputNamedAttribute")
    get_v.data_type = "FLOAT_VECTOR"
    get_v.inputs["Name"].default_value = "spark_velocity"
    get_v.location = (-700, -650)
    get_t = nodes.new("GeometryNodeInputNamedAttribute")
    get_t.data_type = "FLOAT"
    get_t.inputs["Name"].default_value = "spark_temperature"
    get_t.location = (-700, -800)
    get_age = nodes.new("GeometryNodeInputNamedAttribute")
    get_age.data_type = "FLOAT"
    get_age.inputs["Name"].default_value = "age"
    get_age.location = (-700, -950)
    get_life = nodes.new("GeometryNodeInputNamedAttribute")
    get_life.data_type = "FLOAT"
    get_life.inputs["Name"].default_value = "life"
    get_life.location = (-700, -1100)

    sample_v = nodes.new("GeometryNodeSampleGrid")
    sample_v.data_type = "VECTOR"
    sample_v.location = (-450, -500)
    _link(tree, vel_grid.outputs["Grid"], sample_v.inputs["Grid"])
    _link(tree, position.outputs["Position"], sample_v.inputs["Position"])
    sample_t = nodes.new("GeometryNodeSampleGrid")
    sample_t.data_type = "FLOAT"
    sample_t.location = (-450, -750)
    _link(tree, temp_grid.outputs["Grid"], sample_t.inputs["Grid"])
    _link(tree, position.outputs["Position"], sample_t.inputs["Position"])

    # v += (v_gas - v) * (1 - exp(-drag dt)) + g dt
    follow = _math(tree, "SUBTRACT", 1.0, _math(tree, "EXPONENT", _math(tree, "MULTIPLY",
        _math(tree, "MULTIPLY", group_in.outputs["Drag"], -1.0, (-450, -950)), dt, (-300, -950)), None, (-150, -950)), (0, -950))
    delta_v = _vmath(tree, "SUBTRACT", sample_v.outputs["Value"], get_v.outputs["Attribute"], (-200, -500))
    pulled = _vmath(tree, "SCALE", delta_v, follow, (0, -500))
    gravity = nodes.new("ShaderNodeCombineXYZ")
    gravity.location = (-200, -650)
    _link(tree, _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", group_in.outputs["Gravity"], -1.0, (-450, -1150)), dt, (-300, -1150)), gravity.inputs["Z"])
    new_v = _vmath(tree, "ADD", _vmath(tree, "ADD", get_v.outputs["Attribute"], pulled, (150, -500)), gravity.outputs["Vector"], (300, -500))
    step = _vmath(tree, "SCALE", new_v, dt, (450, -500))
    set_pos = nodes.new("GeometryNodeSetPosition")
    set_pos.location = (600, -300)
    _link(tree, sim_in.outputs["Geometry"], set_pos.inputs["Geometry"])
    _link(tree, step, set_pos.inputs["Offset"])
    store_v = nodes.new("GeometryNodeStoreNamedAttribute")
    store_v.data_type = "FLOAT_VECTOR"
    store_v.inputs["Name"].default_value = "spark_velocity"
    store_v.location = (750, -300)
    _link(tree, set_pos.outputs["Geometry"], store_v.inputs["Geometry"])
    _link(tree, new_v, store_v.inputs["Value"])
    # temperature: max(T exp(-dt/cool), gas T)
    cooled = _math(tree, "MULTIPLY", get_t.outputs["Attribute"], _math(tree, "EXPONENT",
        _math(tree, "DIVIDE", _math(tree, "MULTIPLY", dt, -1.0, (-200, -1300)), group_in.outputs["Cooling Time"], (-50, -1300)), None, (100, -1300)), (250, -1300))
    hot = _math(tree, "MAXIMUM", cooled, sample_t.outputs["Value"], (400, -1300))
    store_t = nodes.new("GeometryNodeStoreNamedAttribute")
    store_t.data_type = "FLOAT"
    store_t.inputs["Name"].default_value = "spark_temperature"
    store_t.location = (900, -300)
    _link(tree, store_v.outputs["Geometry"], store_t.inputs["Geometry"])
    _link(tree, hot, store_t.inputs["Value"])
    age = _math(tree, "ADD", get_age.outputs["Attribute"], dt, (400, -1500))
    store_age = nodes.new("GeometryNodeStoreNamedAttribute")
    store_age.data_type = "FLOAT"
    store_age.inputs["Name"].default_value = "age"
    store_age.location = (1050, -300)
    _link(tree, store_t.outputs["Geometry"], store_age.inputs["Geometry"])
    _link(tree, age, store_age.inputs["Value"])
    dead = nodes.new("FunctionNodeBooleanMath")
    dead.operation = "OR"
    dead.location = (900, -1500)
    _link(tree, _math(tree, "GREATER_THAN", age, get_life.outputs["Attribute"], (600, -1500)), dead.inputs[0])
    _link(tree, _math(tree, "LESS_THAN", hot, 700.0, (600, -1650)), dead.inputs[1])
    cull = nodes.new("GeometryNodeDeleteGeometry")
    cull.location = (1150, -300)
    _link(tree, store_age.outputs["Geometry"], cull.inputs["Geometry"])
    _link(tree, dead.outputs[0], cull.inputs["Selection"])

    # --- spawn new sparks in hot voxels ----------------------------------
    to_points = nodes.new("GeometryNodeGridToPoints")
    to_points.data_type = "FLOAT"
    to_points.location = (-900, 400)
    _link(tree, temp_grid.outputs["Grid"], to_points.inputs["Grid"])
    is_hot = _math(tree, "GREATER_THAN", to_points.outputs["Value"], group_in.outputs["Spawn Temperature"], (-650, 500))
    hot_count = nodes.new("GeometryNodeAttributeStatistic")
    hot_count.data_type = "FLOAT"
    hot_count.domain = "POINT"
    hot_count.location = (-450, 650)
    _link(tree, to_points.outputs["Points"], hot_count.inputs["Geometry"])
    _link(tree, is_hot, hot_count.inputs[2])
    probability = _math(tree, "DIVIDE", group_in.outputs["Sparks per Frame"], _math(tree, "MAXIMUM", hot_count.outputs["Sum"], 1.0, (-250, 650)), (-100, 650))
    frame = nodes.new("GeometryNodeInputSceneTime")
    frame.location = (-650, 800)
    seed = _math(tree, "ADD", frame.outputs["Frame"], group_in.outputs["Seed"], (-450, 850))
    chance = nodes.new("FunctionNodeRandomValue")
    chance.data_type = "BOOLEAN"
    chance.location = (50, 650)
    _link(tree, probability, chance.inputs["Probability"])
    _link(tree, seed, chance.inputs["Seed"])
    keep = nodes.new("FunctionNodeBooleanMath")
    keep.operation = "AND"
    keep.location = (250, 550)
    _link(tree, is_hot, keep.inputs[0])
    _link(tree, chance.outputs[0], keep.inputs[1])
    born = nodes.new("GeometryNodeDeleteGeometry")
    born.location = (450, 400)
    _link(tree, to_points.outputs["Points"], born.inputs["Geometry"])
    invert = nodes.new("FunctionNodeBooleanMath")
    invert.operation = "NOT"
    invert.location = (350, 550)
    _link(tree, keep.outputs[0], invert.inputs[0])
    _link(tree, invert.outputs[0], born.inputs["Selection"])
    jitter = nodes.new("FunctionNodeRandomValue")
    jitter.data_type = "FLOAT_VECTOR"
    jitter.location = (450, 200)
    _link(tree, _math(tree, "MULTIPLY", group_in.outputs["Spread"], -1.0, (250, 150)), jitter.inputs["Min"])
    _link(tree, group_in.outputs["Spread"], jitter.inputs["Max"])
    _link(tree, seed, jitter.inputs["Seed"])
    spawn_pos = nodes.new("GeometryNodeSetPosition")
    spawn_pos.location = (650, 400)
    _link(tree, born.outputs["Geometry"], spawn_pos.inputs["Geometry"])
    _link(tree, jitter.outputs[0], spawn_pos.inputs["Offset"])
    position2 = nodes.new("GeometryNodeInputPosition")
    position2.location = (450, 50)
    sample_v2 = nodes.new("GeometryNodeSampleGrid")
    sample_v2.data_type = "VECTOR"
    sample_v2.location = (650, 100)
    _link(tree, vel_grid.outputs["Grid"], sample_v2.inputs["Grid"])
    _link(tree, position2.outputs["Position"], sample_v2.inputs["Position"])
    life_random = nodes.new("FunctionNodeRandomValue")
    life_random.data_type = "FLOAT"
    life_random.location = (650, -50)
    life_random.inputs["Min"].default_value = 0.6
    life_random.inputs["Max"].default_value = 1.4
    _link(tree, seed, life_random.inputs["Seed"])
    life = _math(tree, "MULTIPLY", life_random.outputs[0], group_in.outputs["Lifetime"], (850, -50))
    geometry = spawn_pos.outputs["Geometry"]
    for name, data_type, value in (
        ("spark_velocity", "FLOAT_VECTOR", sample_v2.outputs["Value"]),
        ("spark_temperature", "FLOAT", to_points.outputs["Value"]),
        ("age", "FLOAT", 0.0),
        ("life", "FLOAT", life),
    ):
        store = nodes.new("GeometryNodeStoreNamedAttribute")
        store.data_type = data_type
        store.inputs["Name"].default_value = name
        store.location = (900 + 150 * len(name) % 450, 400)
        _link(tree, geometry, store.inputs["Geometry"])
        if isinstance(value, float):
            store.inputs["Value"].default_value = value
        else:
            _link(tree, value, store.inputs["Value"])
        geometry = store.outputs["Geometry"]
    join = nodes.new("GeometryNodeJoinGeometry")
    join.location = (1250, 100)
    _link(tree, geometry, join.inputs[0])
    _link(tree, cull.outputs["Geometry"], join.inputs[0])
    _link(tree, join.outputs[0], sim_out.inputs["Geometry"])

    radius = nodes.new("GeometryNodeSetPointRadius")
    radius.location = (1450, 0)
    _link(tree, sim_out.outputs["Geometry"], radius.inputs["Points"])
    _link(tree, group_in.outputs["Radius"], radius.inputs["Radius"])
    material = nodes.new("GeometryNodeSetMaterial")
    material.location = (1550, 0)
    material.inputs["Material"].default_value = build_material()
    _link(tree, radius.outputs["Points"], material.inputs["Geometry"])
    _link(tree, material.outputs["Geometry"], group_out.inputs["Geometry"])
    tree[VERSION_KEY] = VERSION
    return tree


def build_material():
    """The shared spark material (built once per file)."""
    material = next((item for item in bpy.data.materials if item.get(VERSION_KEY) == VERSION), None)
    if material is not None:
        return material
    material = bpy.data.materials.new(MATERIAL_NAME)
    material.use_nodes = True
    tree = material.node_tree
    tree.nodes.clear()
    attribute = tree.nodes.new("ShaderNodeAttribute")
    attribute.attribute_name = "spark_temperature"
    blackbody = tree.nodes.new("ShaderNodeBlackbody")
    emission = tree.nodes.new("ShaderNodeEmission")
    strength = tree.nodes.new("ShaderNodeMath")
    strength.operation = "MULTIPLY"
    strength.inputs[1].default_value = 0.01
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    tree.links.new(attribute.outputs["Fac"], blackbody.inputs["Temperature"])
    tree.links.new(blackbody.outputs["Color"], emission.inputs["Color"])
    tree.links.new(attribute.outputs["Fac"], strength.inputs[0])
    tree.links.new(strength.outputs[0], emission.inputs["Strength"])
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    material[VERSION_KEY] = VERSION
    return material


def _input(modifier, name):
    """A value of the Sparks group on one object's modifier, by its name."""
    item = next((item for item in modifier.node_group.interface.items_tree
                 if item.item_type == "SOCKET" and item.in_out == "INPUT" and item.name == name), None)
    return getattr(modifier.properties.inputs, item.identifier, None) if item else None


def shown_volume(domain):
    """The volume object of the bake the domain shows, if it is in the scene."""
    directory = peek_output_directory_for_object(domain)
    if not directory:
        return None
    directory = os.path.normpath(directory)
    return next((obj for obj in bpy.data.objects
                 if obj.type == "VOLUME" and obj.get(VOLUME_MARKER) == directory), None)


def missing_grids(path):
    """Grids the sparks need that a baked frame lacks."""
    try:
        import openvdb
        names = {grid.name for grid in openvdb.readAllGridMetadata(path)}
    except Exception:
        return []
    return [name for name in ("velocity", "temperature") if name not in names]


def follow_bakes():
    """Point every sparks object at the bake its domain shows now.

    A bake or an import replaces the volume object, and a version switch
    shows another one; the sparks go with the domain. A bake that is not in
    the scene leaves them empty until it is.
    """
    for obj in bpy.data.objects:
        domain = obj.get(DOMAIN_MARKER)
        if domain is None or not hasattr(domain, "fumaris"):
            continue
        volume = shown_volume(domain)
        for modifier in obj.modifiers:
            if modifier.type != "NODES" or modifier.node_group is None or not modifier.node_group.get(VERSION_KEY):
                continue
            target = _input(modifier, "Volume")
            if target is not None and target.value != volume:
                target.value = volume
                obj.update_tag()


def add_sparks(context, domain, volume, name="Fumaris Sparks"):
    """Point-cloud object running the Sparks group on the bake `domain` shows."""
    tree = build_group()
    mesh = bpy.data.meshes.new(name)
    obj = bpy.data.objects.new(name, mesh)
    context.scene.collection.objects.link(obj)
    modifier = obj.modifiers.new(name, "NODES")
    modifier.node_group = tree
    obj[DOMAIN_MARKER] = domain
    _input(modifier, "Volume").value = volume
    return obj
