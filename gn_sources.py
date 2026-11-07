"""Ready-made Geometry Nodes sources: Candle, Burst, Trail and Spreading Fire.

Each is a point-cloud emitter (Participant Type: Geometry Nodes, point cloud) whose
node group writes the per-point attributes the exporter reads: `mask` (emit or
not), `radius` (metres, Point Radius is 1), `velocity`, and channel overrides
(`temperature`, `fuel`, `smoke`). Time comes from the scene, so the sources are
animated without keyframes; all inputs are on the Geometry Nodes modifier.
"""
import bpy

from .sparks import _link, _math, _vmath, _socket

SOURCES = (
    ("CANDLE", "Candle", "A small flickering flame that burns where it sits (about 4 cm: needs fine cells, Resolution 512 or more for a few-metre domain)"),
    ("BURST", "Burst", "An expanding shell of fuel and heat over a few frames, for blasts and puffs"),
    ("TRAIL", "Trail", "Fire that travels along a curve: a fuse, a comet, a burning streak"),
    ("SPREADING", "Spreading Fire", "A burning front that spreads across a surface from an ignition point"),
)


def _new_tree(name):
    tree = bpy.data.node_groups.get(name)
    if tree is None:
        tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
    tree.nodes.clear()
    tree.interface.clear()
    tree.interface.new_socket("Geometry", in_out="INPUT", socket_type="NodeSocketGeometry")
    tree.interface.new_socket("Geometry", in_out="OUTPUT", socket_type="NodeSocketGeometry")
    return tree


def _store(tree, geometry, name, value, data_type="FLOAT", location=(0, 0)):
    node = tree.nodes.new("GeometryNodeStoreNamedAttribute")
    node.data_type = data_type
    node.domain = "POINT"
    node.location = location
    node.inputs["Name"].default_value = name
    _link(tree, geometry, node.inputs["Geometry"])
    if isinstance(value, (int, float)):
        node.inputs["Value"].default_value = value
    else:
        _link(tree, value, node.inputs["Value"])
    return node.outputs["Geometry"]


def _io(tree):
    group_in = tree.nodes.new("NodeGroupInput")
    group_in.location = (-1200, 0)
    group_out = tree.nodes.new("NodeGroupOutput")
    group_out.location = (1400, 0)
    time = tree.nodes.new("GeometryNodeInputSceneTime")
    time.location = (-1200, -400)
    return group_in, group_out, time


def _window(tree, frame, start, duration, location=(0, 0)):
    """1 while start <= frame < start + duration, else 0; and progress 0..1."""
    since = _math(tree, "SUBTRACT", frame, start, location)
    progress = _math(tree, "DIVIDE", since, _math(tree, "MAXIMUM", duration, 1.0, location), location)
    after = _math(tree, "GREATER_THAN", since, -0.5, location)
    before = _math(tree, "LESS_THAN", progress, 1.0, location)
    return _math(tree, "MULTIPLY", after, before, location), progress


def build_candle(name="Fumaris Candle"):
    tree = _new_tree(name)
    _socket(tree, "Flame Size", "NodeSocketFloat", 0.04, 0.0)
    _socket(tree, "Flicker", "NodeSocketFloat", 0.35, 0.0)
    _socket(tree, "Flicker Speed", "NodeSocketFloat", 6.0, 0.0)
    _socket(tree, "Rise Speed", "NodeSocketFloat", 0.3)
    group_in, group_out, time = _io(tree)
    points = tree.nodes.new("GeometryNodePoints")
    points.location = (-800, 200)
    points.inputs["Count"].default_value = 1
    # Flicker: noise over time, centred on 1.
    combine = tree.nodes.new("ShaderNodeCombineXYZ")
    combine.location = (-800, -200)
    _link(tree, _math(tree, "MULTIPLY", time.outputs["Seconds"], group_in.outputs["Flicker Speed"], (-1000, -200)), combine.inputs["X"])
    noise = tree.nodes.new("ShaderNodeTexNoise")
    noise.location = (-600, -200)
    noise.inputs["Scale"].default_value = 1.0
    _link(tree, combine.outputs["Vector"], noise.inputs["Vector"])
    wobble = _math(tree, "MULTIPLY", _math(tree, "SUBTRACT", noise.outputs["Fac"], 0.5, (-400, -200)), 2.0, (-300, -200))
    scale = _math(tree, "ADD", 1.0, _math(tree, "MULTIPLY", wobble, group_in.outputs["Flicker"], (-200, -200)), (-100, -200))
    radius = _math(tree, "MULTIPLY", group_in.outputs["Flame Size"], scale, (0, -200))
    rise = tree.nodes.new("ShaderNodeCombineXYZ")
    rise.location = (0, -400)
    _link(tree, group_in.outputs["Rise Speed"], rise.inputs["Z"])
    geometry = _store(tree, points.outputs["Points"], "radius", radius, location=(300, 200))
    geometry = _store(tree, geometry, "velocity", rise.outputs["Vector"], "FLOAT_VECTOR", (500, 200))
    _link(tree, geometry, group_out.inputs["Geometry"])
    return tree


