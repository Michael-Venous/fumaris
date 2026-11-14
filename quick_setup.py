"""Small, undoable starting scenes without changing existing simulations."""

import math

import bmesh
import bpy
from bpy.props import EnumProperty
from mathutils import Vector

from . import detail_stack
from .runtime import active_job


# Presets start from the domain defaults (properties.py)
# and change only what makes each effect what it is (2.0 preset pass,
# 2026-11-07): the look, combustion and fade settings stay the defaults.
# 2 GB of VRAM: the presets use at most ~8,300 blocks (the Explosion with its
# stem at frame 120; 2 GB holds 14,000+), and frames stay under ~32 ms (95th
# percentile) at 1 Sub-Step on an RTX 4060 Ti, so preview playback keeps 24 fps
# (output/qs_pass 2026-11-08, output/perf4 2026-11-09).
_DOMAIN_DEFAULTS = {
    "resolution": 512,
    "simulation_memory": 2.0,
}

# Emitters are physical since 2.0: Expansion in 1/s, and Normal Velocity on a
# closed source is a volume source (v x area), so presets use smaller values
# than the old, mostly cancelled per-cell push (output/expansion, 2026-11-04).
PRESETS = {
    "SMOKE": {
        "name": "Rising Smoke",
        "description": "A continuous rising plume without flames",
        "domain": {"shader_flame_enabled": False},
        # Hot (Flame Temperature) smoke rises on its own heat.
        "emitter": {"emitter_smoke": 5.0, "emitter_temperature": 2.0,
                    "source_op_temperature": "add", "couple_rate_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 0.25, "emitter_divergence": 0.2},
    },
    "FIRE": {
        "name": "Steady Fire",
        "description": "Continuous flames with rising smoke",
        # Short-lived fuel ends the flames a few source widths up instead of
        # in a tall column; more Vorticity makes them flicker (2026-11-07).
        "domain": {"shader_flame_enabled": True, "burn_time": 0.5, "vorticity": 4.0},
        # The source only ignites the fuel; burning makes the heat (Fuel 1
        # burns at Flame Temperature with the default Fuel Energy).
        "emitter": {"emitter_smoke": 5.0, "emitter_temperature": 0.35,
                    "couple_rate_temperature": 10.0, "source_op_temperature": "max",
                    "emitter_fuel": 1.0, "normal_velocity": 0.25, "emitter_divergence": 0.2},
    },
    "EXPLOSION": {
        "name": "Explosion",
        "description": "A short burst of expanding fire and smoke",
        # Hot gas rises at about g (1 - T_air / T) ~ 8 m/s^2: the fireball
        # lifts into a cap over a smoke stem. Disturb (size set below) breaks
        # it into cauliflower billows; the fire burns out in about a second,
        # leaving glowing cracks in the smoke (output/presets1.4).
        # Grander (2026-11-08): a longer, stronger burst (8 frames, Fuel 16,
        # Expansion 14) and more soot (Smoke Amount 2.5) leave a large
        # mushroom cloud instead of a fireball that fades within a second.
        "domain": {"shader_flame_enabled": True, "buoyancy_per_temp": 8.0, "smoke_per_burn": 2.5},
        # A Disturb layer; its largest swirls relative to the sources' largest dimension.
        "disturb": {"strength": 220.0, "detail": 3, "size_per_size": 0.3},
        # No forced jet: a sustained upward push sheds stacked vortex rings.
        # The source only ignites the fuel (Temperature 1 is Flame
        # Temperature); burning heats it. Light source smoke: soot forms as
        # the fuel burns, so the young fireball glows instead of hiding in
        # smoke.
        "emitter": {"emitter_smoke": 1.5, "emitter_temperature": 0.35,
                    "couple_rate_temperature": 30.0, "source_op_temperature": "max",
                    # Expansion burst: the source gas swells about 14x per
                    # second during its burst frames, a pressure blast that
                    # drives the fast, tearing front of a detonation.
                    "emitter_fuel": 16.0, "normal_velocity": 1.0, "emitter_divergence": 14.0,
                    "velocity": (0.0, 0.0, 0.0)},
        "burst_frames": 8,
        # Then a stem: the source vents warm soot for 80 frames, fading over the
        # last 30, so the cloud stands on a column instead of floating off the
        # ground (2026-11-09, output/qs_review/exp1-2).
        "stem": {"frames": 80, "smoke": 3.0, "temperature": 0.4, "normal_velocity": 1.0,
                 "expansion": 1.0, "fade": 30},
        "duration": 120,
    },
    "FOG": {
        "name": "Ground Fog",
        "description": "Cold, heavy fog that spills out and creeps along the ground",
        # Cold smoke heavier than air sinks and spreads along the ground.
        "domain": {"shader_flame_enabled": False, "buoyancy_per_smoke": -0.4,
                   "shader_smoke_color": (0.8,) * 3},
        # Disturb rolls the spreading fog into billows; without it the heavy
        # sheet sloshed outward in regular concentric rings (2026-11-09,
        # output/qs_review/fog1-2).
        "disturb": {"strength": 25.0, "detail": 1, "size_per_size": 0.5},
        "emitter": {"emitter_smoke": 2.0, "emitter_temperature": 0.0,
                    "source_op_temperature": "add", "couple_rate_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 0.5, "emitter_divergence": 0.3},
        "ground": True,
    },
    "STACK": {
        "name": "Chimney Smoke",
        "description": "A warm column of smoke blown upward from the source, as from a chimney or stack",
        # Disturb breaks the column into turbulent smoke a few source widths
        # up; without it the exhaust rose as a smooth tube.
        "domain": {"shader_flame_enabled": False},
        "disturb": {"strength": 60.0, "detail": 1, "size_per_size": 0.5},
        # Warm exhaust (a quarter of Flame Temperature) pushed up at 4 m/s.
        "emitter": {"emitter_smoke": 4.0, "emitter_temperature": 0.25,
                    "source_op_temperature": "add", "couple_rate_temperature": 2.0,
                    "emitter_fuel": 0.0, "normal_velocity": 0.0, "emitter_divergence": 0.0,
                    "velocity": (0.0, 0.0, 4.0), "couple_rate_velocity": 8.0},
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


_ROOT_MARKER = "fumaris_root"
_ROLE_LABELS = (("emitter", "Emitters"), ("collider", "Colliders"),
                ("effector", "Effectors"), ("outflow", "Outflows"))


def fumaris_root(scene):
    """The scene's top-level Fumaris collection, which holds one group per
    domain: found by its marker (a renamed root still works), else made."""
    for child in scene.collection.children:
        if child.get(_ROOT_MARKER):
            return child
    root = _new_collection("Fumaris", scene.collection)
    root[_ROOT_MARKER] = True
    return root


def new_domain_group(scene, name):
    """Fumaris > name > the domain's Emitters, Colliders, Effectors and
    Outflows collections. Returns (group, {role: collection})."""
    group = _new_collection(name, fumaris_root(scene))
    return group, {role: _new_collection(f"{group.name} {label}", group) for role, label in _ROLE_LABELS}


def _new_source(collection, name, position):
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        # Smooth enough not to show facets when scaled up for large effects.
        bmesh.ops.create_icosphere(bm, subdivisions=4, radius=1.0)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = position
    return obj


def _new_debris(collection, name, minimum, maximum, start_frame):
    """Debris thrown out of an explosion: particles that leave smoke trails.

    A hidden icosphere the size of the sources emits a few dozen particles in
    the first two frames, flung outward and upward and pulled down by scene
    gravity. Each particle is a small point emitter of hot smoke, so its head
    glows briefly and its path leaves a thin trail that cools to grey.
    """
    size = max(0.1, max(maximum - minimum))
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_icosphere(bm, subdivisions=2, radius=0.5 * size)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = (minimum + maximum) * 0.5
    obj.hide_render = True
    obj.display_type = "WIRE"
    modifier = obj.modifiers.new(name, "PARTICLE_SYSTEM")
    settings = modifier.particle_system.settings
    settings.name = name
    settings.count = 32
    settings.frame_start = start_frame
    settings.frame_end = start_frame + 1
    # Ejecta speed grows with the blast size: ~8 m/s at 2 m, ~27 m/s at 20 m.
    speed = 6.0 * math.sqrt(size)
    # Short-lived: trails reach about twice the source size before they
    # stop, so they do not claim memory far from the fireball.
    settings.lifetime = 30
    settings.lifetime_random = 0.4
    settings.emit_from = "FACE"
    settings.normal_factor = speed
    settings.object_align_factor = (0.0, 0.0, 0.6 * speed)
    settings.factor_random = 0.5 * speed
    settings.physics_type = "NEWTON"
    settings.render_type = "NONE"
    settings.display_method = "DOT"
    props = obj.fumaris
    props.smoke_object_type = "emitter"
    props.participant_enabled = True
    props.participant_type = "particles"
    props.particle_system_name = modifier.particle_system.name
    props.particle_subtype = "point_cloud"
    # About the distance a particle moves per solver step, so its stamps
    # join into a trail instead of a row of beads.
    props.point_radius = 0.04 * size
    _apply_settings(props, {"emitter_smoke": 2.0, "emitter_temperature": 0.6,
                            "source_op_temperature": "max", "emitter_fuel": 0.0,
                            "normal_velocity": 0.0, "emitter_divergence": 0.0})
    return obj


def _new_ground(collection, name, minimum, maximum):
    """A collider floor just under the sources, ten source widths across."""
    size = 10.0 * max(1.0, max(maximum - minimum))
    mesh = bpy.data.meshes.new(name)
    bm = bmesh.new()
    try:
        bmesh.ops.create_grid(bm, x_segments=1, y_segments=1, size=0.5 * size)
        bm.to_mesh(mesh)
    finally:
        bm.free()
    obj = bpy.data.objects.new(name, mesh)
    collection.objects.link(obj)
    obj.location = ((minimum.x + maximum.x) * 0.5, (minimum.y + maximum.y) * 0.5, minimum.z - 0.05)
    obj.fumaris.smoke_object_type = "collider"
    obj.fumaris.participant_enabled = True
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
        burst_end = start_frame + duration - 1
        stem = preset.get("stem")
        end = burst_end
        if stem:
            # After the blast the source keeps venting warm, sooty gas (no fuel,
            # no expansion) that fades out: the column the rising fireball
            # draws up under itself, so the cloud stands on a stem.
            stem_values = {"emitter_fuel": 0.0, "emitter_divergence": stem.get("expansion", 0.0),
                           "emitter_smoke": stem["smoke"], "emitter_temperature": stem["temperature"],
                           "normal_velocity": stem.get("normal_velocity", 0.0)}
            end = burst_end + stem["frames"]
            for name, value in stem_values.items():
                burst = getattr(props, name)
                props.keyframe_insert(name, frame=burst_end)
                setattr(props, name, value)
                props.keyframe_insert(name, frame=burst_end + 1)
                if name in ("emitter_smoke", "emitter_temperature"):
                    props.keyframe_insert(name, frame=end - stem.get("fade", 0))
                    setattr(props, name, 0.0)
                    props.keyframe_insert(name, frame=end)
                setattr(props, name, burst)
        props.participant_enabled = True
        props.keyframe_insert("participant_enabled", frame=end)
        props.participant_enabled = False
        props.keyframe_insert("participant_enabled", frame=end + 1)
        props.participant_enabled = current_frame <= end


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
    debris: bpy.props.BoolProperty(
        name="Debris Trails",
        description="Explosion only: throw a few dozen particles out of the blast that leave hot, smoky trails",
        default=False,
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
        if self.preset == "EXPLOSION":
            layout.prop(self, "debris")
        layout.label(text="Creates a simulation empty and its collections under Fumaris", icon="MOD_FLUID")

    def execute(self, context):
        sources = list(context.selected_objects) if self.source_mode == "SELECTED" else []
        if self.source_mode == "SELECTED":
            error = _source_error(context, sources)
            if error:
                self.report({"WARNING"}, error)
                return {"CANCELLED"}
        preset = PRESETS[self.preset]
        # A second Rising Smoke is "Rising Smoke 2", its objects named to match.
        name = _unique_group_name(preset["name"])
        root, collections = new_domain_group(context.scene, name)
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
        # Short effects keep their own length; the rest follow the timeline.
        props.use_scene_frame_range = "duration" not in preset
        _apply_settings(props, _DOMAIN_DEFAULTS)
        _apply_settings(props, preset["domain"])
        disturb = preset.get("disturb")
        if disturb:
            # Disturb's size is in world units: scale it to the sources so
            # large explosions get large billows (0.1 m did nothing at 20 m).
            detail_stack.add_layer(props, "DISTURB", strength=disturb["strength"], detail=disturb["detail"],
                                   size=max(0.05, disturb["size_per_size"] * max(maximum - minimum)))
        for role, collection in collections.items():
            setattr(props, f"{role}_collection", collection)
        for source in sources:
            _configure_source(source, preset, props.sim_start_frame, context.scene.frame_current)
        if preset.get("ground"):
            _new_ground(collections["collider"], f"{name} Ground", minimum, maximum)
        if self.debris and self.preset == "EXPLOSION":
            _new_debris(collections["emitter"], f"{name} Debris", minimum, maximum, props.sim_start_frame)
        for obj in context.selected_objects:
            obj.select_set(False)
        domain.select_set(True)
        context.view_layer.objects.active = domain
        if hasattr(context.scene, "fumaris_preview_domain"):
            from .playback import choose_domain
            choose_domain(context.scene, domain)
        context.view_layer.update()
        self.report({"INFO"}, f"{name} ready: press Play")
        return {"FINISHED"}


def _unique_group_name(base):
    if base not in bpy.data.collections:
        return base
    number = 2
    while f"{base} {number}" in bpy.data.collections:
        number += 1
    return f"{base} {number}"


class FUMARIS_OT_domain_create(bpy.types.Operator):
    bl_idname = "fumaris.domain_create"
    bl_label = "New Domain"
    bl_description = ("Create an empty simulation domain at the 3D cursor, with its Emitters, Colliders, Effectors "
                      "and Outflows collections under the Fumaris collection, and choose it in the Fumaris tab")
    bl_options = {"REGISTER", "UNDO"}

    # Empty: the Fumaris tab's name box (then Simulation).
    name: bpy.props.StringProperty(name="Name")

    @classmethod
    def poll(cls, context):
        return FUMARIS_OT_quick_setup.poll(context)

    def execute(self, context):
        scene = context.scene
        typed = getattr(scene, "fumaris_new_domain_name", "").strip()
        name = _unique_group_name(self.name.strip() or typed or "Simulation")
        if typed:
            scene.fumaris_new_domain_name = ""   # used once
        group, collections = new_domain_group(context.scene, name)
        cursor = context.scene.cursor.location
        domain = _new_domain(group, f"{name} Domain", cursor - Vector((1, 1, 1)), cursor + Vector((1, 1, 1)))
        props = domain.fumaris
        props.smoke_object_type = "domain"
        props.sim_start_frame = context.scene.frame_start
        props.sim_end_frame = context.scene.frame_end
        _apply_settings(props, _DOMAIN_DEFAULTS)
        for role, collection in collections.items():
            setattr(props, f"{role}_collection", collection)
        for obj in context.selected_objects:
            obj.select_set(False)
        domain.select_set(True)
        context.view_layer.objects.active = domain
        from .playback import choose_domain
        choose_domain(context.scene, domain)
        self.report({"INFO"}, f"{domain.name} ready: add emitters from the Fumaris tab")
        return {"FINISHED"}


_ROLE_NAMES = dict(_ROLE_LABELS)


def _add_error(obj, role, domain):
    """Why obj cannot join domain as role, or None."""
    from .participants import participant_for_object
    if obj.fumaris.smoke_object_type == "domain":
        return "a domain"
    if not obj.is_editable:
        return "linked from a library"
    kind = obj.fumaris.smoke_object_type
    if kind not in {"none", role}:
        return f"already a Fumaris {kind}"
    if participant_for_object(obj, role, 0, "") is None:
        return f"{obj.type.lower()} objects cannot be {_ROLE_NAMES[role].lower()} yet"
    return None


class FUMARIS_OT_participants_add_selected(bpy.types.Operator):
    bl_idname = "fumaris.participants_add_selected"
    bl_label = "Add Participant"
    bl_description = "Add selected objects to the chosen domain, or create a participant at the 3D cursor"
    bl_options = {"REGISTER", "UNDO"}

    role: EnumProperty(name="Role", items=[(role, label[:-1], "") for role, label in _ROLE_LABELS])

    @classmethod
    def description(cls, context, properties):
        if not any(obj.fumaris.smoke_object_type != "domain" for obj in context.selected_objects):
            return f"Create a new {_ROLE_NAMES[properties.role][:-1].lower()} at the 3D cursor in the chosen domain"
        return (f"Make the selected objects {_ROLE_NAMES[properties.role].lower()} of the chosen domain: each is "
                "linked into the domain's collection for that role and keeps its other collections")

    @classmethod
    def poll(cls, context):
        from .playback import resolve_domain
        if context.mode != "OBJECT":
            cls.poll_message_set("Switch to Object Mode")
            return False
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation first")
            return False
        if resolve_domain(context) is None:
            cls.poll_message_set("Choose a domain in the Fumaris tab")
            return False
        return True

    def execute(self, context):
        from .playback import resolve_domain
        domain = resolve_domain(context)
        props = domain.fumaris
        attribute = f"{self.role}_collection"
        collection = getattr(props, attribute)
        if collection is None:
            # A domain made by hand: give it the role's collection beside it.
            parent = domain.users_collection[0] if domain.users_collection else context.scene.collection
            collection = _new_collection(f"{domain.name} {_ROLE_NAMES[self.role]}", parent)
            setattr(props, attribute, collection)
        selected = [obj for obj in context.selected_objects if obj.fumaris.smoke_object_type != "domain"]
        if not selected:
            name = f"{domain.name} {_ROLE_NAMES[self.role][:-1]}"
            if self.role == "effector":
                obj = bpy.data.objects.new(name, None)
                collection.objects.link(obj)
                obj.empty_display_type = "SPHERE"
            elif self.role == "collider":
                mesh = bpy.data.meshes.new(name)
                bm = bmesh.new()
                try:
                    bmesh.ops.create_cube(bm, size=2.0)
                    bm.to_mesh(mesh)
                finally:
                    bm.free()
                obj = bpy.data.objects.new(name, mesh)
                collection.objects.link(obj)
            else:
                obj = _new_source(collection, name, context.scene.cursor.location)
            obj.location = context.scene.cursor.location
            obj.fumaris.smoke_object_type = self.role
            obj.fumaris.participant_enabled = True
            for other in context.selected_objects:
                other.select_set(False)
            obj.select_set(True)
            context.view_layer.objects.active = obj
            self.report({"INFO"}, f"Created {obj.name} in {domain.name}")
            return {"FINISHED"}
        added, skipped = [], []
        for obj in sorted(context.selected_objects, key=lambda item: item.name):
            error = _add_error(obj, self.role, domain)
            if error:
                skipped.append(f"{obj.name} ({error})")
                continue
            if obj.name not in collection.all_objects:
                collection.objects.link(obj)
            obj.fumaris.smoke_object_type = self.role
            added.append(obj.name)
        role = _ROLE_NAMES[self.role].lower() if len(added) != 1 else _ROLE_NAMES[self.role][:-1].lower()
        if not added:
            self.report({"WARNING"}, "Nothing added: " + "; ".join(skipped))
            return {"CANCELLED"}
        message = f"Added {len(added)} {role} to {domain.name}"
        if skipped:
            self.report({"WARNING"}, message + "; skipped " + "; ".join(skipped))
        else:
            self.report({"INFO"}, message)
        return {"FINISHED"}


class FUMARIS_OT_domain_choose(bpy.types.Operator):
    bl_idname = "fumaris.domain_choose"
    bl_label = "Choose Domain"
    bl_description = ("Choose this domain for the Fumaris tab, playback and Add actions, select it and show its "
                      "settings. It stays chosen while you select its emitters and colliders")
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation to choose another domain")
            return False
        return True

    def invoke(self, context, event):
        domain = bpy.data.objects.get(self.name)
        if domain is None or domain.name not in context.view_layer.objects:
            self.report({"WARNING"}, f"{self.name} is not in this view layer")
            return {"CANCELLED"}
        from .playback import choose_domain
        choose_domain(context.scene, domain)
        return bpy.ops.fumaris.participant_select("INVOKE_DEFAULT", name=domain.name, activate=True)


class FUMARIS_OT_participant_remove(bpy.types.Operator):
    bl_idname = "fumaris.participant_remove"
    bl_label = "Remove from Domain"
    bl_description = ("Take this object out of the chosen domain: unlink it from the domain's collections and clear "
                      "its Fumaris role. The object stays in the scene")
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def poll(cls, context):
        return not active_job()

    def execute(self, context):
        from .playback import resolve_domain
        obj = bpy.data.objects.get(self.name)
        domain = resolve_domain(context)
        if obj is None or domain is None:
            return {"CANCELLED"}
        props = domain.fumaris
        for role, _label in _ROLE_LABELS:
            top = getattr(props, f"{role}_collection")
            if top is None:
                continue
            for collection in [top, *top.children_recursive]:
                if obj.name in collection.objects:
                    if len(obj.users_collection) == 1:
                        # Its only collection: keep it in the scene.
                        context.scene.collection.objects.link(obj)
                    collection.objects.unlink(obj)
        obj.fumaris.smoke_object_type = "none"
        self.report({"INFO"}, f"Removed {obj.name} from {domain.name}")
        return {"FINISHED"}


class FUMARIS_OT_object_delete(bpy.types.Operator):
    bl_idname = "fumaris.object_delete"
    bl_label = "Delete"
    bl_description = "Delete this object from the file"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})

    @classmethod
    def description(cls, context, properties):
        obj = bpy.data.objects.get(properties.name)
        if obj is not None and obj.fumaris.smoke_object_type == "domain":
            return ("Delete this domain object. Its collections and the objects in them are kept; "
                    "its baked files stay on disk")
        return "Delete this object"

    @classmethod
    def poll(cls, context):
        if active_job():
            cls.poll_message_set("Stop the active Fumaris simulation first")
            return False
        return True

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return {"CANCELLED"}
        name = obj.name
        if getattr(context.scene, "fumaris_preview_domain", None) == obj:
            context.scene.fumaris_preview_domain = None
        bpy.data.objects.remove(obj, do_unlink=True)
        self.report({"INFO"}, f"Deleted {name}")
        return {"FINISHED"}


class FUMARIS_OT_object_rename(bpy.types.Operator):
    bl_idname = "fumaris.object_rename"
    bl_label = "Rename"
    bl_description = "Rename this object"
    bl_options = {"REGISTER", "UNDO"}

    name: bpy.props.StringProperty(options={"HIDDEN"})
    new_name: bpy.props.StringProperty(name="Name")

    def invoke(self, context, event):
        obj = bpy.data.objects.get(self.name)
        if obj is None:
            return {"CANCELLED"}
        self.new_name = obj.name
        return context.window_manager.invoke_props_dialog(self, width=260)

    def draw(self, context):
        self.layout.activate_init = True   # type at once
        self.layout.prop(self, "new_name", text="")

    def execute(self, context):
        obj = bpy.data.objects.get(self.name)
        new_name = self.new_name.strip()
        if obj is None or not new_name:
            return {"CANCELLED"}
        obj.name = new_name
        return {"FINISHED"}


def _button_context_menu(self, context):
    """Right-click on a domain or object in the Fumaris tab: delete it."""
    op = getattr(context, "button_operator", None)
    try:
        identifier = op.bl_rna.identifier if op is not None else ""
    except (AttributeError, ReferenceError):
        return
    if identifier not in {"FUMARIS_OT_domain_choose", "FUMARIS_OT_participant_select"}:
        return
    obj = bpy.data.objects.get(op.name)
    if obj is None:
        return
    layout = self.layout
    layout.separator()
    layout.label(text=obj.name, icon="OBJECT_DATA")
    layout.operator("fumaris.object_rename", text="Rename…", icon="GREASEPENCIL").name = obj.name
    if identifier == "FUMARIS_OT_domain_choose":
        layout.operator("fumaris.object_delete", text="Delete Domain (keeps its collections)",
                        icon="TRASH").name = obj.name
        return
    layout.operator("fumaris.participant_remove", icon="REMOVE").name = obj.name
    layout.operator("fumaris.object_delete", text="Delete Object", icon="TRASH").name = obj.name


def _draw_add_menu(self, context):
    obj = context.object
    if obj is None or obj.fumaris.smoke_object_type == "none":
        self.layout.operator(FUMARIS_OT_quick_setup.bl_idname, text="Fumaris Quick Setup…", icon="MOD_FLUID")


_CLASSES = (FUMARIS_OT_quick_setup, FUMARIS_OT_domain_create, FUMARIS_OT_participants_add_selected,
            FUMARIS_OT_domain_choose, FUMARIS_OT_participant_remove, FUMARIS_OT_object_delete,
            FUMARIS_OT_object_rename)


def register():
    bpy.types.Scene.fumaris_new_domain_name = bpy.props.StringProperty(
        name="Domain Name", description="Name for the next New Domain (empty: Simulation)")
    for cls in _CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.UI_MT_button_context_menu.append(_button_context_menu)
    bpy.types.VIEW3D_MT_add.append(_draw_add_menu)


def unregister():
    bpy.types.VIEW3D_MT_add.remove(_draw_add_menu)
    bpy.types.UI_MT_button_context_menu.remove(_button_context_menu)
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.fumaris_new_domain_name
