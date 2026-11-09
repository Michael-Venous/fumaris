"""Detail & Shaping stack: an ordered list of layers that add detail to the flow
(Disturb, Grain, Shred) or scale a rate per cell (Vorticity, Dissipation,
Cooling, Soot Burn-off), each with up to four field masks combined by blend.

Masks are map ranges: the field from From Min to From Max gives a factor from
To Min to To Max (clamped). A layer acts only where its masks allow; new layers
start with the masks that make them useful (Disturb and Grain: smoke or heat).

Replaces Turbulence Detail, Control Ramps and Soot Burn-off (2.0). The old
domain properties are kept, hidden, only as inputs: adopt_legacy() turns their
values into layers (saved scenes, older presets and takes, Quick Setup) and
resets them.
"""
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, StringProperty)
from bpy.types import Operator, PropertyGroup, UIList

STACK_VERSION = 2
MASKS_PER_LAYER = 6
# Disturb and Grain act where there is smoke, or heat (full at this many kelvin).
GATE_KELVIN = 2500.0
VELOCITY_LAYER_LIMIT = 8
SHAPE_LAYER_LIMIT = 4

# identifier, label, description, icon, protocol name, default strength
LAYER_TYPES = (
    ("DISTURB", "Disturb",
     "Swirling pushes inside smoke and hot gas, of a set size: breakup and billows the base "
     "motion lacks. Add one large and one fine layer for detail at two scales",
     "FORCE_TURBULENCE", "disturb", 100.0),
    ("GRAIN", "Grain",
     "Small random kicks, one direction per block of Grain Size, renewed every frame: the "
     "gritty breakup of fast fronts",
     "MOD_NOISE", "grain", 100.0),
    ("SHRED", "Shred",
     "Stretches the flow across steep temperature changes, tearing flame edges into strands",
     "MOD_PHYSICS", "shred", 0.5),
    ("VORTICITY", "Vorticity",
     "Scales Vorticity (Smoke Behavior) per cell by the masks, e.g. stronger swirls in hot gas",
     "FORCE_VORTEX", "vorticity", 0.0),
    ("DISSIPATION", "Dissipation",
     "Scales Dissipation per cell by the masks, e.g. cold smoke fades faster than hot",
     "MOD_FLUIDSIM", "dissipation", 0.0),
    ("COOLING", "Cooling",
     "Scales cooling per cell by the masks, e.g. thin smoke loses its heat faster",
     "FREEZE", "cooling", 0.0),
    ("SOOT_BURN", "Soot Burn-off",
     "Smoke thins (burns off) at this rate per second where the masks allow: by default where "
     "gas is hot and air is left, as soot oxidises. Forms nothing, adds no heat",
     "FORCE_FORCE", "soot_burn", 1.0),
)
_LAYER = {item[0]: item for item in LAYER_TYPES}
VELOCITY_TYPES = {"DISTURB", "GRAIN", "SHRED", "VORTICITY"}
SCALE_TYPES = {"VORTICITY", "DISSIPATION", "COOLING"}

MASK_FIELDS = (
    ("TEMPERATURE", "Temperature", "Gas temperature in kelvin", "temperature"),
    ("SPEED", "Speed", "Gas speed, scene units per second", "speed"),
    ("SMOKE", "Smoke", "Smoke amount", "smoke"),
    ("FUEL", "Fuel", "Unburnt fuel", "fuel"),
    ("BURN", "Burn", "This step's burning (where fire is reacting)", "burn"),
    ("AIR", "Air Left",
     "Air left to burn with, 0-1: the Oxygen Field when it is on, otherwise estimated from "
     "temperature (1 at the air's temperature, 0 at Flame Temperature)", "air"),
    ("NOISE", "Noise", "A smooth noise of Noise Size, 0-1, fixed in the domain", "noise"),
)
_FIELD = {item[0]: item for item in MASK_FIELDS}

# A new mask, or one whose field changes, maps this range of the field to 0..1.
FIELD_DEFAULT_RANGES = {
    "TEMPERATURE": (500.0, 1500.0),  # K: warm gas to fire
    "SPEED": (1.0, 10.0),            # m/s: drifting smoke to a fast front
    "SMOKE": (0.0, 1.0),
    "FUEL": (0.0, 2.0),
    "BURN": (0.0, 0.5),
    "AIR": (0.0, 1.0),
    "NOISE": (0.0, 1.0),
}
FIELD_UNITS = {"TEMPERATURE": " K", "SPEED": " m/s"}


