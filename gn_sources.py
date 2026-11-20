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
    ("CANDLE", "Candle", "A small flickering flame that burns where it sits. Added at least three cells tall: a real candle flame (about 4 cm) needs Resolution 2400 or more"),
    ("BURST", "Burst", "Clumps of fuel and heat thrown out of a ball over a few frames: uneven lobes that roll up into smoke, for blasts and puffs"),
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


# Burst: the Explosion's clump generator at the size of a blast or puff. It was
# one icosphere shell of points pushed straight out (2026-11-10): it could only
# make a round ball ("just a ball of points", user, 2026-11-19). Clumps of fuel
# thrown out at their own speeds, sizes, delays and fuel break it into uneven
# lobes that roll up into smoke.
BURST_DEFAULTS = (
    ("Duration", 8.0), ("Clumps", 24), ("Radius", 0.6), ("Clump Size", 0.5), ("Speed", 6.0), ("Drag", 3.0),
    ("Fuel", 8.0), ("Expansion", 16.0), ("Lift", 0.3), ("Upward Spread", 1.0), ("Downward Spread", 0.35),
    ("Fuel Low", 0.3), ("Fuel High", 3.0), ("Long Burn", 0.15), ("Long Burn Frames", 24.0),
)


def build_burst(name="Fumaris Burst"):
    """A blast or puff: Clumps balls of fuel and heat fly out of a ball of
    Radius at random speeds (slowed by Drag), each burning for its own time with
    its own size and fuel, so the burst grows uneven lobes (build_clumps)."""
    tree = build_clumps(name)
    for key, value in BURST_DEFAULTS:
        tree.interface.items_tree[key].default_value = value
    return tree


def _random(tree, data_type, low, high, seed, offset, location=(0, 0)):
    """Random Value per point (by index), Seed + offset: a separate stream per use."""
    node = tree.nodes.new("FunctionNodeRandomValue")
    node.data_type = data_type
    node.location = location
    # Blender 5 shows only the data type's sockets: Min, Max, ID, Seed.
    node.inputs["Min"].default_value = low
    node.inputs["Max"].default_value = high
    out = node.outputs[0]
    index = tree.nodes.new("GeometryNodeInputIndex")
    index.location = (location[0] - 200, location[1])
    _link(tree, index.outputs["Index"], node.inputs["ID"])
    _link(tree, _math(tree, "ADD", seed, float(offset), (location[0] - 200, location[1] - 60)), node.inputs["Seed"])
    return out


