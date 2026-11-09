"""Detail & Shaping stack: an ordered list of layers that add detail to the flow
(Disturb, Grain, Shred) or scale a rate per cell (Vorticity, Dissipation,
Cooling, Soot Burn-off), each with up to four field masks combined by blend.

Replaces Turbulence Detail, Control Ramps and Soot Burn-off (2.0). The old
domain properties are kept, hidden, only as inputs: adopt_legacy() turns their
values into layers (saved scenes, older presets and takes, Quick Setup) and
resets them.
"""
import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, StringProperty)
from bpy.types import Operator, PropertyGroup, UIList

STACK_VERSION = 1
MASKS_PER_LAYER = 4
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
     "gas is hot, optionally only where air is left. Forms nothing, adds no heat",
     "FORCE_FORCE", "soot_burn", 1.0),
)
_LAYER = {item[0]: item for item in LAYER_TYPES}
VELOCITY_TYPES = {"DISTURB", "GRAIN", "SHRED", "VORTICITY"}
SCALE_TYPES = {"VORTICITY", "DISSIPATION", "COOLING"}

MASK_FIELDS = (
    ("TEMPERATURE", "Temperature", "Temperature relative to Flame Temperature (1 = Flame Temperature)", "temperature"),
    ("SPEED", "Speed", "Gas speed, scene units per second", "speed"),
    ("SMOKE", "Smoke", "Smoke amount", "smoke"),
    ("FUEL", "Fuel", "Unburnt fuel", "fuel"),
    ("BURN", "Burn", "This step's burning (where fire is reacting)", "burn"),
    ("AIR", "Air Left",
     "Air left to burn with, 0-1: the Oxygen Field when it is on, otherwise estimated from "
     "temperature (1 - temperature relative to Flame Temperature)", "air"),
    ("NOISE", "Noise", "A smooth noise of Noise Size, 0-1, fixed in the domain", "noise"),
)
_FIELD = {item[0]: item for item in MASK_FIELDS}