def _field_updated(self, _context):
    # A sensible range for the new field; To keeps its values.
    self.range = FIELD_DEFAULT_RANGES.get(self.field, (0.0, 1.0))

BLENDS = (
    ("MULTIPLY", "Multiply", "Multiply by this mask (the first mask multiplies 1)", "multiply"),
    ("MINIMUM", "Minimum", "Keep the smaller of this mask and the masks above", "minimum"),
    ("MAXIMUM", "Maximum", "Keep the larger of this mask and the masks above", "maximum"),
    ("ADD", "Add", "Add this mask", "add"),
    ("SUBTRACT", "Subtract", "Subtract this mask", "subtract"),
    ("REPLACE", "Replace", "Use this mask instead of the masks above", "replace"),
)
_BLEND = {item[0]: item for item in BLENDS}


def _type_items():
    return [(key, label, description, icon, index)
            for index, (key, label, description, icon, _name, _strength) in enumerate(LAYER_TYPES)]


def _field_items():
    return [(key, label, description, index) for index, (key, label, description, _name) in enumerate(MASK_FIELDS)]


def _blend_items():
    return [(key, label, description, index) for index, (key, label, description, _name) in enumerate(BLENDS)]


class FumarisFieldMask(PropertyGroup):
    enabled: BoolProperty(name="Enabled", default=True, description="Use this mask")
    field: EnumProperty(name="Field", items=_field_items(), default="TEMPERATURE", update=_field_updated,
                        description="Field this mask reads in each cell")
    blend: EnumProperty(name="Blend", items=_blend_items(), default="MULTIPLY",
                        description="How this mask combines with the masks above it (the first mask starts from 1)")
    invert: BoolProperty(name="Invert", default=False, options={"HIDDEN"},
                         description="Flip the ramp (kept for older files; swap To Min and To Max instead)")
    range: FloatVectorProperty(name="From", size=2, default=(500.0, 1500.0),
                               description="Field values mapped from: From Min to From Max")
    values: FloatVectorProperty(name="To", size=2, default=(0.0, 1.0), min=0.0, soft_max=4.0,
                                description="Factor at From Min and at From Max (clamped between them, never below 0)")
    noise_size: FloatProperty(name="Noise Size", default=1.0, min=0.001, soft_max=10.0, subtype="DISTANCE",
                              description="Feature size of the noise field")


class FumarisDetailLayer(PropertyGroup):
    enabled: BoolProperty(name="Enabled", default=True, description="Use this layer")
    layer_type: EnumProperty(name="Type", items=_type_items(), default="DISTURB")
    strength: FloatProperty(name="Strength", default=100.0, min=0.0, soft_max=1000.0,
                            description="Disturb and Grain: push in scene units per second squared (about 50 "
                                        "is subtle, 300 strong on a fireball a few metres across). Shred and "
                                        "Soot Burn-off: rate per second")
    size: FloatProperty(name="Size", default=0.5, min=0.001, soft_max=10.0, subtype="DISTANCE",
                        description="Disturb: swirl size. Grain: block edge length. In scene units; use a "
                                    "few voxels or more")
    detail: IntProperty(name="Detail", default=1, min=1, max=6,
                        description="Disturb: adds smaller swirls, each half the size. Grain: layers, each "
                                    "three times the block size (up to 3). 1 = one size")
    # Stack version 1 only: now Speed and Smoke masks.
    min_speed: FloatProperty(name="Min Speed", default=0.0, min=0.0, options={"HIDDEN"})
    edge: FloatProperty(name="Edges Only", default=0.0, min=0.0, options={"HIDDEN"})
    masks: CollectionProperty(type=FumarisFieldMask)
    masks_index: IntProperty(name="Active Mask", default=0)


def label(layer):
    return _LAYER[layer.layer_type][1]


def is_velocity(layer_type):
    return layer_type in VELOCITY_TYPES


def counts(props):
    velocity = sum(1 for layer in props.detail_layers if is_velocity(layer.layer_type))
    return velocity, len(props.detail_layers) - velocity


def can_add(props, layer_type):
    velocity, shape = counts(props)
    return velocity < VELOCITY_LAYER_LIMIT if is_velocity(layer_type) else shape < SHAPE_LAYER_LIMIT


