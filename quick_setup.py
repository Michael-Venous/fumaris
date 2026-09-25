"""Small, undoable starting scenes without changing existing simulations."""

import math

import bmesh
import bpy
from bpy.props import EnumProperty
from mathutils import Vector

from .runtime import active_job


_DOMAIN_DEFAULTS = {
    "resolution": 512,
    "dissipation": 1.0,
    "temp_per_burn": 1.0,
    "preview_max_ray_steps": 256,
}

PRESETS = {
    "SMOKE": {
        "name": "Rising Smoke",
        "description": "A continuous rising plume without flames",
        "domain": {"shader_flame_enabled": False, "vorticity": 4.0,
                   "shader_smoke_density": 2.0,
                   "shader_smoke_color": (0.6038313,) * 3},
        "emitter": {"emitter_smoke": 5.0, "emitter_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 1.0, "emitter_divergence": 2.0},
    },
    "FIRE": {
        "name": "Steady Fire",
        "description": "Continuous flames with rising smoke",
        "domain": {"shader_flame_enabled": True, "vorticity": 4.0,
                   "buoyancy_per_temp": 1.5, "shader_smoke_density": 3.0,
                   "shader_smoke_color": (0.3185545,) * 3},
        "emitter": {"emitter_smoke": 5.0, "emitter_temperature": 2.0,
                    "emitter_fuel": 0.5, "normal_velocity": 1.0, "emitter_divergence": 2.0},
    },
    "EXPLOSION": {
        "name": "Explosion",
        "description": "A short burst of expanding fire and smoke",
        "domain": {"shader_flame_enabled": True, "vorticity": 6.0,
                   "buoyancy_per_temp": 1.5, "burn_per_temp": 2.0,
                   "divergence_per_burn": 4.0, "cooling_rate": 0.5,
                   "shader_smoke_density": 3.0,
                   "shader_smoke_color": (0.3185545,) * 3},
        "emitter": {"emitter_smoke": 5.0, "emitter_temperature": 1.0,
                    "emitter_fuel": 2.0, "normal_velocity": 1.0, "emitter_divergence": 2.0,
                    "velocity": (0.0, 0.0, 7.0)},
        "burst_frames": 10,
        "duration": 100,
    },
}


def _source_error(context, sources):
    if not sources:
        return "Select a mesh source, or choose New Source"
    for obj in sources:
        if obj.type != "MESH":
            return f"{obj.name} is not a mesh; choose mesh sources or New Source"
        if not obj.is_editable:
            return f"{obj.name} is linked and cannot be edited; choose New Source"
        if obj.fumaris.smoke_object_type != "none":
            return f"{obj.name} already has a Fumaris role; choose New Source"
        # Activating an unassigned object in an existing participant collection
        # could silently make it emit into two simulations.
        for domain in bpy.data.objects:
            props = getattr(domain, "fumaris", None)
            if props is None or props.smoke_object_type != "domain":
                continue
            collection = props.emitter_collection
            if collection and obj.name in collection.all_objects:
                return f"{obj.name} is already in {domain.name}'s emitters; choose New Source"
    return None


def _apply_settings(props, values):
    for name, value in values.items():
        setattr(props, name, value)


def _new_collection(name, parent):
    collection = bpy.data.collections.new(name)
    parent.children.link(collection)
    return collection


def _new_source(collection, name, position):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_icosphere(bm, subdivisions=2, radius=1.0)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = position
    return obj


def _source_bounds(context, sources):
    depsgraph = context.evaluated_depsgraph_get()
    positions = []
    for obj in sources:
        evaluated = obj.evaluated_get(depsgraph)
        for corner in evaluated.bound_box:
            position = evaluated.matrix_world @ Vector(corner)
            if all(math.isfinite(value) for value in position):
                positions.append(position)
    if not positions:
        positions = [context.scene.cursor.location.copy()]
    minimum = Vector(tuple(min(point[axis] for point in positions) for axis in range(3)))
    maximum = Vector(tuple(max(point[axis] for point in positions) for axis in range(3)))
    return minimum, maximum


def _new_domain(collection, name, minimum, maximum):
    # A settings anchor, not a physical boundary for the sparse simulation.
    obj = bpy.data.objects.new(name, None)
    collection.objects.link(obj)
    obj.location = (minimum + maximum) * 0.5
    obj.empty_display_type = "PLAIN_AXES"
    obj.empty_display_size = max(0.5, max(maximum - minimum) * 0.5)
    obj.hide_render = True
    return obj


def _configure_source(obj, preset, start_frame, current_frame):
    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_enabled = True
    if obj.particle_systems:
        props.participant_type = "particles"
        props.particle_system_name = obj.particle_systems[0].name
        props.particle_subtype = "point_cloud"
    elif any(modifier.type == "NODES" for modifier in obj.modifiers):
        props.participant_type = "geometry_nodes"
        props.gn_subtype = "mesh"
    else:
        props.participant_type = "mesh"
    _apply_settings(props, preset["emitter"])
    duration = preset.get("burst_frames")
    if duration:
        animation = obj.animation_data
        action = animation.action if animation else None
        if action and (action.users > 1 or action.library or not action.is_editable):
            # Object copies can share the same Action and even the same Action
            # Slot. Keep the burst keys private without losing other channels
            # or switching to another slot when the copied action is assigned.
            slot = animation.action_slot
            slot_identifier = slot.identifier if slot else None
            animation.action = action.copy()
            if slot_identifier:
                animation.action_slot = animation.action.slots[slot_identifier]
        # Boolean channels use discrete interpolation. Animate only the new
        # Fumaris enable channel, leaving transforms and other animation intact.
        props.participant_enabled = True
        props.keyframe_insert("participant_enabled", frame=start_frame + duration - 1)
        props.participant_enabled = False
        props.keyframe_insert("participant_enabled", frame=start_frame + duration)
        props.participant_enabled = current_frame < start_frame + duration


class FUMARIS_OT_quick_setup(bpy.types.Operator):
    bl_idname = "fumaris.quick_setup"
    bl_label = "Fumaris Quick Setup"
    bl_description = "Create a ready-to-preview smoke or fire setup with linked emitter collections"
    bl_options = {"REGISTER", "UNDO"}

    preset: EnumProperty(
        name="Effect",
        items=[(key, values["name"], values["description"]) for key, values in PRESETS.items()],
        default="SMOKE",
    )
    source_mode: EnumProperty(
        name="Emit From",
        items=[
            ("SELECTED", "Selected Objects", "Use selected mesh objects without an existing Fumaris role"),
            ("NEW", "New Source", "Create a source at the 3D cursor; leave selected objects unchanged"),
        ],
        default="NEW",
    )

    @classmethod
    def poll(cls, context):
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode to create a Fumaris setup")
            return False
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation before creating a setup")
            return False
        return context.scene is not None

    def invoke(self, context, event):
        self.source_mode = "NEW" if _source_error(context, context.selected_objects) else "SELECTED"
        return context.window_manager.invoke_props_dialog(self, width=360)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "preset")
        layout.prop(self, "source_mode")
        if self.source_mode == "SELECTED":
            error = _source_error(context, context.selected_objects)
            if error:
                layout.label(text=error, icon="ERROR")
            else:
                layout.label(text=f"{len(context.selected_objects)} selected mesh source(s)", icon="MESH_DATA")
        layout.label(text="Creates a simulation empty and participant collections", icon="MOD_FLUID")

    def execute(self, context):
        sources = list(context.selected_objects) if self.source_mode == "SELECTED" else []
        if self.source_mode == "SELECTED":
            error = _source_error(context, sources)
            if error:
                self.report({"WARNING"}, error)
                return {"CANCELLED"}
        preset = PRESETS[self.preset]
        name = preset["name"]
        root = _new_collection(f"Fumaris {name}", context.scene.collection)
        collections = {
            role: _new_collection(f"{name} {label}", root)
            for role, label in (
                ("emitter", "Emitters"), ("collider", "Colliders"),
                ("effector", "Effectors"), ("outflow", "Outflows"),
            )
        }
        if not sources:
            sources = [_new_source(collections["emitter"], f"{name} Source", context.scene.cursor.location)]
        else:
            for source in sources:
                collections["emitter"].objects.link(source)
        minimum, maximum = _source_bounds(context, sources)
        domain = _new_domain(root, f"{name} Simulation", minimum, maximum)
        props = domain.fumaris
        props.smoke_object_type = "domain"
        props.sim_start_frame = context.scene.frame_start
        props.sim_end_frame = props.sim_start_frame + preset["duration"] - 1 if "duration" in preset else context.scene.frame_end
        _apply_settings(props, _DOMAIN_DEFAULTS)
        _apply_settings(props, preset["domain"])
        for role, collection in collections.items():
            setattr(props, f"{role}_collection", collection)
        for source in sources:
            _configure_source(source, preset, props.sim_start_frame, context.scene.frame_current)
        for obj in context.selected_objects:
            obj.select_set(False)
        domain.select_set(True)
        context.view_layer.objects.active = domain
        if hasattr(context.scene, "fumaris_preview_domain"):
            context.scene.fumaris_preview_domain = domain
        context.view_layer.update()
        self.report({"INFO"}, f"{name} ready: press Play")
        return {"FINISHED"}


def _draw_add_menu(self, context):
    obj = context.object
    if obj is None or obj.fumaris.smoke_object_type == "none":
        self.layout.operator(FUMARIS_OT_quick_setup.bl_idname, text="Fumaris Quick Setup…", icon="MOD_FLUID")


def register():
    bpy.utils.register_class(FUMARIS_OT_quick_setup)
    bpy.types.VIEW3D_MT_add.append(_draw_add_menu)


def unregister():
    bpy.types.VIEW3D_MT_add.remove(_draw_add_menu)
    bpy.utils.unregister_class(FUMARIS_OT_quick_setup)