def build_clumps(name="Fumaris Explosion Clumps"):
    """Uneven fireball lobes: Clumps balls of fuel start inside the source and
    fly outward at random speeds (slowed by drag), each burning for its own
    random time with its own size, fuel and expansion, the way the grains of
    a Houdini/Axiom "grains emitter" seed separate cauliflower billows. Times
    in frames from Start Frame; sizes in metres."""
    tree = _new_tree(name)
    _socket(tree, "Start Frame", "NodeSocketFloat", 1.0)
    _socket(tree, "Duration", "NodeSocketFloat", 8.0, 1.0)
    _socket(tree, "Clumps", "NodeSocketInt", 24, 1)
    _socket(tree, "Radius", "NodeSocketFloat", 1.0, 0.0)
    _socket(tree, "Clump Size", "NodeSocketFloat", 0.3, 0.0)
    _socket(tree, "Speed", "NodeSocketFloat", 6.0, 0.0)
    _socket(tree, "Drag", "NodeSocketFloat", 4.0, 0.0)
    _socket(tree, "Fuel", "NodeSocketFloat", 16.0, 0.0)
    _socket(tree, "Expansion", "NodeSocketFloat", 14.0, 0.0)
    _socket(tree, "Seed", "NodeSocketInt", 0)
    # Upward share of each clump's heading: low keeps the burst wide and low
    # (a ground explosion ballooning out), high throws it up into a column.
    _socket(tree, "Lift", "NodeSocketFloat", 0.4, 0.0)
    # Fuel varies from clump to clump between these multiples of Fuel (spread
    # evenly in ratio, so most clumps are lean and a few rich): lean lobes turn
    # to soot first while rich ones keep burning (real fuel fires, 2026-11-18).
    _socket(tree, "Fuel Low", "NodeSocketFloat", 0.4, 0.0)
    _socket(tree, "Fuel High", "NodeSocketFloat", 1.6, 0.0)
    # This share of the clumps keeps feeding fuel, without Expansion, for up to
    # Long Burn Frames after its burst: fire lingers in pockets and at the base
    # while the rest of the cloud goes black.
    _socket(tree, "Long Burn", "NodeSocketFloat", 0.0, 0.0)
    _socket(tree, "Long Burn Frames", "NodeSocketFloat", 48.0, 0.0)
    # How high the clumps' directions reach: 1 = the whole upper hemisphere,
    # lower = a flat lens that sets the width at once and rises later.
    _socket(tree, "Upward Spread", "NodeSocketFloat", 1.0, 0.0)
    # How far below the source's level directions reach (0: upper hemisphere
    # only, for a source on the ground; Burst goes some way down too).
    _socket(tree, "Downward Spread", "NodeSocketFloat", 0.0, 0.0)
    group_in, group_out, time = _io(tree)
    i = group_in.outputs
    seed = i["Seed"]
    points = tree.nodes.new("GeometryNodePoints")
    points.location = (-800, 300)
    _link(tree, i["Clumps"], points.inputs["Count"])
    # Start inside the source: a random direction, at a random depth. Upper
    # hemisphere only: clumps thrown downward ended up under the ground
    # collider, emitting fire below the floor (2026-11-17).
    # z in [-Downward Spread, Upward Spread] (the same random stream as before
    # Downward Spread existed: at 0 the directions are unchanged).
    flatten = tree.nodes.new("ShaderNodeCombineXYZ")
    flatten.location = (-1100, 700)
    flatten.inputs["X"].default_value = flatten.inputs["Y"].default_value = 1.0
    _link(tree, _math(tree, "ADD", i["Upward Spread"], i["Downward Spread"], (-1200, 700)), flatten.inputs["Z"])
    lower = tree.nodes.new("ShaderNodeCombineXYZ")
    lower.location = (-1100, 800)
    _link(tree, i["Downward Spread"], lower.inputs["Z"])
    raw = _vmath(tree, "SUBTRACT",
                 _vmath(tree, "MULTIPLY", _random(tree, "FLOAT_VECTOR", (-1, -1, 0), (1, 1, 1), seed, 1, (-1000, 600)),
                        flatten.outputs["Vector"], (-950, 600)),
                 lower.outputs["Vector"], (-900, 600))
    direction = _vmath(tree, "NORMALIZE", raw, None, (-800, 600))
    depth = _math(tree, "MULTIPLY", _random(tree, "FLOAT", 0.2, 0.7, seed, 2, (-1000, 500)), i["Radius"], (-800, 500))
    start = _vmath(tree, "SCALE", direction, depth, (-600, 600))
    # Outward and a little upward, each at its own speed.
    up = tree.nodes.new("ShaderNodeCombineXYZ")
    up.location = (-800, 450)
    _link(tree, i["Lift"], up.inputs["Z"])
    heading = _vmath(tree, "NORMALIZE", _vmath(tree, "ADD", direction, up.outputs["Vector"], (-700, 450)), None, (-600, 450))
    speed = _math(tree, "MULTIPLY", i["Speed"], _random(tree, "FLOAT", 0.4, 1.6, seed, 3, (-1000, 350)), (-600, 350))
    v0 = _vmath(tree, "SCALE", heading, speed, (-400, 450))
    # Each clump's own window: a random delay and burn time within Duration.
    delay = _math(tree, "MULTIPLY", i["Duration"], _random(tree, "FLOAT", 0.0, 0.5, seed, 4, (-1000, 200)), (-800, 200))
    life = _math(tree, "MULTIPLY", i["Duration"], _random(tree, "FLOAT", 0.4, 1.0, seed, 5, (-1000, 100)), (-800, 100))
    since = _math(tree, "SUBTRACT", _math(tree, "SUBTRACT", time.outputs["Frame"], i["Start Frame"], (-800, 0)), delay, (-700, 0))
    started = _math(tree, "GREATER_THAN", since, -0.5, (-600, 0))
    bursting = _math(tree, "LESS_THAN", since, life, (-600, -50))
    # Long burners: a random Long Burn share, each feeding 0.6-1 x Long Burn Frames longer.
    lingers = _math(tree, "LESS_THAN", _random(tree, "FLOAT", 0.0, 1.0, seed, 9, (-1000, -350)), i["Long Burn"], (-800, -350))
    extra = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", lingers, i["Long Burn Frames"], (-700, -350)),
                  _random(tree, "FLOAT", 0.6, 1.0, seed, 10, (-1000, -450)), (-600, -350))
    feeding = _math(tree, "LESS_THAN", since, _math(tree, "ADD", life, extra, (-700, -400)), (-600, -400))
    active = _math(tree, "MULTIPLY", started, feeding, (-500, 0))
    fps = bpy.context.scene.render.fps / bpy.context.scene.render.fps_base if bpy.context.scene else 24.0
    seconds = _math(tree, "DIVIDE", _math(tree, "MAXIMUM", since, 0.0, (-600, -150)), fps, (-500, -150))
    # Drag: x = x0 + v0 (1 - exp(-k t)) / k, v = v0 exp(-k t).
    k = _math(tree, "MAXIMUM", i["Drag"], 1e-3, (-500, -250))
    decay = _math(tree, "EXPONENT", _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", k, -1.0, (-400, -250)), seconds, (-300, -250)), None, (-200, -250))
    travel = _math(tree, "DIVIDE", _math(tree, "SUBTRACT", 1.0, decay, (-100, -250)), k, (0, -250))
    position = _vmath(tree, "ADD", start, _vmath(tree, "SCALE", v0, travel, (100, 450)), (200, 450))
    velocity = _vmath(tree, "SCALE", v0, decay, (200, 350))
    _link(tree, position, points.inputs["Position"])
    radius = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", i["Radius"], i["Clump Size"], (0, 100)),
                   _random(tree, "FLOAT", 0.6, 1.4, seed, 6, (-200, 100)), (100, 100))
    # Fuel Low x (Fuel High / Fuel Low) ^ random: even in ratio.
    low = _math(tree, "MAXIMUM", i["Fuel Low"], 1e-4, (-400, 0))
    ratio = _math(tree, "DIVIDE", _math(tree, "MAXIMUM", i["Fuel High"], low, (-400, -50)), low, (-300, 0))
    spread = _math(tree, "MULTIPLY", low, _math(tree, "POWER", ratio, _random(tree, "FLOAT", 0.0, 1.0, seed, 7, (-400, 50)), (-200, 0)), (-100, 0))
    fuel = _math(tree, "MULTIPLY", i["Fuel"], spread, (100, 0))
    # Expansion only during the burst itself, not while a long burner lingers.
    expansion = _math(tree, "MULTIPLY", _math(tree, "MULTIPLY", i["Expansion"], bursting, (0, -100)),
                      _random(tree, "FLOAT", 0.5, 1.5, seed, 8, (-200, -100)), (100, -100))
    geometry = _store(tree, points.outputs["Points"], "mask", active, location=(400, 300))
    geometry = _store(tree, geometry, "radius", radius, location=(600, 300))
    geometry = _store(tree, geometry, "velocity", velocity, "FLOAT_VECTOR", (800, 300))
    geometry = _store(tree, geometry, "fuel", fuel, location=(1000, 300))
    geometry = _store(tree, geometry, "expansion", expansion, location=(1200, 300))
    _link(tree, geometry, group_out.inputs["Geometry"])
    return tree