def gate_masks(min_speed=0.0):
    """Disturb and Grain's usual masks: smoke, or heat where there is more of
    it (full at GATE_KELVIN); and with min_speed, only faster gas."""
    masks = [{"field": "SMOKE", "blend": "MULTIPLY", "range": [0.0, 1.0], "values": [0.0, 1.0]},
             {"field": "TEMPERATURE", "blend": "MAXIMUM", "range": [0.0, GATE_KELVIN], "values": [0.0, 1.0]}]
    if min_speed > 0.0:
        masks.append({"field": "SPEED", "blend": "MULTIPLY", "range": [0.5 * min_speed, min_speed],
                      "values": [0.0, 1.0]})
    return masks


def default_masks(layer_type, flame_kelvin):
    """The masks a new layer starts with, editable like any other."""
    if layer_type in {"DISTURB", "GRAIN"}:
        return gate_masks()
    if layer_type == "SOOT_BURN":
        # Soot oxidises where gas is hot (0.65 to 0.9 of Flame Temperature)
        # and still holds air (the Oxygen Field, or estimated from temperature).
        return [{"field": "TEMPERATURE", "range": [0.65 * flame_kelvin, 0.9 * flame_kelvin], "values": [0.0, 1.0]},
                {"field": "AIR", "blend": "MULTIPLY", "range": [0.0, 1.0], "values": [0.0, 1.0]}]
    if layer_type == "VORTICITY":   # stronger swirls in hot gas
        return [{"field": "TEMPERATURE", "range": [0.0, flame_kelvin], "values": [0.5, 2.0]}]
    if layer_type == "DISSIPATION":  # cold smoke fades faster than hot
        return [{"field": "TEMPERATURE", "range": [0.0, 0.5 * flame_kelvin], "values": [2.0, 0.5]}]
    if layer_type == "COOLING":     # thin gas loses its heat faster
        return [{"field": "SMOKE", "range": [0.0, 1.0], "values": [2.0, 1.0]}]
    return []


def add_layer(props, layer_type, masks=None, **values):
    layer = props.detail_layers.add()
    layer.layer_type = layer_type
    layer.strength = _LAYER[layer_type][5]
    if layer_type == "GRAIN":
        layer.size = 0.2
    for key, value in values.items():
        setattr(layer, key, value)
    if masks is None:
        masks = default_masks(layer_type, float(getattr(props, "flame_temperature", 2000.0)))
    for mask in masks:
        add_mask(layer, **mask)
    return layer


def add_mask(layer, **values):
    mask = layer.masks.add()
    values.setdefault("field", "TEMPERATURE")
    for key, value in values.items():
        setattr(mask, key, value)  # field first: its default range, then any given one
    if "range" not in values:
        mask.range = FIELD_DEFAULT_RANGES.get(mask.field, (0.0, 1.0))
    return mask


def export_layers(props):
    """The enabled layers and masks as the bridge reads them ("detail_layers")."""
    layers = []
    for layer in getattr(props, "detail_layers", ()):
        if not layer.enabled:
            continue
        masks = [{
            "field": _FIELD[mask.field][3],
            "blend": _BLEND[mask.blend][3],
            "invert": bool(mask.invert),
            "range": [float(mask.range[0]), float(mask.range[1])],
            "values": [float(mask.values[0]), float(mask.values[1])],
            "noise_size": float(mask.noise_size),
        } for mask in layer.masks if mask.enabled][:MASKS_PER_LAYER]
        layers.append({
            "type": _LAYER[layer.layer_type][4],
            "strength": float(layer.strength),
            "size": float(layer.size),
            "octaves": int(min(layer.detail, 3 if layer.layer_type == "GRAIN" else 6)),
            "masks": masks,
        })
    return layers


_LAYER_KEYS = ("enabled", "layer_type", "strength", "size", "detail", "min_speed", "edge")
_MASK_KEYS = ("enabled", "field", "blend", "invert", "range", "values", "noise_size")


def _plain(value):
    return [float(v) for v in value] if hasattr(value, "__len__") and not isinstance(value, str) else value


def to_list(props):
    """Every layer, enabled or not, for presets and takes."""
    return [{**{key: _plain(getattr(layer, key)) for key in _LAYER_KEYS},
             "masks": [{key: _plain(getattr(mask, key)) for key in _MASK_KEYS} for mask in layer.masks]}
            for layer in props.detail_layers]