def build_burst(name="Fumaris Burst"):
    tree = _new_tree(name)
    _socket(tree, "Start Frame", "NodeSocketFloat", 1.0)
    _socket(tree, "Duration", "NodeSocketFloat", 6.0, 1.0)
    _socket(tree, "Radius", "NodeSocketFloat", 0.5, 0.0)
    _socket(tree, "Growth", "NodeSocketFloat", 2.0, 0.0)
    _socket(tree, "Speed", "NodeSocketFloat", 8.0)
    _socket(tree, "Detail", "NodeSocketInt", 3, 1)
    group_in, group_out, time = _io(tree)
    active, progress = _window(tree, time.outputs["Frame"], group_in.outputs["Start Frame"], group_in.outputs["Duration"], (-1000, -300))
    sphere = tree.nodes.new("GeometryNodeMeshIcoSphere")
    sphere.location = (-800, 200)
    # The shell grows from Radius to Radius x (1 + Growth) over Duration.
    grow = _math(tree, "ADD", 1.0, _math(tree, "MULTIPLY", _math(tree, "MINIMUM", progress, 1.0, (-900, 0)), group_in.outputs["Growth"], (-900, 0)), (-900, 0))
    _link(tree, _math(tree, "MULTIPLY", group_in.outputs["Radius"], grow, (-900, 100)), sphere.inputs["Radius"])
    _link(tree, group_in.outputs["Detail"], sphere.inputs["Subdivisions"])
    to_points = tree.nodes.new("GeometryNodeMeshToPoints")
    to_points.location = (-600, 200)
    _link(tree, sphere.outputs["Mesh"], to_points.inputs["Mesh"])
    position = tree.nodes.new("GeometryNodeInputPosition")
    position.location = (-600, -100)
    outward = _vmath(tree, "NORMALIZE", position.outputs["Position"], None, (-400, -100))
    velocity = _vmath(tree, "SCALE", outward, group_in.outputs["Speed"], (-200, -100))
    # Point size about the spacing of the shell's vertices.
    spacing = _math(tree, "DIVIDE", _math(tree, "MULTIPLY", group_in.outputs["Radius"], grow, (-400, 0)),
                    _math(tree, "POWER", 2.0, group_in.outputs["Detail"], (-400, 0)), (-300, 0))
    geometry = _store(tree, to_points.outputs["Points"], "mask", active, location=(100, 200))
    geometry = _store(tree, geometry, "radius", _math(tree, "MULTIPLY", spacing, 1.5, (0, 0)), location=(300, 200))
    geometry = _store(tree, geometry, "velocity", velocity, "FLOAT_VECTOR", (500, 200))
    _link(tree, geometry, group_out.inputs["Geometry"])
    return tree