BLENDS = (
    ("MULTIPLY", "Multiply", "Multiply by this mask", "multiply"),
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
    field: EnumProperty(name="Field", items=_field_items(), default="TEMPERATURE",
                        description="Field this mask reads in each cell")
    blend: EnumProperty(name="Blend", items=_blend_items(), default="MULTIPLY",
                        description="How this mask combines with the masks above it (the first mask starts from 1)")
    invert: BoolProperty(name="Invert", default=False,
                         description="Flip the ramp: the high end's value at the low end of the range")
    range: FloatVectorProperty(name="Range", size=2, default=(0.0, 1.0),
                               description="Field values where the ramp starts and ends")
    values: FloatVectorProperty(name="Values", size=2, default=(0.0, 1.0), min=0.0, soft_max=4.0,
                                description="Factor at the low and the high end of the range (never below 0)")
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
    min_speed: FloatProperty(name="Min Speed", default=0.0, min=0.0, soft_max=20.0,
                             description="Only gas moving faster than this (scene units per second); 0 = any speed")
    edge: FloatProperty(name="Edges Only", default=0.0, min=0.0, soft_max=4.0,
                        description="Grain: only gas thinner than this smoke amount, so breaking-up edges "
                                    "get grit and dense cores stay smooth; 0 = everywhere")
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


def add_layer(props, layer_type, **values):
    layer = props.detail_layers.add()
    layer.layer_type = layer_type
    layer.strength = _LAYER[layer_type][5]
    if layer_type == "GRAIN":
        layer.size = 0.2
    for key, value in values.items():
        setattr(layer, key, value)
    if layer_type == "SOOT_BURN" and "masks" not in values:
        add_mask(layer, field="TEMPERATURE", range=(0.65, 0.9), values=(0.0, 1.0))
    return layer


def add_mask(layer, **values):
    mask = layer.masks.add()
    for key, value in values.items():
        setattr(mask, key, value)
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
            "min_speed": float(layer.min_speed),
            "edge": float(layer.edge),
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


def legacy_layers(values):
    """The stack equivalent to the old settings in `values` (a dict), in the
    order the solver applied them (bridge readLegacyDetailLayers)."""
    def ramp(name):
        field = _LEGACY_FIELDS.get(_legacy(values, f"{name}_scale_by"))
        if field is None:
            return []
        return [{"enabled": True, "field": field, "blend": "MULTIPLY", "invert": False,
                 "range": list(_legacy(values, f"{name}_scale_range")),
                 "values": list(_legacy(values, f"{name}_scale_values")), "noise_size": 1.0}]

    layers = []

    def shaping(layer_type, name):
        masks = ramp(name)
        if masks:
            layers.append({"enabled": True, "layer_type": layer_type, "strength": 0.0, "masks": masks})

    shaping("VORTICITY", "vorticity")
    if _legacy(values, "disturb") > 0.0:
        layers.append({"enabled": True, "layer_type": "DISTURB", "strength": _legacy(values, "disturb"),
                       "size": _legacy(values, "disturb_size"), "detail": int(_legacy(values, "disturb_detail")),
                       "min_speed": _legacy(values, "disturb_min_speed"), "masks": ramp("disturb")})
    if _legacy(values, "grain") > 0.0:
        layers.append({"enabled": True, "layer_type": "GRAIN", "strength": _legacy(values, "grain"),
                       "size": _legacy(values, "grain_size"), "detail": int(_legacy(values, "grain_layers")),
                       "min_speed": _legacy(values, "grain_min_speed"), "edge": _legacy(values, "grain_edge"),
                       "masks": ramp("grain")})
    if _legacy(values, "shred") > 0.0:
        layers.append({"enabled": True, "layer_type": "SHRED", "strength": _legacy(values, "shred"), "masks": []})
    if _legacy(values, "soot_burn_off") > 0.0:
        masks = [{"enabled": True, "field": "TEMPERATURE", "blend": "MULTIPLY", "invert": False,
                  "range": [0.65, 0.9], "values": [0.0, 1.0], "noise_size": 1.0}]
        if _legacy(values, "soot_burn_needs_air"):
            masks.append({"enabled": True, "field": "AIR", "blend": "MULTIPLY", "invert": False,
                          "range": [0.0, 1.0], "values": [0.0, 1.0], "noise_size": 1.0})
        layers.append({"enabled": True, "layer_type": "SOOT_BURN", "strength": _legacy(values, "soot_burn_off"),
                       "masks": masks})
    shaping("DISSIPATION", "dissipation")
    shaping("COOLING", "cooling")
    return layers


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
    layers = legacy_layers(_legacy_values(props))
    if replace:
        from_list(props, layers)
    else:
        existing = to_list(props)
        from_list(props, existing + layers)
    reset_legacy(props)
    return len(layers)


def migrate(props):
    """Saved domains from before the stack: their old settings become layers."""
    if props.smoke_object_type != "domain" or props.detail_stack_version >= STACK_VERSION:
        return
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
        blend = "" if index == 0 else _BLEND[item.blend][1] + " "
        invert = " (inverted)" if item.invert else ""
        sub.label(text=f"{blend}{_FIELD[item.field][1]}{invert}  {item.range[0]:g}-{item.range[1]:g}"
                       f" → {item.values[0]:g}-{item.values[1]:g}")


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
        row = box.row(align=True)
        row.prop(layer, "detail", text="Detail" if kind == "DISTURB" else "Layers")
        row.prop(layer, "min_speed")
        if kind == "GRAIN":
            box.prop(layer, "edge")
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
        return
    mask = layer.masks[min(layer.masks_index, len(layer.masks) - 1)]
    column = layout.column(align=True)
    column.active = mask.enabled
    row = column.row(align=True)
    row.prop(mask, "field", text="")
    if layer.masks_index > 0:
        row.prop(mask, "blend", text="")
    row.prop(mask, "invert", text="", icon="ARROW_LEFTRIGHT")
    if mask.field == "NOISE":
        column.prop(mask, "noise_size")
    row = column.row(align=True)
    row.prop(mask, "range", text="")
    row = column.row(align=True)
    row.prop(mask, "values", text="")


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