def from_list(props, layers):
    props.detail_layers.clear()
    for entry in layers or ():
        if entry.get("layer_type") not in _LAYER:
            continue
        layer = props.detail_layers.add()
        for key in _LAYER_KEYS:
            if key in entry:
                setattr(layer, key, entry[key])
        for mask_entry in entry.get("masks", ())[:MASKS_PER_LAYER]:
            mask = layer.masks.add()
            for key in _MASK_KEYS:
                if key in mask_entry:
                    try:
                        setattr(mask, key, mask_entry[key])
                    except (TypeError, ValueError):
                        pass
    props.detail_layers_index = min(props.detail_layers_index, max(0, len(props.detail_layers) - 1))


# Old domain properties (hidden, inputs only) and their defaults.
LEGACY_DEFAULTS = {
    "disturb": 0.0, "disturb_size": 0.5, "disturb_detail": 1, "disturb_min_speed": 0.0,
    "grain": 0.0, "grain_size": 0.2, "grain_edge": 0.0, "grain_min_speed": 0.0, "grain_layers": 1,
    "shred": 0.0, "soot_burn_off": 0.0, "soot_burn_needs_air": False,
    **{f"{name}_scale_by": "uniform" for name in ("vorticity", "disturb", "grain", "dissipation", "cooling")},
    **{f"{name}_scale_range": (0.0, 1.0) for name in ("vorticity", "disturb", "grain", "dissipation", "cooling")},
    **{f"{name}_scale_values": (1.0, 1.0) for name in ("vorticity", "disturb", "grain", "dissipation", "cooling")},
}
_LEGACY_FIELDS = {"temperature": "TEMPERATURE", "speed": "SPEED", "smoke": "SMOKE"}


def _legacy(values, key):
    return values.get(key, LEGACY_DEFAULTS[key])


def _mask(field, range_, values, blend="MULTIPLY"):
    return {"enabled": True, "field": field, "blend": blend, "invert": False,
            "range": list(range_), "values": list(values), "noise_size": 1.0}


def legacy_layers(values, flame_kelvin=2000.0):
    """The stack equivalent to the old settings in `values` (a dict), in the
    order the solver applied them (bridge readLegacyDetailLayers). Temperature
    ramps were relative to Flame Temperature."""
    def ramp(name):
        field = _LEGACY_FIELDS.get(_legacy(values, f"{name}_scale_by"))
        if field is None:
            return []
        range_ = list(_legacy(values, f"{name}_scale_range"))
        if field == "TEMPERATURE":
            range_ = [v * flame_kelvin for v in range_]
        return [_mask(field, range_, _legacy(values, f"{name}_scale_values"))]

    def gate(min_speed):
        return [_mask(m["field"], m["range"], m["values"], m["blend"]) for m in gate_masks(min_speed)]

    layers = []

    def shaping(layer_type, name):
        masks = ramp(name)
        if masks:
            layers.append({"enabled": True, "layer_type": layer_type, "strength": 0.0, "masks": masks})

    shaping("VORTICITY", "vorticity")
    if _legacy(values, "disturb") > 0.0:
        layers.append({"enabled": True, "layer_type": "DISTURB", "strength": _legacy(values, "disturb"),
                       "size": _legacy(values, "disturb_size"), "detail": int(_legacy(values, "disturb_detail")),
                       "masks": gate(_legacy(values, "disturb_min_speed")) + ramp("disturb")})
    if _legacy(values, "grain") > 0.0:
        masks = gate(_legacy(values, "grain_min_speed"))
        edge = _legacy(values, "grain_edge")
        if edge > 0.0:  # Edges Only: thinner smoke than this
            masks.append(_mask("SMOKE", (0.5 * edge, edge), (1.0, 0.0)))
        layers.append({"enabled": True, "layer_type": "GRAIN", "strength": _legacy(values, "grain"),
                       "size": _legacy(values, "grain_size"), "detail": int(_legacy(values, "grain_layers")),
                       "masks": masks + ramp("grain")})
    if _legacy(values, "shred") > 0.0:
        layers.append({"enabled": True, "layer_type": "SHRED", "strength": _legacy(values, "shred"), "masks": []})
    if _legacy(values, "soot_burn_off") > 0.0:
        masks = [_mask("TEMPERATURE", (0.65 * flame_kelvin, 0.9 * flame_kelvin), (0.0, 1.0))]
        if _legacy(values, "soot_burn_needs_air"):
            masks.append(_mask("AIR", (0.0, 1.0), (0.0, 1.0)))
        layers.append({"enabled": True, "layer_type": "SOOT_BURN", "strength": _legacy(values, "soot_burn_off"),
                       "masks": masks})
    shaping("DISSIPATION", "dissipation")
    shaping("COOLING", "cooling")
    return layers