def build_trail(name="Fumaris Trail"):
    tree = _new_tree(name)
    tree.interface.new_socket("Curve", in_out="INPUT", socket_type="NodeSocketObject")
    _socket(tree, "Start Frame", "NodeSocketFloat", 1.0)
    _socket(tree, "End Frame", "NodeSocketFloat", 60.0)
    _socket(tree, "Head Length", "NodeSocketFloat", 0.08, 0.0)
    _socket(tree, "Radius", "NodeSocketFloat", 0.1, 0.0)
    _socket(tree, "Points", "NodeSocketInt", 400, 2)
    group_in, group_out, time = _io(tree)
    info = tree.nodes.new("GeometryNodeObjectInfo")
    info.transform_space = "RELATIVE"
    info.location = (-1000, 300)
    _link(tree, group_in.outputs["Curve"], info.inputs["Object"])
    resample = tree.nodes.new("GeometryNodeResampleCurve")
    resample.location = (-800, 300)
    _link(tree, info.outputs["Geometry"], resample.inputs["Curve"])
    _link(tree, group_in.outputs["Points"], resample.inputs["Count"])
    parameter = tree.nodes.new("GeometryNodeSplineParameter")
    parameter.location = (-1000, -100)
    # The curve's own parameter, kept as an attribute: points have no spline.
    along = _store(tree, resample.outputs["Curve"], "trail_factor", parameter.outputs["Factor"], location=(-700, 300))
    to_points = tree.nodes.new("GeometryNodeCurveToPoints")
    to_points.mode = "EVALUATED"
    to_points.location = (-600, 300)
    _link(tree, along, to_points.inputs["Curve"])
    factor = tree.nodes.new("GeometryNodeInputNamedAttribute")
    factor.data_type = "FLOAT"
    factor.inputs["Name"].default_value = "trail_factor"
    factor.location = (-800, -150)
    # The head moves from the curve's start to its end between the frames;
    # points within Head Length (share of the curve) behind it burn.
    head = _math(tree, "DIVIDE", _math(tree, "SUBTRACT", time.outputs["Frame"], group_in.outputs["Start Frame"], (-900, -300)),
                 _math(tree, "MAXIMUM", _math(tree, "SUBTRACT", group_in.outputs["End Frame"], group_in.outputs["Start Frame"], (-900, -350)), 1.0, (-800, -350)), (-700, -300))
    behind = _math(tree, "SUBTRACT", head, factor.outputs["Attribute"], (-600, -200))
    started = _math(tree, "GREATER_THAN", behind, -1e-4, (-500, -200))
    recent = _math(tree, "LESS_THAN", behind, group_in.outputs["Head Length"], (-500, -250))
    running = _math(tree, "LESS_THAN", head, 1.0 + 1e-4, (-500, -300))
    active = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", started, recent, (-400, -200)), running, (-300, -200))
    geometry = _store(tree, to_points.outputs["Points"], "mask", active, location=(300, 200))
    geometry = _store(tree, geometry, "radius", group_in.outputs["Radius"], location=(500, 200))
    _link(tree, geometry, group_out.inputs["Geometry"])
    return tree


def build_spreading(name="Fumaris Spreading Fire"):
    tree = _new_tree(name)
    tree.interface.new_socket("Surface", in_out="INPUT", socket_type="NodeSocketObject")
    tree.interface.new_socket("Ignition", in_out="INPUT", socket_type="NodeSocketObject")
    _socket(tree, "Start Frame", "NodeSocketFloat", 1.0)
    _socket(tree, "Spread Speed", "NodeSocketFloat", 0.5, 0.0)
    _socket(tree, "Front Width", "NodeSocketFloat", 0.6, 0.0)
    _socket(tree, "Density", "NodeSocketFloat", 60.0, 0.0)
    _socket(tree, "Flame Rise", "NodeSocketFloat", 0.5)
    group_in, group_out, time = _io(tree)
    surface = tree.nodes.new("GeometryNodeObjectInfo")
    surface.transform_space = "RELATIVE"
    surface.location = (-1000, 300)
    _link(tree, group_in.outputs["Surface"], surface.inputs["Object"])
    distribute = tree.nodes.new("GeometryNodeDistributePointsOnFaces")
    distribute.location = (-800, 300)
    _link(tree, surface.outputs["Geometry"], distribute.inputs["Mesh"])
    _link(tree, group_in.outputs["Density"], distribute.inputs["Density"])
    ignition = tree.nodes.new("GeometryNodeObjectInfo")
    ignition.transform_space = "RELATIVE"
    ignition.location = (-1000, -100)
    _link(tree, group_in.outputs["Ignition"], ignition.inputs["Object"])
    position = tree.nodes.new("GeometryNodeInputPosition")
    position.location = (-800, -100)
    distance = _vmath(tree, "DISTANCE", position.outputs["Position"], ignition.outputs["Location"], (-600, -100), output=1)
    # Burnt radius grows at Spread Speed (m/s) from Start Frame; points within
    # Front Width behind the front burn (0 = everything inside keeps burning).
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base if bpy.context.scene else 24.0
    seconds = _math(tree, "DIVIDE", _math(tree, "SUBTRACT", time.outputs["Frame"], group_in.outputs["Start Frame"], (-900, -400)), fps, (-800, -400))
    front = _math(tree, "MULTIPLY", _math(tree, "MAXIMUM", seconds, 0.0, (-700, -400)), group_in.outputs["Spread Speed"], (-600, -400))
    inside = _math(tree, "LESS_THAN", distance, front, (-400, -200))
    depth = _math(tree, "SUBTRACT", front, distance, (-400, -300))
    no_width = _math(tree, "LESS_THAN", group_in.outputs["Front Width"], 1e-4, (-400, -400))
    in_front = _math(tree, "MAXIMUM", _math(tree, "LESS_THAN", depth, group_in.outputs["Front Width"], (-300, -300)), no_width, (-200, -300))
    started = _math(tree, "GREATER_THAN", seconds, 0.0, (-300, -450))
    active = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", inside, in_front, (-100, -250)), started, (0, -250))
    spacing = _math(tree, "DIVIDE", 1.0, _math(tree, "SQRT", _math(tree, "MAXIMUM", group_in.outputs["Density"], 1e-3, (-300, 0)), None, (-200, 0)), (-100, 0))
    rise = tree.nodes.new("ShaderNodeCombineXYZ")
    rise.location = (0, -500)
    _link(tree, group_in.outputs["Flame Rise"], rise.inputs["Z"])
    geometry = _store(tree, distribute.outputs["Points"], "mask", active, location=(300, 200))
    geometry = _store(tree, geometry, "radius", _math(tree, "MULTIPLY", spacing, 0.75, (100, 0)), location=(500, 200))
    geometry = _store(tree, geometry, "velocity", rise.outputs["Vector"], "FLOAT_VECTOR", (700, 200))
    _link(tree, geometry, group_out.inputs["Geometry"])
    return tree