def build_trail(name="Fumaris Trail"):
    tree = _new_tree(name)
    tree.interface.new_socket("Curve", in_out="INPUT", socket_type="NodeSocketObject")
    _socket(tree, "Start Frame", "NodeSocketFloat", 1.0)
    _socket(tree, "End Frame", "NodeSocketFloat", 60.0)
    _socket(tree, "Head Length", "NodeSocketFloat", 0.25, 0.0)
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
    _socket(tree, "Spread Speed", "NodeSocketFloat", 0.75, 0.0)
    _socket(tree, "Front Width", "NodeSocketFloat", 0.6, 0.0)
    _socket(tree, "Density", "NodeSocketFloat", 25.0, 0.0)
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
    # A source thinner than a couple of cells barely reaches the grid (a 4 cm
    # candle at Resolution 512, 6 cm cells, heated one cell), so sizes start at
    # a few of this domain's cells. Set before the modifier copies them.
    cell = 32.0 / max(1, int(getattr(getattr(domain, "fumaris", None), "resolution", 512)))
    inputs = tree.interface.items_tree
    if kind == "CANDLE":
        inputs["Flame Size"].default_value = max(inputs["Flame Size"].default_value, 3.0 * cell)
    elif kind == "BURST":
        # Each clump at least 2.5 cells across.
        inputs["Radius"].default_value = max(inputs["Radius"].default_value,
                                             2.5 * cell / max(inputs["Clump Size"].default_value, 1e-3))
    elif kind == "TRAIL":
        inputs["Radius"].default_value = max(inputs["Radius"].default_value, 2.5 * cell)
    elif kind == "SPREADING":
        # Point radius is 0.75 / sqrt(Density).
        inputs["Density"].default_value = min(inputs["Density"].default_value, (0.75 / (2.5 * cell)) ** 2)
    modifier = obj.modifiers.new(label, "NODES")
    modifier.node_group = tree
    # Blender 5.2.0 leaves a new modifier's Object inputs empty instead of
    # taking the group's defaults (5.2.2 and 5.3 take them): Trail and
    # Spreading Fire emitted nothing there. Set them on the modifier.
    inputs = getattr(getattr(modifier, "properties", None), "inputs", None)
    for item in tree.interface.items_tree:
        if (getattr(item, "in_out", "") == "INPUT" and getattr(item, "socket_type", "") == "NodeSocketObject"
                and item.default_value is not None):
            slot = getattr(inputs, item.identifier, None) if inputs is not None else None
            if slot is not None and getattr(slot, "value", None) is None:
                slot.value = item.default_value

    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_type = "geometry_nodes"
    props.gn_subtype = "point_cloud"
    props.point_radius = 1.0
    if kind == "CANDLE":
        # Rising gas leaves the flame within about a tenth of a second, so it
        # burns fast to reach Flame Temperature: Burn Rate 2 stayed under 650 K;
        # 6 reached ~1800 K but rendered as a dim column, 10 a bright flame
        # base (its smoke follows the domain's Smoke Amount; 2026-11-10).
        props.emitter_burning = True
        props.emitter_burn_rate = 10.0
        props.emitter_smoke = 0.5
        props.emitter_fuel = 0.0
    elif kind == "BURST":
        # Fuel and Expansion come per clump (the group's Fuel and Expansion).
        # A Steady Fire domain at Resolution 384: a ~3 m fireball of uneven
        # lobes in half a second, a few pockets burning on, rolling up into
        # soot (2026-11-19, /tmp/burst sheets).
        props.emitter_fuel = 8.0
        props.emitter_temperature = 1.0
        props.emitter_smoke = 2.0
        props.emitter_divergence = 16.0
    else:
        # Trail and Spreading Fire light their own fuel: at Flame Temperature.
        # Their points pass over a cell briefly, so fuel couples fast.
        props.emitter_fuel = 1.0
        props.emitter_temperature = 1.0
        props.emitter_smoke = 1.0
        props.couple_rate_fuel = 10.0
    obj.hide_render = True
    return obj