def upgrade(layers, version, flame_kelvin):
    """Saved stack lists (layer dicts) to this version. Version 1 read
    Temperature relative to Flame Temperature and had Disturb and Grain's
    smoke-or-heat gate, Min Speed and Edges Only built in."""
    if version >= STACK_VERSION:
        return layers
    upgraded = []
    for entry in layers:
        entry = dict(entry)
        masks = [dict(mask) for mask in entry.get("masks", ())]
        for mask in masks:
            if mask.get("field") == "TEMPERATURE":
                mask["range"] = [float(v) * flame_kelvin for v in mask.get("range", (0.0, 1.0))]
        if entry.get("layer_type") in {"DISTURB", "GRAIN"}:
            gate = [_mask(m["field"], m["range"], m["values"], m["blend"])
                    for m in gate_masks(float(entry.get("min_speed", 0.0)))]
            edge = float(entry.get("edge", 0.0))
            if entry["layer_type"] == "GRAIN" and edge > 0.0:
                gate.append(_mask("SMOKE", (0.5 * edge, edge), (1.0, 0.0)))
            masks = gate + masks
            entry["min_speed"], entry["edge"] = 0.0, 0.0
        entry["masks"] = masks[:MASKS_PER_LAYER]
        upgraded.append(entry)
    return upgraded


def _legacy_values(props):
    return {key: _plain(getattr(props, key)) for key in LEGACY_DEFAULTS if hasattr(props, key)}


def _differs(value, default):
    if isinstance(default, tuple):
        return any(_differs(v, d) for v, d in zip(value, default))
    if isinstance(default, float):
        return abs(float(value) - default) > 1e-6 * max(1.0, abs(default))
    return value != default


def has_legacy(props):
    values = _legacy_values(props)
    return any(_differs(values[key], default) for key, default in LEGACY_DEFAULTS.items() if key in values)


def reset_legacy(props):
    for key, value in LEGACY_DEFAULTS.items():
        if hasattr(props, key):
            setattr(props, key, value)


def adopt_legacy(props, replace=False):
    """Turn non-default old settings into layers (appended, or replacing the
    stack) and reset them. Returns the number of layers made."""
    if not has_legacy(props):
        return 0
    layers = legacy_layers(_legacy_values(props), float(props.flame_temperature))
    if replace:
        from_list(props, layers)
    else:
        existing = to_list(props)
        from_list(props, existing + layers)
    reset_legacy(props)
    return len(layers)


def migrate(props):
    """Saved domains: old settings become layers; stacks saved by an earlier
    version of the stack are upgraded."""
    if props.smoke_object_type != "domain" or props.detail_stack_version >= STACK_VERSION:
        return
    if props.detail_stack_version >= 1:
        from_list(props, upgrade(to_list(props), props.detail_stack_version, float(props.flame_temperature)))
    adopt_legacy(props, replace=not len(props.detail_layers))
    props.detail_stack_version = STACK_VERSION


# ------------------------------------------------------------------ operators


def _domain(context, name):
    obj = bpy.data.objects.get(name) if name else getattr(context, "object", None)
    if obj is None or getattr(obj, "fumaris", None) is None or obj.fumaris.smoke_object_type != "domain":
        return None
    return obj


class FUMARIS_OT_detail_layer_add(Operator):
    bl_idname = "fumaris.detail_layer_add"
    bl_label = "Add Layer"
    bl_description = "Add a Detail & Shaping layer"
    bl_options = {"REGISTER", "UNDO"}

    layer_type: EnumProperty(name="Type", items=_type_items())
    domain_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def description(cls, _context, properties):
        return _LAYER[properties.layer_type][2]

    def execute(self, context):
        domain = _domain(context, self.domain_name)
        if domain is None:
            return {"CANCELLED"}
        props = domain.fumaris
        if not can_add(props, self.layer_type):
            self.report({"WARNING"}, f"At most {VELOCITY_LAYER_LIMIT} motion layers (Disturb, Grain, Shred, "
                                     f"Vorticity) and {SHAPE_LAYER_LIMIT} smoke and heat layers")
            return {"CANCELLED"}
        add_layer(props, self.layer_type)
        props.detail_layers_index = len(props.detail_layers) - 1
        return {"FINISHED"}