def add_source(context, kind, domain):
    """Create an emitter object running the chosen source group, linked to the domain."""
    label = dict((key, name) for key, name, _ in SOURCES)[kind]
    scene = context.scene
    location = scene.cursor.location.copy()
    extras = []
    if kind == "TRAIL":
        curve_data = bpy.data.curves.new(f"{label} Path", "CURVE")
        curve_data.dimensions = "3D"
        spline = curve_data.splines.new("BEZIER")
        spline.bezier_points.add(1)
        spline.bezier_points[0].co = (-2.0, 0.0, 0.5)
        spline.bezier_points[1].co = (2.0, 0.0, 1.5)
        for point in spline.bezier_points:
            point.handle_left_type = point.handle_right_type = "AUTO"
        curve = bpy.data.objects.new(f"{label} Path", curve_data)
        curve.location = location
        extras.append(curve)
    if kind == "SPREADING":
        mesh = bpy.data.meshes.new(f"{label} Surface")
        size = 2.0
        mesh.from_pydata([(-size, -size, 0), (size, -size, 0), (size, size, 0), (-size, size, 0)], [], [(0, 1, 2, 3)])
        surface = bpy.data.objects.new(f"{label} Surface", mesh)
        surface.location = location
        ignition = bpy.data.objects.new(f"{label} Ignition", None)
        ignition.empty_display_type = "SPHERE"
        ignition.empty_display_size = 0.2
        ignition.location = location + type(location)((-size * 0.8, 0.0, 0.0))
        extras.extend((surface, ignition))

    builder = {"CANDLE": build_candle, "BURST": build_burst, "TRAIL": build_trail, "SPREADING": build_spreading}[kind]
    holder = bpy.data.meshes.new(f"Fumaris {label}")
    obj = bpy.data.objects.new(f"Fumaris {label}", holder)
    # One group per source object: its object inputs are the group's defaults.
    tree = builder(obj.name)
    obj.location = location
    collection = domain.fumaris.emitter_collection if domain is not None else None
    target = collection if collection is not None else scene.collection
    target.objects.link(obj)
    for extra in extras:
        scene.collection.objects.link(extra)
        extra.hide_render = True
    if kind == "TRAIL":
        tree.interface.items_tree["Curve"].default_value = extras[0]
    if kind == "SPREADING":
        tree.interface.items_tree["Surface"].default_value = extras[0]
        tree.interface.items_tree["Ignition"].default_value = extras[1]
    modifier = obj.modifiers.new(label, "NODES")
    modifier.node_group = tree

    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_type = "geometry_nodes"
    props.gn_subtype = "point_cloud"
    props.point_radius = 1.0
    if kind == "CANDLE":
        props.emitter_burning = True
        props.emitter_burn_rate = 2.0
        props.emitter_smoke = 0.5
        props.emitter_fuel = 0.0
    elif kind == "BURST":
        props.emitter_fuel = 2.0
        props.emitter_temperature = 1.0
        props.emitter_smoke = 2.0
    else:
        # Trail and Spreading Fire light their own fuel: at Flame Temperature.
        props.emitter_fuel = 1.0
        props.emitter_temperature = 1.0
        props.emitter_smoke = 1.0
    obj.hide_render = True
    return obj