class FUMARIS_OT_detail_layer_remove(Operator):
    bl_idname = "fumaris.detail_layer_remove"
    bl_label = "Remove Layer"
    bl_description = "Remove the active Detail & Shaping layer"
    bl_options = {"REGISTER", "UNDO"}

    domain_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        domain = _domain(context, self.domain_name)
        if domain is None or not len(domain.fumaris.detail_layers):
            return {"CANCELLED"}
        props = domain.fumaris
        props.detail_layers.remove(props.detail_layers_index)
        props.detail_layers_index = max(0, min(props.detail_layers_index, len(props.detail_layers) - 1))
        return {"FINISHED"}


class FUMARIS_OT_detail_layer_move(Operator):
    bl_idname = "fumaris.detail_layer_move"
    bl_label = "Move Layer"
    bl_description = "Move the active layer up or down. Motion layers apply in this order"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))
    domain_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        domain = _domain(context, self.domain_name)
        if domain is None:
            return {"CANCELLED"}
        props = domain.fumaris
        index = props.detail_layers_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not 0 <= target < len(props.detail_layers):
            return {"CANCELLED"}
        props.detail_layers.move(index, target)
        props.detail_layers_index = target
        return {"FINISHED"}


def _active_layer(context, name):
    domain = _domain(context, name)
    if domain is None or not len(domain.fumaris.detail_layers):
        return None
    props = domain.fumaris
    return props.detail_layers[min(props.detail_layers_index, len(props.detail_layers) - 1)]


class FUMARIS_OT_field_mask_add(Operator):
    bl_idname = "fumaris.field_mask_add"
    bl_label = "Add Mask"
    bl_description = "Add a mask to the active layer (up to 4); masks combine in order by their blend"
    bl_options = {"REGISTER", "UNDO"}

    domain_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        layer = _active_layer(context, self.domain_name)
        if layer is None:
            return {"CANCELLED"}
        if len(layer.masks) >= MASKS_PER_LAYER:
            self.report({"WARNING"}, f"At most {MASKS_PER_LAYER} masks per layer")
            return {"CANCELLED"}
        add_mask(layer)
        layer.masks_index = len(layer.masks) - 1
        return {"FINISHED"}


class FUMARIS_OT_field_mask_remove(Operator):
    bl_idname = "fumaris.field_mask_remove"
    bl_label = "Remove Mask"
    bl_description = "Remove the active mask"
    bl_options = {"REGISTER", "UNDO"}

    domain_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        layer = _active_layer(context, self.domain_name)
        if layer is None or not len(layer.masks):
            return {"CANCELLED"}
        layer.masks.remove(layer.masks_index)
        layer.masks_index = max(0, min(layer.masks_index, len(layer.masks) - 1))
        return {"FINISHED"}


class FUMARIS_OT_field_mask_move(Operator):
    bl_idname = "fumaris.field_mask_move"
    bl_label = "Move Mask"
    bl_description = "Move the active mask up or down (masks combine in order)"
    bl_options = {"REGISTER", "UNDO"}

    direction: EnumProperty(items=(("UP", "Up", ""), ("DOWN", "Down", "")))
    domain_name: StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        layer = _active_layer(context, self.domain_name)
        if layer is None:
            return {"CANCELLED"}
        index = layer.masks_index
        target = index - 1 if self.direction == "UP" else index + 1
        if not 0 <= target < len(layer.masks):
            return {"CANCELLED"}
        layer.masks.move(index, target)
        layer.masks_index = target
        return {"FINISHED"}


# ------------------------------------------------------------------ interface


class FUMARIS_UL_detail_layers(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_property, _index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        sub = row.row(align=True)
        sub.active = item.enabled
        text = label(item)
        if item.layer_type not in SCALE_TYPES:
            text += f"  {item.strength:g}"
        if item.layer_type in {"DISTURB", "GRAIN"}:
            text += f"  ·  {item.size:g} m"
        if len(item.masks):
            text += f"  ·  {len(item.masks)} mask" + ("s" if len(item.masks) > 1 else "")
        sub.label(text=text, icon=_LAYER[item.layer_type][3])


class FUMARIS_UL_field_masks(UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_property, index):
        row = layout.row(align=True)
        row.prop(item, "enabled", text="")
        sub = row.row(align=True)
        sub.active = item.enabled
        units = FIELD_UNITS.get(item.field, "")
        invert = " (inverted)" if item.invert else ""
        sub.label(text=f"{_BLEND[item.blend][1]} {_FIELD[item.field][1]}{invert}  "
                       f"{item.range[0]:g}-{item.range[1]:g}{units} → {item.values[0]:g}-{item.values[1]:g}")


def draw(layout, domain):
    """The stack and the active layer's settings and masks."""
    props = domain.fumaris
    row = layout.row()
    row.template_list("FUMARIS_UL_detail_layers", "", props, "detail_layers", props, "detail_layers_index", rows=3)
    side = row.column(align=True)
    side.operator_menu_enum("fumaris.detail_layer_add", "layer_type", text="", icon="ADD")
    side.operator("fumaris.detail_layer_remove", text="", icon="REMOVE").domain_name = domain.name
    side.separator()
    up = side.operator("fumaris.detail_layer_move", text="", icon="TRIA_UP")
    up.direction, up.domain_name = "UP", domain.name
    down = side.operator("fumaris.detail_layer_move", text="", icon="TRIA_DOWN")
    down.direction, down.domain_name = "DOWN", domain.name
    if not len(props.detail_layers):
        layout.label(text="Add Disturb or Grain for detail, or a layer that scales a rate per cell", icon="INFO")
        return
    layer = props.detail_layers[min(props.detail_layers_index, len(props.detail_layers) - 1)]
    box = layout.column(align=True)
    box.active = layer.enabled
    kind = layer.layer_type
    if kind in {"DISTURB", "GRAIN"}:
        row = box.row(align=True)
        row.prop(layer, "strength")
        row.prop(layer, "size")
        box.prop(layer, "detail", text="Detail" if kind == "DISTURB" else "Layers")
    elif kind in {"SHRED", "SOOT_BURN"}:
        box.prop(layer, "strength", text="Rate" if kind == "SOOT_BURN" else "Strength")
    else:
        box.label(text=f"Scales {label(layer)} by the masks below", icon="INFO")
    layout.label(text="Masks")
    row = layout.row()
    row.template_list("FUMARIS_UL_field_masks", "", layer, "masks", layer, "masks_index", rows=2)
    side = row.column(align=True)
    side.operator("fumaris.field_mask_add", text="", icon="ADD").domain_name = domain.name
    side.operator("fumaris.field_mask_remove", text="", icon="REMOVE").domain_name = domain.name
    side.separator()
    up = side.operator("fumaris.field_mask_move", text="", icon="TRIA_UP")
    up.direction, up.domain_name = "UP", domain.name
    down = side.operator("fumaris.field_mask_move", text="", icon="TRIA_DOWN")
    down.direction, down.domain_name = "DOWN", domain.name
    if not len(layer.masks):
        layout.label(text="No masks: this layer acts everywhere" if kind not in SCALE_TYPES
                     else "No masks: this layer changes nothing", icon="INFO")
        return
    mask = layer.masks[min(layer.masks_index, len(layer.masks) - 1)]
    # A map range: the field from From Min to From Max gives To Min to To Max.
    column = layout.column(align=True)
    column.active = mask.enabled
    row = column.row(align=True)
    row.prop(mask, "field", text="")
    row.prop(mask, "blend", text="")
    if mask.field == "NOISE":
        column.prop(mask, "noise_size")
    units = FIELD_UNITS.get(mask.field, "")
    row = column.row(align=True)
    row.prop(mask, "range", index=0, text=f"From Min{units}")
    row.prop(mask, "range", index=1, text=f"From Max{units}")
    row = column.row(align=True)
    row.prop(mask, "values", index=0, text="To Min")
    row.prop(mask, "values", index=1, text="To Max")


PROPERTY_CLASSES = (FumarisFieldMask, FumarisDetailLayer)
CLASSES = (
    FUMARIS_OT_detail_layer_add, FUMARIS_OT_detail_layer_remove, FUMARIS_OT_detail_layer_move,
    FUMARIS_OT_field_mask_add, FUMARIS_OT_field_mask_remove, FUMARIS_OT_field_mask_move,
    FUMARIS_UL_detail_layers, FUMARIS_UL_field_masks,
)


def register_properties():
    for cls in PROPERTY_CLASSES:
        bpy.utils.register_class(cls)


def unregister_properties():
    for cls in reversed(PROPERTY_CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
