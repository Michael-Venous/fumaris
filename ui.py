import bpy
import os
import textwrap
from bpy.types import Panel

from . import detail_stack
from .runtime import active_job, active_mode, background_job, blocking_job
from .jobs import WRITING_LAST_FRAMES, writing_last_frames
from . import diagnostics
from .memory_auto import draw_budget_summary, draw_full_resolution_forecast
from .cache import is_protected
from .sparks import MISSING_MARKER as SPARKS_MISSING_MARKER
from .utils import FLAME_VALUE_KEYS, flame_value, glow_source_heat, is_pyro_look, peek_output_directory_for_object, physical_buoyancy


def _foldout(layout, props, property_name, label):
    row = layout.row(align=True)
    # Left, like the section headers (a full-width unembossed button centres
    # its text, 2026-11-14 UX test).
    row.alignment = "LEFT"
    opened = getattr(props, property_name)
    row.prop(
        props,
        property_name,
        text=label,
        icon="TRIA_DOWN" if opened else "TRIA_RIGHT",
        emboss=False,
    )
    return opened


# Set per draw: the Properties editor is too narrow for two labelled values
# side by side (Blender's default ~330 px truncated every pair's label).
_NARROW = False
NARROW_WIDTH = 440
# Narrower still (Blender's default Properties editor, ~250 px): three
# labelled buttons no longer fit a row, so rows stack and text wraps.
_TIGHT = False
TIGHT_WIDTH = 330
# About how many characters of label text fit a line (for wrapping status).
_LINE_CHARS = 60


def _scene_heat(props):
    """Whether the domain's emitters heat or burn."""
    from .exporters import _participants, emitted_fields
    try:
        return emitted_fields(_participants(props)) != "smoke"
    except Exception:
        return False


def _fire_on_half_storage(props):
    """A fire whose fuel and burn stay at half resolution without
    Full-Resolution Channels (no other option keeps all four channels)."""
    from .exporters import _participants, emitted_fields, full_resolution_forced
    if (full_resolution_forced(props) or props.density_storage != "auto"
            or props.export_fuel_vdb or props.export_burn_vdb):
        return False
    try:
        return emitted_fields(_participants(props)) == "fire"
    except Exception:
        return False


def _fire_storage(props):
    """Where the domain's fire keeps its fuel and burn: "half" (fire
    storage), "full" (Full-Resolution Channels) or "none" (no fire, or
    another option already keeps all four channels)."""
    if not _fire_on_half_storage(props):
        return "none"
    return "full" if props.full_resolution_velocity else "half"


def _wrapped(layout, text, icon="NONE"):
    """A status label that wraps instead of being cut off in a narrow panel."""
    lines = textwrap.wrap(text, width=max(16, _LINE_CHARS)) or [""]
    for index, line in enumerate(lines):
        layout.label(text=line, icon=icon if index == 0 else ("BLANK1" if icon != "NONE" else "NONE"))


def _pair(layout):
    """Two labelled values: side by side in a wide panel, stacked in a narrow one."""
    return layout.column(align=True) if _NARROW else layout.row(align=True)


def _label_split(layout, wide, narrow):
    return layout.split(factor=narrow if _NARROW else wide, align=True)


def _section(box, props, property_name, label, icon):
    """A top-level foldout: its arrow at the box edge (like Advanced), then the
    section's icon and title; folded sections show only this row."""
    row = box.row(align=True)
    row.alignment = "LEFT"
    opened = getattr(props, property_name)
    row.prop(props, property_name, text="", icon="TRIA_DOWN" if opened else "TRIA_RIGHT", emboss=False)
    row.label(text=label, icon=icon)
    return opened


def _nested_foldout(layout, props, property_name, label):
    row = layout.row(align=True)
    row.separator(factor=0.65)
    column = row.column(align=True)
    opened = _foldout(column, props, property_name, label)
    if opened:
        # Space between the header and its first option, like every group.
        layout.separator(factor=0.5)
        content = layout.row(align=True)
        content.separator(factor=1.3)
        return content.column(align=True), opened
    return column, opened


_ROLE_ICONS = {"emitter": "OUTLINER_OB_FORCE_FIELD", "collider": "MOD_PHYSICS",
               "effector": "FORCE_WIND", "outflow": "TRIA_DOWN_BAR"}


def _draw_participant_list(layout, props):
    """Objects in the domain's collections: select, enable, solo, and why an
    object is not simulated."""
    layout.separator(factor=0.5)
    content, opened = _nested_foldout(layout, props, "show_participant_list", "Objects")
    if not opened:
        return
    draw_participant_rows(content, props)


def _highlight_row(layout, operator, name, icon, highlighted, **properties):
    """An operator button showing name on the left; highlighted, a blank
    pressed button joined to it fills the rest of the row (Blender centres
    the text of a button stretched across a row)."""
    sub = layout.row(align=True)
    sub.alignment = "LEFT"
    buttons = [sub.operator(operator, text=name, icon=icon, emboss=highlighted, depress=highlighted)]
    if highlighted:
        buttons.append(layout.operator(operator, text=" ", emboss=True, depress=True))
    for button in buttons:
        button.name = name
        for key, value in properties.items():
            setattr(button, key, value)


# Long object lists get a search box; Show More pages through them.
_SEARCH_FROM = 8
_PAGE = 60
# Separate emitters of one shape from which the panel suggests a Collection
# source (100 mesh emitters: 37.8 ms a frame, 4.2 ms as one Collection Mesh).
_COLLECTION_HINT_FROM = 16


class FUMARIS_OT_participant_list_more(bpy.types.Operator):
    bl_idname = "fumaris.participant_list_more"
    bl_label = "Show More"
    bl_description = f"Show {_PAGE} more objects in this list (or type part of a name in the search box)"

    def execute(self, context):
        context.window_manager.fumaris_participant_limit += _PAGE
        return {"FINISHED"}


def draw_participant_rows(content, props, *, activate=False):
    """One row per object in the domain's collections. activate (the
    sidebar): clicking a name makes it active and shows its settings."""
    from .operators import solo_object_name
    from .participants import participant_overview
    # Solo and Enabled act on this domain whatever object is active.
    content.context_pointer_set("object", props.id_data)
    wm = bpy.context.window_manager
    search = wm.fumaris_participant_search.strip().lower()
    counts = {}
    rows = participant_overview(props, limit=wm.fumaris_participant_limit, search=search, counts=counts)
    matching = counts.get("matching", 0)
    if search or matching > _SEARCH_FROM:
        content.prop(wm, "fumaris_participant_search", text="", icon="VIEWZOOM")
    if not rows:
        content.label(text="No matching objects" if search else "No objects in these collections", icon="INFO")
        return
    solo = solo_object_name(props.id_data)
    for obj, role, reason in rows:
        usable = reason is None or reason == "disabled"
        icon = _ROLE_ICONS.get(role, "OBJECT_DATA")
        # Selected objects are highlighted across the row, like in the Outliner.
        selected = activate and obj.select_get()
        if selected and usable:
            # One joined, pressed row: name, filler, Enabled and Solo.
            row = content.row(align=True)
            _highlight_row(row, "fumaris.participant_select", obj.name, icon, True, activate=activate)
            row.prop(obj.fumaris, "participant_enabled", text="", toggle=True,
                     icon="CHECKBOX_HLT" if obj.fumaris.participant_enabled else "CHECKBOX_DEHLT")
            solo_op = row.operator("fumaris.participant_solo", text="", depress=True,
                                   icon="SOLO_ON" if solo == obj.name else "SOLO_OFF")
            solo_op.name = obj.name
            continue
        split = content.split(factor=0.8 if usable else 0.45, align=True)
        _highlight_row(split.row(align=True), "fumaris.participant_select", obj.name, icon, selected,
                       activate=activate)
        row = split.row(align=True)
        row.alignment = "RIGHT" if usable else "LEFT"
        if not usable:
            row.alert = True
        if usable:
            row.prop(obj.fumaris, "participant_enabled", text="")
            solo_op = row.operator("fumaris.participant_solo", text="",
                                   icon="SOLO_ON" if solo == obj.name else "SOLO_OFF", emboss=False)
            solo_op.name = obj.name
        else:
            row.label(text=reason, icon="ERROR")
    if matching > len(rows):
        more = content.row(align=True)
        more.label(text=f"Showing {len(rows)} of {matching}")
        more.operator("fumaris.participant_list_more", text="Show More", icon="TRIA_DOWN")
    for shape, kind, source in (("mesh", "mesh", "Collection Mesh"), ("sphere", "sphere", "Collection Points")):
        if counts.get(shape, 0) >= _COLLECTION_HINT_FROM:
            hint = content.column(align=True)
            _wrapped(hint, f"{counts[shape]} separate {kind} emitters. If they can share settings, one "
                           f"{source} emitter for their collection simulates several times faster", "INFO")


def _is_particle_point_cloud(props):
    return (
        props.participant_type == "particles"
        and props.particle_subtype == "point_cloud"
    )


def _draw_mesh_emitter_options(box, props):
    box.prop(props, "mesh_emission_mode", text="Region")
    box.prop(props, "mesh_emission_distance")
    box.prop(props, "emitter_global_influence", slider=True)
    box.prop(props, "mesh_emission_mask_attribute", text="Mask")
    if props.mesh_emission_mask_attribute.strip():
        box.prop(props, "mesh_emission_mask_threshold")
    box.prop(props, "mesh_velocity_attribute", text="Velocity")
    _draw_velocity_attribute_problem(box, props)


def _draw_velocity_attribute_problem(box, props):
    """Warn under Velocity when the named attribute is missing, before Play
    fails on it. Reads the already evaluated scene (no evaluation)."""
    from .mesh_export import uses_velocity_attribute, velocity_attribute_problem
    if not uses_velocity_attribute(props):
        return
    obj = props.id_data
    try:
        evaluated = obj.evaluated_get(bpy.context.view_layer.depsgraph)
    except (AttributeError, ReferenceError, RuntimeError):
        return
    mesh = getattr(evaluated, "data", None)
    if obj.type != "MESH" or mesh is None or not hasattr(mesh, "attributes"):
        return
    problem = velocity_attribute_problem(obj, mesh, props)
    if problem:
        row = box.column(align=True)
        row.alert = True
        _wrapped(row, problem.replace(f" on {obj.name}", ""), icon="ERROR")


def _draw_attribute_mapping(box, props):
    column, opened = _nested_foldout(box, props, "show_attribute_settings", "Attribute Mapping")
    if not opened:
        return

    column.prop(props, "attr_position")
    column.prop(props, "attr_velocity")
    column.prop(props, "attr_radius")
    column.separator()
    column.prop(props, "attr_smoke")
    column.prop(props, "attr_temperature")
    column.prop(props, "attr_fuel")
    column.prop(props, "attr_burn")
    column.prop(props, "attr_divergence")
    column.separator()
    column.prop(props, "attr_smoke_coupling")
    column.prop(props, "attr_temperature_coupling")
    column.prop(props, "attr_velocity_coupling")
    column.prop(props, "attr_mask")


def _draw_emitter_shape(box, obj, props):
    box.label(text="Source Geometry", icon="MESH_DATA")
    box.prop(props, "participant_type", text="Shape")

    if props.participant_type == "sphere":
        box.prop(props, "emitter_radius")
    elif props.participant_type == "box":
        box.label(text="Uses this object's transform as a box", icon="CUBE")
    elif props.participant_type == "mesh":
        _draw_mesh_emitter_options(box, props)
    elif props.participant_type == "particles":
        box.prop_search(
            props,
            "particle_system_name",
            obj,
            "particle_systems",
            icon="PARTICLE_DATA",
            text="System",
        )
        box.prop(props, "particle_subtype", text="Type")
        if props.particle_subtype == "point_cloud":
            box.prop(props, "point_radius")
        elif props.particle_subtype == "mesh":
            _draw_mesh_emitter_options(box, props)
            _draw_small_instance_warning(box, obj, props)
    elif props.participant_type == "geometry_nodes":
        box.prop(props, "gn_subtype", text="Type")
        if props.gn_subtype == "point_cloud":
            box.prop(props, "point_radius")
            _draw_attribute_mapping(box, props)
        elif props.gn_subtype == "mesh":
            _draw_mesh_emitter_options(box, props)
        elif props.gn_subtype == "volume":
            box.label(text="Uses evaluated Geometry Nodes volume grids")
    elif props.participant_type == "openvdb":
        # Short labels: Blender gives a text field's label 40% of a narrow
        # panel, which cut "OpenVDB File" to "OpenV..." (2026-11-14 UX test).
        box.prop(props, "volume_filepath", text="File")
        row = box.row(align=True)
        row.prop(props, "volume_use_sequence")
        sub = row.row(align=True)
        sub.enabled = props.volume_use_sequence
        sub.prop(props, "volume_frame_offset", text="Offset")
        box.prop(props, "volume_use_velocity")
        grids, grids_open = _nested_foldout(box, props, "show_volume_grids", "Grid Names")
        if grids_open:
            grids.prop(props, "volume_grid_density", text="Smoke")
            grids.prop(props, "volume_grid_temperature", text="Temperature")
            grids.prop(props, "volume_grid_fuel", text="Fuel")
            grids.prop(props, "volume_grid_burn", text="Burn")
            grids.prop(props, "volume_grid_velocity", text="Velocity")
    elif props.participant_type == "collection_points":
        box.prop(props, "source_collection", text="Source")
        box.prop(props, "collection_point_center", text="Center")
        box.prop(props, "collection_radius_mode", text="Radius")
        if props.collection_radius_mode == "bounds":
            box.prop(props, "collection_radius_scale")
        else:
            box.prop(props, "point_radius", text="Radius")
        box.label(text="One sphere per mesh")
        box.label(text="Automatic motion velocity")
        box.label(text="Source meshes need no Fumaris role")
    elif props.participant_type == "collection_mesh":
        box.prop(props, "source_collection", text="Source")
        _draw_mesh_emitter_options(box, props)
        box.label(text="Combined geometry, shared settings")
        box.label(text="Source meshes need no Fumaris role")

    mesh_options = (props.participant_type in {"mesh", "collection_mesh"} or
                    (props.participant_type == "particles" and props.particle_subtype == "mesh") or
                    (props.participant_type == "geometry_nodes" and props.gn_subtype == "mesh"))
    if not mesh_options:
        box.prop(props, "emitter_global_influence", slider=True)


def _hot_fuel_without_air(obj, props):
    # With an Oxygen Limit and no Oxygen Field the air left is judged from
    # temperature, so fuel injected at Flame Temperature counts as burnt gas
    # and cannot burn until it mixes.
    if props.emitter_fuel <= 0.0 or props.emitter_temperature < 1.0:
        return False
    for domain in bpy.data.objects:
        settings = getattr(domain, "fumaris", None)
        if (settings is None or settings.smoke_object_type != "domain"
                or settings.oxygen_limit <= 0.0 or settings.oxygen_field):
            continue
        collection = settings.emitter_collection
        if collection is not None and obj.name in collection.all_objects:
            return True
    return False


def _emitter_below_ignition(obj, props):
    """(emitted K, ignition K) when this fuel emitter's Temperature stays under
    the Ignition Temperature of a Realistic domain it emits into, else None.
    Emitter Temperature 1 is that domain's Flame Temperature."""
    if props.emitter_fuel <= 0.0 or props.emitter_temperature <= 0.0:
        return None
    for domain in bpy.data.objects:
        settings = getattr(domain, "fumaris", None)
        if settings is None or settings.smoke_object_type != "domain":
            continue
        collection = settings.emitter_collection
        if collection is None or obj.name not in collection.all_objects:
            continue
        kelvin = props.emitter_temperature * settings.flame_temperature
        if kelvin < settings.ignition_temperature_k:
            return kelvin, settings.ignition_temperature_k
    return None


def _draw_emitter(layout, obj, props):
    box = layout.box()
    if _section(box, props, "show_panel_emitter", "Emitter", "OUTLINER_OB_FORCE_FIELD"):
        box.prop(props, "participant_enabled")
        _draw_emitter_shape(box, obj, props)

    box = layout.box()
    if _section(box, props, "show_panel_emitted_channels", "Emitted Channels", "MOD_FLUID"):
        smoke_row = box.row(align=True)
        smoke_row.active = not props.emitter_influence
        smoke_row.prop(props, "emitter_smoke")
        # Its own smoke color: the swatch while a domain of it has Per-Emitter
        # Smoke Color on; otherwise a button that offers to turn it on (with its
        # cost) or to set the domain's Color, so no one pays for it unawares.
        domains = emitter_domains(obj)
        # (narrow, so the Smoke label keeps its room)
        color = smoke_row.row(align=True)
        color.ui_units_x = 1.6
        if any(domain.fumaris.color_mixing for domain in domains):
            color.prop(props, "emitter_color", text="")
        elif domains:
            color.operator("fumaris.emitter_color_offer", text="", icon="COLOR")
        box.prop(props, "emitter_temperature")
        box.prop(props, "emitter_fuel")
        if _hot_fuel_without_air(obj, props):
            hint = box.column(align=True)
            hint.label(text="Fuel enters at Flame Temperature, which the", icon="INFO")
            hint.label(text="Oxygen Limit reads as burnt gas: it waits to mix.")
            hint.label(text="Use Temperature about 0.35, or the Oxygen Field")
        cold = _emitter_below_ignition(obj, props)
        if cold is not None:
            hint = box.column(align=True)
            hint.alert = True
            hint.label(text=f"Fuel won't light: {cold[0]:.0f} K is below", icon="ERROR")
            hint.label(text=f"Ignition Temperature ({cold[1]:.0f} K). Temperature is")
            hint.label(text="a share of Flame Temperature; raise it")
        box.prop(props, "emitter_burn")
        box.prop(props, "emitter_divergence")

    box = layout.box()
    if _section(box, props, "show_panel_emitter_velocity", "Velocity", "FORCE_FORCE"):
        box.prop(props, "velocity")
        if props.participant_type != "openvdb":
            box.prop(props, "normal_velocity")
        box.prop(props, "motion_velocity_scale")

    box = layout.box()
    # A box of its own: the arrow at the box edge, like the domain's Advanced.
    opened = _section(box, props, "show_emitter_coupling", "Emission Blending", "MOD_DATA_TRANSFER")
    if opened:
        column = box.column(align=True)
        # The rate gets the room: its label is the channel's name.
        for channel in ("smoke", "temperature", "fuel", "burn", "velocity", "divergence"):
            row = column.split(factor=0.64 if _NARROW else 0.6, align=True)
            row.prop(props, f"couple_rate_{channel}")
            row.prop(props, f"source_op_{channel}", text="")

    box = layout.box()
    opened = _section(box, props, "show_emitter_advanced", "Advanced", "PREFERENCES")
    if opened:
        column = box.column(align=True)
        column.prop(props, "emitter_influence")
        column.prop(props, "emitter_burning")
        if props.emitter_burning:
            column.prop(props, "emitter_burn_rate")
        column.separator()
        if not (
            props.participant_type in {"box", "openvdb"}
            or (
                props.participant_type == "geometry_nodes"
                and props.gn_subtype == "volume"
            )
        ):
            column.prop(props, "motion_substeps")
        column.prop(props, "emitter_apply_post_pressure")
        if props.participant_type == "sphere":
            column.prop(props, "sphere_multisample")
            if props.sphere_multisample:
                column.prop(props, "sphere_trace_samples")
        if _is_particle_point_cloud(props):
            column.prop(props, "point_enable_interpolation")


def _draw_collider(layout, props):
    box = layout.box()
    if _section(box, props, "show_panel_collider", "Collider", "MOD_PHYSICS"):
        box.prop(props, "participant_enabled")
        box.prop(props, "collider_type", text="Shape")
        if props.collider_type in {"mesh", "box"}:
            box.prop(props, "collider_margin")
        elif props.collider_type == "sphere":
            box.prop(props, "collider_radius")
        box.prop(props, "collider_solid")
        box.prop(props, "collider_velocity_influence")


def _draw_effector(layout, props):
    box = layout.box()
    if _section(box, props, "show_panel_effector", "Effector", "FORCE_FORCE"):
        box.prop(props, "participant_enabled")
        box.prop(props, "effector_type", text="Type")
        strength_label = "Swirl" if props.effector_type == "vortex" else "Strength"
        box.prop(props, "effector_strength", text=strength_label)
        box.prop(props, "effector_radius")
        if props.effector_type == "vortex":
            box.prop(props, "effector_vortex_height")
            box.prop(props, "effector_vortex_core_radius")
            box.prop(props, "effector_vortex_inflow")
            box.prop(props, "effector_vortex_lift")
        box.prop(props, "effector_coupling")
        box.prop(props, "effector_samples")

        if props.effector_type in {"force", "wind", "vortex", "turbulence"}:
            noise, noise_open = _nested_foldout(box, props, "show_panel_noise", "Noise")
            if noise_open:
                if props.effector_type != "turbulence":
                    noise.prop(props, "effector_noise_amount")
                noise.prop(props, "effector_noise_size")
                noise.prop(props, "effector_noise_w")
                noise.prop(props, "effector_noise_seed")

        mask, mask_open = _nested_foldout(box, props, "show_panel_mask", "Field Mask")
        if mask_open:
            mask.prop(props, "effector_mask_field")
            if props.effector_mask_field != "none":
                row = mask.row(align=True)
                row.prop(props, "effector_mask_min")
                row.prop(props, "effector_mask_max")
                mask.prop(props, "effector_mask_invert")

        falloff, falloff_open = _nested_foldout(box, props, "show_panel_falloff", "Falloff")
        if falloff_open:
            if props.effector_type == "vortex":
                falloff.prop(props, "effector_z_direction")
                falloff.prop(props, "effector_falloff_power", text="Edge Power")
            else:
                falloff.prop(props, "effector_z_direction")
                falloff.prop(props, "effector_falloff_power")
                row = falloff.row(align=True)
                row.prop(props, "effector_use_min_distance", text="")
                sub = row.row(align=True)
                sub.enabled = props.effector_use_min_distance
                sub.prop(props, "effector_min_distance")
                row = falloff.row(align=True)
                row.prop(props, "effector_use_max_distance", text="")
                sub = row.row(align=True)
                sub.enabled = props.effector_use_max_distance
                sub.prop(props, "effector_max_distance")


def _draw_outflow(layout, props):
    box = layout.box()
    if _section(box, props, "show_panel_outflow", "Outflow", "MOD_FLUID"):
        box.prop(props, "participant_enabled")
        box.prop(props, "mesh_emission_mode", text="Region")
        box.prop(props, "mesh_emission_distance")
        box.prop(props, "mesh_emission_mask_attribute", text="Mask")
        if props.mesh_emission_mask_attribute.strip():
            box.prop(props, "mesh_emission_mask_threshold")
        box.prop(props, "outflow_coupling")
        box.prop(props, "motion_velocity_scale", text="Motion Velocity")


def _compact_time(seconds):
    seconds = float(seconds)
    if seconds <= 0.0:
        return "N/A"
    if seconds < 60.0:
        return f"{seconds:.1f}s"
    minutes, seconds = divmod(int(round(seconds)), 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _compact_memory(mebibytes):
    mebibytes = float(mebibytes)
    if mebibytes <= 0.0:
        return "N/A"
    if mebibytes >= 1024.0:
        return f"{mebibytes / 1024.0:.1f}G"
    return f"{mebibytes:.0f}M"


def _bake_summary(props, state):
    """The parts of the bake's summary line; figures not measured are left
    out (a queued bake dropped before it ran read "Stopped: 0 frames | N/A")."""
    frames = int(props.baked_frames)
    if frames <= 0:
        return ["Not baked" if state == "Baked" else "Stopped before the first frame"]
    parts = [f"{state}: {frames} frame" + ("" if frames == 1 else "s")]
    if props.bake_elapsed > 0.0:
        parts.append(_compact_time(props.bake_elapsed))
    if props.bake_peak_vram_mb > 0.0:
        parts.append(f"VRAM {_compact_memory(props.bake_peak_vram_mb)}")
    if props.bake_peak_ram_mb > 0.0:
        parts.append(f"RAM {_compact_memory(props.bake_peak_ram_mb)}")
    return parts


def _wrapped_parts(layout, parts, icon="NONE", separator=" | "):
    """Parts joined by a separator, wrapped between parts so no line ends
    with a separator (a wrapped summary read "Baked: 1 frames | 1.0s |")."""
    width = max(16, _LINE_CHARS)
    lines = []
    for part in (part for part in parts if part):
        if lines and len(lines[-1]) + len(separator) + len(part) <= width:
            lines[-1] += separator + part
        else:
            lines.append(part)
    for index, line in enumerate(lines or [""]):
        layout.label(text=line, icon=icon if index == 0 else ("BLANK1" if icon != "NONE" else "NONE"))


def _compact_count(value):
    for scale, suffix in ((1e9, " B"), (1e6, " M"), (1e3, " k")):
        if value >= scale:
            return f"{value / scale:.3g}{suffix}"
    return f"{int(value)}"


def _draw_diagnostics(layout, props):
    state = diagnostics.snapshot(props.id_data)
    # Use the panel background, darker than the surrounding raised boxes in
    # Blender's dark theme. Do not change the user's global theme colors.
    layout.separator()
    box = layout.column()
    opened = _section(box, props, "show_diagnostics", "Diagnostics", "SYSTEM")
    if not opened:
        error = diagnostics.error_summary(state)
        if error:
            box.label(text=error, icon="ERROR")
        return
    if state:
        metrics = state["metrics"]
        number = lambda name: diagnostics.value(state, name)
        name = metrics.get("flow_device_name")
        if name:
            box.label(text=str(name), icon="GRAPH")
        label = state["status"] if state["active"] else f"Last run: {state['status']}"
        box.label(text=label)
        if "flow_device_memory_bytes" in metrics:
            box.label(text="Flow GPU: " + _compact_memory(number("flow_device_memory_bytes") / 1048576))
        capacity = number("flow_active_block_capacity")
        if capacity:
            box.label(text=f"Sparse blocks: {int(number('flow_active_blocks')):,} / {int(capacity):,}")
        fine_capacity = number("flow_fine_block_capacity")
        if fine_capacity and fine_capacity < capacity:
            # air blocks keep only velocity; the rest have smoke storage, a pool of its own
            box.label(text=f"With smoke storage: {int(number('flow_fine_blocks')):,} / {int(fine_capacity):,}")
        cells = number("flow_active_cells")
        if cells:
            box.label(text=f"Voxels: {_compact_count(cells)}")
        if opened:
            box.label(text=f"Effective resolution: {number('flow_effective_resolution'):.0f} / {number('flow_requested_resolution'):.0f}")
            box.label(text=f"CFL (previous field): {number('flow_previous_courant'):.2f} cells/step")
            box.label(text=f"Smoke CFL (drives Adaptive): {number('flow_previous_smoke_courant'):.2f} cells/step")
            box.label(text=f"Voxel size: {number('flow_density_voxel_size_x'):.5g} m")
            box.label(text=f"Frame latency: {number('blender_round_trip_ms'):.1f} ms")
            box.label(text=f"Flow submit / wait: {number('flow_submit_ms'):.1f} / {number('flow_wait_ms'):.1f} ms")
            box.label(text=f"Last texture upload: {number('blender_texture_upload_ms'):.2f} ms")
            box.label(text="Bridge RAM: " + _compact_memory(number("bridge_rss_bytes") / 1048576))
            box.label(text="Blender RAM: " + _compact_memory(number("blender_rss_bytes") / 1048576))
            transfer = number("flow_upload_memory_bytes") + number("flow_readback_memory_bytes")
            box.label(text="Flow transfer buffers: " + _compact_memory(transfer / 1048576))
            box.label(text="Flow allocations, not total/free VRAM", icon="INFO")
        for warning in diagnostics.warnings(state)[-2:]:
            for i, line in enumerate(textwrap.wrap(warning, width=48)):
                box.label(text=line, icon="ERROR" if i == 0 else "NONE")
    elif opened:
        box.label(text="Start a bake or preview to collect metrics")
    if opened:
        box.operator("fumaris.copy_diagnostics", icon="COPYDOWN")


def _draw_memory_usage(layout, domain):
    state = diagnostics.snapshot(domain)
    usage = diagnostics.memory_usage(state)
    if not usage:
        return
    used, allocated = usage
    fraction = used / allocated
    gib = 1073741824.0
    # Show the VRAM the session reserved (the setting it started with, or the
    # Auto amount), not only Flow's own allocations, so the total matches the
    # VRAM field; the fill is the share of the smoke pool in use.
    started = state.get("vram_setting")
    if isinstance(started, float):
        allocated = started * gib
    elif started == "auto":
        # Auto's amount, as the Auto line shows it (Flow's own allocation can
        # differ from it, and read as a second, larger VRAM figure).
        from .memory_auto import budget_bytes_for_display
        auto = diagnostics.value(state, "flow_auto_memory_bytes")
        if auto <= 0 and getattr(domain, "fumaris", None) is not None and domain.fumaris.simulation_memory_auto:
            auto = budget_bytes_for_display(domain.fumaris, state)[0] or 0
        if auto > 0:
            allocated = auto
    used = fraction * allocated
    # GPU memory: the simulation's reserved pool and the share smoke fills.
    if _NARROW:
        # (the whole line no longer fits: the bar cut it mid-text; it still
        # says VRAM, so it is not read as the preview cache's RAM below).
        # The amount held comes first: "VRAM 0.0/40.0 GB" read as an empty
        # GPU while the reserved pool filled the card (beta 3 review).
        text = f"VRAM {allocated / gib:.1f} GB, {fraction:.0%} used"
    else:
        # "reserved": the total is the pool Flow holds, the fill what smoke
        # uses of it (0.0 of 1.0 GB read as wrong while Flow held 823 MB).
        text = f"VRAM in use: {used / gib:.1f} of {allocated / gib:.1f} GB reserved ({fraction:.0%})"
    if not state.get("active"):
        text = ("Last run: " if _NARROW else "Last run · ") + text
    elif diagnostics.vram_setting_changed(domain, state):
        text += " · VRAM changes on restart"
    try:
        layout.progress(factor=fraction, type="BAR", text=text)
    except (AttributeError, TypeError):
        layout.label(text=text, icon="MEMORY")


def _draw_capacity_warning(layout, domain):
    state = diagnostics.snapshot(domain)
    if diagnostics.memory_spill_warning(state):
        box = layout.column(align=True)
        title = box.row(align=True)
        title.alert = True
        title.label(text="GPU memory full", icon="ERROR")
        _wrapped(box, "The simulation runs slowly. " + diagnostics.memory_full_advice(state))
    status = diagnostics.capacity_status(state)
    if not status:
        return
    box = layout.column(align=True)
    title = box.row(align=True)
    title.alert = True
    # Short lines: "Capacity reached — smoke may clip" was cut off at the
    # default Properties width (beta 3 review).
    frame = (state or {}).get("capacity_reached_frame") if status == "full" else None
    title.label(text=(f"Capacity reached at frame {frame}" if frame is not None else "Capacity reached")
                if status == "full" else "Capacity almost full", icon="ERROR")
    if state and state.get("source_too_large"):
        # R35: more VRAM does not help a source thousands of times the pool.
        box.label(text="A source is too large for this")
        box.label(text="Resolution: lower Resolution.")
    else:
        box.label(text="Smoke may clip: increase VRAM")
        box.label(text="or lower Resolution, then restart.")
    if status == "full" and state and not state.get("active"):
        box.label(text="Last run", icon="INFO")


def _draw_flame_ramp(layout, props):
    """Color: Ramp. The ramp is a node of the domain's generated material (both
    looks read it); colour left to right over 0.45-1 x Full Glow."""
    from . import importers
    domain = props.id_data
    material = importers.synced_material(domain, create=False) if domain is not None else None
    node = material.node_tree.nodes.get(importers.NODE_RAMP) if material is not None and material.node_tree else None
    if node is None:
        if material is None:
            layout.operator("fumaris.edit_flame_ramp", icon="COLOR").domain_name = domain.name
        else:
            _wrapped(layout, "This domain's own Volume Material has no Fumaris ramp", "INFO")
        return
    layout.template_color_ramp(node, "color_ramp", expand=True)


def _draw_fire_look(fire, props):
    """The chosen Look's own controls: Temperature and Pyro keep separate values."""
    pyro = props.shader_look == "PYRO"
    key = {name: keys[1 if pyro else 0] for name, keys in FLAME_VALUE_KEYS.items()}
    fire.row(align=True).prop(props, "pyro_color" if pyro else "shader_color", expand=True)
    if (props.pyro_color if pyro else props.shader_color) == "GOLDEN":
        _draw_flame_ramp(fire, props)
    exposure = _pair(fire)
    exposure.prop(props, key["brightness"])
    exposure.prop(props, key["glow_temperature"])
    shape = _pair(fire)
    shape.prop(props, key["contrast"])
    spread = shape.row(align=True)
    spread.active = pyro or props.shader_color == "BLACKBODY"
    if pyro:
        thin = spread.row(align=True)
        thin.prop(props, "shader_thin_gas_ratio" if props.shader_thin_gas_auto else "shader_thin_gas")
        thin.prop(props, "shader_thin_gas_auto", text="", icon="AUTO")
    else:
        spread.prop(props, "shader_color_spread")
    color = _pair(fire)
    color.prop(props, key["temperature_multiplier"])
    color.prop(props, key["flame_core"], text="Blue Core")
    tint = _label_split(fire, 0.25, 0.35)
    tint.label(text="Flame Tint")
    tint.prop(props, key["flame_tint"], text="")
    fire.separator(factor=0.5)
    fire.prop(props, key["scatter_glow"])
    glow = _pair(fire)
    glow.enabled = getattr(props, key["scatter_glow"]) > 0.0
    glow.prop(props, key["glow_spread"], slider=True)
    glow.prop(props, key["glow_shadowing"], text="Shadowing")


def _draw_preview_cache_status(layout, scene, job, paused):
    """Preview cache: what can be scrubbed, what is shown, an export's result."""
    from .compare import preview_cache
    cache = preview_cache(job)
    if getattr(job, "_export_pending", None):
        layout.label(text="Saving the version…", icon="TIME")
    report = getattr(job, "_export_report", None)
    error = getattr(job, "_export_error", None)
    if error and not getattr(job, "_export_pending", None):
        row = layout.column(align=True)
        row.alert = True
        _wrapped(row, f"Could not save: {error}", "ERROR")
    elif report and not getattr(job, "_export_pending", None):
        frames, slot, first, last = report
        span = f"frame {first}" if first == last else f"frames {first}–{last}"
        _wrapped(layout, f"Saved {span} as '{slot}'", "CHECKMARK")
        if getattr(job, "_export_warning", None):
            _wrapped(layout, job._export_warning, "INFO")
    if not cache.get("frames"):
        return
    first, last = cache["first"], cache["last"]

    def size_text(size):
        # Binary GB, as Preferences' Preview Cache RAM and the bridge's budget
        # count them (1e9 showed a 16 GB budget as "17.2 GB", beta 3 review).
        return f"{size / (1 << 30):.1f} GB" if size >= (1 << 30) else f"{size / (1 << 20):.0f} MB"

    live = getattr(job, "_live_frame", None)
    if paused and live is not None and first <= scene.frame_current < live:
        shown = cache.get("shown", -1)
        _wrapped(layout, f"Showing cached frame {shown if shown >= 0 else scene.frame_current} (live {live})",
                 "SEQUENCE")
    elif paused and live is not None and scene.frame_current < first:
        _wrapped(layout, f"Frame {scene.frame_current} is no longer cached (oldest kept: {first})", "INFO")
    elif paused and getattr(job, "_loop_reset_pending", False):
        _wrapped(layout, "At the end: Resume starts over", "INFO")
    frames = cache["frames"]
    every = "" if frames >= last - first + 1 else f" ({frames} frames)"
    # The cache lives in system RAM, not on the GPU: its figures say so (a
    # tester read "8.6 GB" beside the VRAM bar as GPU memory, 2026-11-19).
    _wrapped(layout, f"Preview cache: frames {first}–{last}{every} · {size_text(cache.get('bytes', 0))} of "
                     f"{size_text(cache.get('budget', 0))} RAM", "DISK_DRIVE")
    # Out of cache RAM: not an error (the simulation runs on), so no red;
    # which frames are gone and what Save Sequence keeps is the one warning.
    start = None
    domain = bpy.data.objects.get(getattr(job, "_domain_name", "") or "")
    if domain is not None:
        from .utils import simulation_frame_range
        start = simulation_frame_range(domain)[0]
    lost = start is not None and first > start
    if cache.get("low_memory") or cache.get("dropped") or lost:
        box = layout.box().column(align=True)
        if cache.get("low_memory"):
            _wrapped(box, "System RAM is low (other programs use it), so the preview cache keeps fewer frames. "
                          "The simulation is not affected.", "INFO")
        else:
            _wrapped(box, f"Preview cache RAM is full ({size_text(cache.get('budget', 0))}): the oldest frames "
                          "are dropped. The simulation is not affected.", "INFO")
        if lost:
            gone = f"frame {start}" if first - 1 == start else f"frames {start}–{first - 1}"
            _wrapped(box, f"Lost {gone}; Save Sequence keeps only {first}–{last}", "ERROR")
        _wrapped(box, "Keep more frames: Preferences > Preview Cache RAM, Cache Every 2+, lower Resolution, "
                      "or Bake (to disk, any length)", "INFO")


def _changed_count(props, names):
    """How many of these settings differ from their defaults (a folded group
    says so, so no changed value hides unnoticed)."""
    count = 0
    for name in names:
        prop = props.bl_rna.properties.get(name)
        if prop is None:
            continue
        value = getattr(props, name)
        default = prop.default_array if getattr(prop, "is_array", False) else prop.default
        if getattr(prop, "is_array", False):
            if any(abs(float(a) - float(b)) > 1e-6 for a, b in zip(value, default)):
                count += 1
        elif isinstance(value, float):
            if abs(value - float(default)) > 1e-6:
                count += 1
        elif value != default:
            count += 1
    return count


# More emitters than this and the panel does not compare their colours.
_COLOR_CHECK_LIMIT = 500


def _emitter_color_note(props):
    """None, or why Per-Emitter Smoke Color buys nothing in this domain: every
    enabled emitter has one (unanimated) colour, which the domain's Color
    gives without the second smoke field."""
    collection = props.emitter_collection
    if collection is None:
        return None
    colors = set()
    for index, obj in enumerate(collection.all_objects):
        if index >= _COLOR_CHECK_LIMIT:
            return None
        settings = getattr(obj, "fumaris", None)
        if settings is None or settings.smoke_object_type != "emitter" or not settings.participant_enabled:
            continue
        animation = obj.animation_data
        if animation is not None and (animation.drivers or animation.action is not None):
            # (a keyed or driven colour may change: no claim)
            if any(curve.data_path == "fumaris.emitter_color" for curve in animation.drivers):
                return None
            action = animation.action
            curves = getattr(action, "fcurves", None) if action is not None else None
            if curves is None or any(curve.data_path == "fumaris.emitter_color" for curve in curves):
                return None
        colors.add(tuple(round(float(c), 3) for c in settings.emitter_color))
        if len(colors) > 1:
            return None
    if len(colors) != 1:
        return None
    return ("All emitters have the same Smoke Color, so this changes nothing yet. Give the emitters "
            "different Smoke Colors (emitter > Emitted Channels), or turn this off and use Color above")


def _draw_emitter_colors(layout, props):
    """Per-Emitter Smoke Color, under the smoke Color: the switch, its cost while
    it is on, and a note when it buys nothing."""
    layout.prop(props, "color_mixing")
    if not props.color_mixing:
        return
    note = _emitter_color_note(props)
    if note:
        # (advice, not an error: the result is right, only slower)
        _wrapped(layout, note, "INFO")
    else:
        _wrapped(layout, "Colors the smoke, not the flame. Costs about 1.4x the simulation time and a third "
                         "of the blocks per GB", "INFO")


def _draw_small_instance_warning(layout, obj, props):
    """Blender scales each instance by the particle Size (0.05 by default), so
    instances often end up far smaller than a cell and emit almost nothing
    (S10, 2026-11-20: 1.5 cm spheres at 12 cm cells emitted 0.04% of the
    points' smoke)."""
    system = obj.particle_systems.get(props.particle_system_name) if obj else None
    settings = getattr(system, "settings", None)
    instance = getattr(settings, "instance_object", None)
    if settings is None or settings.render_type != "OBJECT" or instance is None:
        return
    size = max(instance.dimensions) * settings.particle_size
    domains = emitter_domains(obj)
    if not domains:
        return
    cell = 32.0 / max(1, max(int(d.fumaris.resolution) for d in domains))
    if size < 2.0 * cell:
        layout.label(text=f"Instances are {size * 100:.3g} cm, under 2 cells: raise the particle Size",
                     icon="ERROR")


def emitter_domains(obj, scene=None):
    """The scene's domains whose Emitters collection holds this object."""
    scene = scene or bpy.context.scene
    domains = []
    for candidate in scene.objects:
        settings = getattr(candidate, "fumaris", None)
        if settings is None or settings.smoke_object_type != "domain":
            continue
        collection = settings.emitter_collection
        if collection is not None and obj.name in collection.all_objects:
            domains.append(candidate)
    return domains


class FUMARIS_MT_bake_more(bpy.types.Menu):
    """Beside Play and Bake: the less common bake actions."""
    bl_label = "Bake"
    bl_idname = "FUMARIS_MT_bake_more"

    def draw(self, context):
        layout = self.layout
        obj = context.object
        protected = obj is not None and is_protected(peek_output_directory_for_object(obj))
        row = layout.row()
        row.enabled = not protected
        row.operator("fumaris.bake_background", text="Bake in Background", icon="RENDERLAYERS")
        layout.operator("fumaris.playblast", text="Save Playblast", icon="FILE_MOVIE")
        layout.separator()
        row = layout.row()
        row.enabled = not protected
        row.operator("fumaris.delete", text="Delete Bake", icon="TRASH")


class FUMARIS_OT_emitter_color_offer(bpy.types.Operator):
    bl_idname = "fumaris.emitter_color_offer"
    bl_label = "Own Smoke Color"
    bl_description = ("Give this emitter's smoke its own color (the smoke, not the flame). That needs the "
                      "domain's Per-Emitter Smoke Color (about 1.4x the simulation time and a third of the "
                      "blocks per GB); for one color everywhere, set the domain's Color instead")

    def invoke(self, context, _event):
        obj = context.object
        domains = emitter_domains(obj, context.scene) if obj is not None else []
        if not domains:
            self.report({"INFO"}, "This emitter is in no domain's Emitters collection")
            return {"CANCELLED"}

        def draw(menu, _context):
            layout = menu.layout
            layout.label(text="Per-Emitter Smoke Color: about 1.4x the simulation time", icon="INFO")
            layout.label(text="and a third of the blocks per GB.", icon="BLANK1")
            layout.separator()
            for domain in domains:
                on = layout.operator("fumaris.color_mixing_enable", text=f"Turn On in {domain.name}", icon="COLOR")
                on.domain = domain.name
            layout.separator()
            for domain in domains:
                look = layout.operator("fumaris.domain_show_look", text=f"Set {domain.name}'s Color Instead",
                                       icon="SHADING_RENDERED")
                look.domain = domain.name
        context.window_manager.popup_menu(draw, title="Own Smoke Color", icon="COLOR")
        return {"FINISHED"}

    def execute(self, context):
        return self.invoke(context, None)


class FUMARIS_OT_color_mixing_enable(bpy.types.Operator):
    bl_idname = "fumaris.color_mixing_enable"
    bl_label = "Turn On Per-Emitter Smoke Color"
    bl_description = ("Turn on the domain's Per-Emitter Smoke Color, so each emitter's smoke keeps its own color "
                      "(about 1.4x the simulation time and a third of the blocks per GB)")
    bl_options = {"REGISTER", "UNDO"}

    domain: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    @classmethod
    def poll(cls, _context):
        if active_mode() == "baking":
            cls.poll_message_set("Stop the bake first")
            return False
        return True

    def execute(self, context):
        domain = bpy.data.objects.get(self.domain)
        if domain is None or domain.fumaris.smoke_object_type != "domain":
            self.report({"WARNING"}, f"No domain named {self.domain}")
            return {"CANCELLED"}
        domain.fumaris.color_mixing = True
        self.report({"INFO"}, f"Per-Emitter Smoke Color is on in {domain.name}: set this emitter's Smoke Color")
        return {"FINISHED"}


class FUMARIS_OT_domain_show_look(bpy.types.Operator):
    bl_idname = "fumaris.domain_show_look"
    bl_label = "Show the Domain's Look"
    bl_description = "Make the domain the active object and open its Look section, where Color sets the smoke's color"

    domain: bpy.props.StringProperty(options={"HIDDEN", "SKIP_SAVE"})

    def execute(self, context):
        domain = bpy.data.objects.get(self.domain)
        if domain is None:
            return {"CANCELLED"}
        domain.fumaris.show_appearance = True
        if context.mode == "OBJECT" and domain.name in context.view_layer.objects:
            for other in context.selected_objects:
                other.select_set(False)
            domain.select_set(True)
            context.view_layer.objects.active = domain
        return {"FINISHED"}


def _draw_domain(layout, scene, props):
    job = active_job()
    mode = active_mode()
    is_baking = mode == "baking"
    is_previewing = mode == "previewing"
    is_preview_paused = is_previewing and bool(
        getattr(job, "_pause_requested", False)
    )
    is_initializing = job is not None and not getattr(job, "_accepted", False)
    domain = props.id_data
    slot_directory = peek_output_directory_for_object(domain)
    slot_protected = is_protected(slot_directory)
    # This domain's background bake (others, and the preview, stay usable).
    background = background_job(domain)

    simulate_box = layout.box()
    if _section(simulate_box, props, "show_panel_simulate", "Simulate", "PHYSICS"):
        controls = simulate_box.column(align=True)
        # Tight: words without icons, so no word is cut off.
        icon = (lambda name: "NONE") if _TIGHT else (lambda name: name)
        # One row for what runs: Play and Bake when idle; while something
        # runs, its own buttons and one Stop for it. The menu at the end
        # holds Bake in Background and Delete Bake.
        row = controls.row(align=True)
        if background is not None:
            from .background import stop_label
            sub = row.row(align=True)
            # (Ended and waiting to be imported: nothing left to stop.)
            sub.enabled = background.state != "ended"
            sub.operator("fumaris.bake_background_stop", icon=icon("CANCEL"), text=stop_label(background))
        elif is_baking:
            row.operator("fumaris.stop", icon=icon("CANCEL"), text="Stop Bake")
        elif is_previewing:
            play = row.row(align=True)
            play.enabled = not is_initializing
            play.operator(
                "fumaris.preview_pause",
                icon=icon("PLAY" if is_preview_paused else "PAUSE"),
                text="Resume" if is_preview_paused else "Pause",
            )
            row.operator("fumaris.preview_stop", icon=icon("CANCEL"), text="Stop")
            restart = row.row(align=True)
            restart.enabled = not is_initializing
            restart.operator("fumaris.preview_restart", icon="FILE_REFRESH", text="")
        else:
            play = row.row(align=True)
            play.enabled = mode is None
            play.operator("fumaris.preview_play", icon=icon("PLAY"), text="Play")
            bake = row.row(align=True)
            bake.enabled = mode is None and not slot_protected
            bake.operator("fumaris.bake", icon=icon("RENDER_ANIMATION"), text="Bake")
        row.menu("FUMARIS_MT_bake_more", text="", icon="DOWNARROW_HLT")
        if slot_protected:
            controls.label(text=f"Version '{props.cache_slot}' is protected", icon="LOCKED")
        controls.separator(factor=0.5)
        cache_preview = controls.row(align=True)
        cache_preview.enabled = mode is None
        cache_preview.prop(props, "preview_cache")
        if props.preview_cache:
            # Beside the checkbox; tight, on its own line so neither is cut off.
            step = controls.row(align=True) if _TIGHT else cache_preview
            step.enabled = mode is None
            step.prop(props, "preview_cache_step", text="")
        bake_preview = controls.row(align=True)
        bake_preview.enabled = mode is None
        bake_preview.prop(props, "preview_bake")
        # While this domain previews: a cached preview can be kept as a
        # version (Sequence and Frame wait for a pause), and the viewport
        # pictured. Stopped, the buttons are hidden.
        previewing_here = (is_previewing and job is not None
                           and getattr(job, "_domain_name", None) == props.id_data.name)
        cached = previewing_here and job._worker._session.get("preview_cache")
        if cached:
            # "Save" is dropped when narrow, so the three names stay whole;
            # tight, they stack with their full names.
            word = "Save " if (_TIGHT or not _NARROW) else ""
            save = controls.column(align=True) if _TIGHT else controls.row(align=True)
            save.operator("fumaris.version_save", text=word + "Sequence", icon="SEQUENCE").frame_only = False
            save.operator("fumaris.version_save", text=word + "Frame", icon="IMAGE_PLANE").frame_only = True
            save.operator("fumaris.snapshot", text=word + "Snapshot", icon="RESTRICT_RENDER_OFF")
        elif previewing_here:
            controls.operator("fumaris.snapshot", text="Save Snapshot", icon="RESTRICT_RENDER_OFF")
        controls.separator(factor=0.5)
        # Keep transport rows together; all status text lives below their options.
        status = controls.column(align=True)
        failure = diagnostics.error_summary(diagnostics.snapshot(props.id_data))
        if background is not None:
            from .background import draw_status
            draw_status(status, background)
        elif failure:
            error_row = status.row(align=True)
            error_row.alert = True
            _wrapped(error_row.column(align=True), failure, "ERROR")
            _wrapped(status, "See Diagnostics below for details.")
        elif is_initializing:
            status.label(text="Initializing Flow GPU…", icon="INFO")
            _wrapped(status, "First-run shaders can take several minutes")
        elif is_baking and writing_last_frames(job):
            # Every frame is simulated; the files are still being written
            # (minutes on a slow disk): not stuck, not "0 s left".
            status.label(text=WRITING_LAST_FRAMES, icon="TIME")
        elif is_baking:
            status.label(text="Baking…", icon="TIME")
        elif mode is None and props.simulation_state in {"baked", "stopped"}:
            completed = props.simulation_state == "baked"
            _wrapped_parts(status, _bake_summary(props, "Baked" if completed else "Stopped"),
                           "CHECKMARK" if completed else "PAUSE")
        elif is_previewing:
            replaying = getattr(job, "_replaying", False)
            if replaying:
                status.label(text="Replaying the cache · Paused" if is_preview_paused else "Replaying the cache",
                             icon="PAUSE" if is_preview_paused else "LOOP_FORWARDS")
            else:
                status.label(text="Live preview · Paused" if is_preview_paused else "Live preview",
                             icon="PAUSE" if is_preview_paused else "PLAY")
            # A run changed mid-pass shows on the Timeline (its frames turn
            # stale), so the panel says nothing more.
            stale = getattr(job, "_pass_stale_frame", None)
            if stale is None and job._worker._session.get("preview_cache"):
                from .operators import stale_frames
                behind = len(stale_frames(job, props.id_data))
                if behind:
                    _wrapped(status, f"{behind} cached frames are from earlier settings (orange on the Timeline); "
                                     "this pass replaces them", "INFO")
            if (is_preview_paused and getattr(job, "_loop_reset_pending", False)
                    and not job._worker._session.get("preview_cache")):
                # (A cached preview says so with its cache status.)
                _wrapped(status, "At the end: Resume starts over", "INFO")
            _draw_preview_cache_status(status, scene, job, is_preview_paused)
        if not domain.visible_get(view_layer=bpy.context.view_layer):
            _wrapped(status, "Domain hidden: the simulation still runs; unhide it to see the preview", "INFO")

        _draw_capacity_warning(status, domain)
        _draw_memory_usage(status, domain)

        settings = controls.column(align=True)
        settings.enabled = not is_baking
        row = settings.row(align=True)
        row.prop(props, "use_scene_frame_range", text="", icon="TIME")
        row.prop(props, "preview_loop", text="", icon="LOOP_FORWARDS")
        if props.use_scene_frame_range:
            row.prop(props, "warmup_frames")
        else:
            row.prop(props, "sim_start_frame", text="Start")
            row.prop(props, "sim_end_frame", text="End")
            if props.sim_end_frame < props.sim_start_frame:
                warning = settings.row()
                warning.alert = True
                warning.label(text="End is before Start: one frame runs", icon="ERROR")
        grid_row = settings.row(align=True)
        if _NARROW:
            settings.prop(props, "resolution")
            grid_row = settings.row(align=True)
        else:
            grid_row.prop(props, "resolution")
        memory_field = grid_row.row(align=True)
        memory_field.active = not props.simulation_memory_auto
        memory_field.prop(props, "simulation_memory", text="VRAM (GB)")
        auto = grid_row.row(align=True)
        auto.ui_units_x = 2.2
        auto.prop(props, "simulation_memory_auto", text="Auto", toggle=True)
        draw_budget_summary(settings, props, diagnostics.snapshot(domain))
        if props.full_resolution_velocity and not props.show_panel_advanced:
            # Advanced (where the forecast sits under the checkbox) is closed:
            # say here when the last run's peak would not fit (Auto keeps the
            # amount, the blocks cost 3-4x).
            if draw_full_resolution_forecast(settings, props, diagnostics.snapshot(domain),
                                             _fire_on_half_storage(props), alerts_only=True):
                settings.separator()
        settings.prop(props, "air_margin")
        substeps_row = _pair(settings)
        substeps_row.prop(props, "num_sub_steps", text="Max Sub-Steps" if props.adaptive_cfl > 0.0 else "Sub-Steps")
        # Cells per Step adds steps up to Max Sub-Steps: nothing to add at 1.
        cells = substeps_row.row(align=True)
        cells.active = props.num_sub_steps > 1
        cells.prop(props, "adaptive_cfl")

    # Versions right under Simulate: bakes, snapshots and playblasts are what a
    # shot goes back and forth between (Brent, 2026-11-19: it sat below Output).
    compare_box = layout.box()
    if _section(compare_box, props, "show_panel_compare", "Versions", "FILE_FOLDER"):
        from .compare import draw_compare
        draw_compare(compare_box, props.id_data, props)

    look_box = layout.box()
    look_box.enabled = not is_baking
    if _section(look_box, props, "show_appearance", "Look", "SHADING_RENDERED"):
        appearance = look_box.column(align=True)
        appearance.label(text="Smoke")
        smoke = appearance.column(align=True)
        smoke_row = _pair(smoke)
        smoke_row.prop(props, "shader_smoke_density")
        smoke_row.prop(props, "shader_anisotropy")
        color = _label_split(smoke, 0.2, 0.3)
        color.label(text="Color")
        color.prop(props, "shader_smoke_color", text="")
        if is_pyro_look(props):
            smoke.prop(props, "shader_hot_soot", slider=True)
        hot = _pair(smoke)
        hot.prop(props, "shader_hot_density")
        hot_range = hot.row(align=True)
        hot_range.active = props.shader_hot_density != 1.0
        hot_range.prop(props, "shader_hot_density_from", text="From")
        hot_range.prop(props, "shader_hot_density_to", text="To")
        _draw_emitter_colors(smoke, props)
        appearance.separator()
        flame_header = appearance.row(align=True)
        flame_header.prop(props, "shader_flame_enabled")
        if props.shader_flame_enabled:
            flame_header.prop(props, "shader_look", text="")
            _draw_fire_look(appearance.column(align=True), props)
        cycles = getattr(scene, "cycles", None)
        if scene.render.engine == "CYCLES" and cycles is not None and hasattr(cycles, "volume_biased"):
            # Cycles' volume method, as a switch: Biased (ray-marched) or
            # Unbiased. Which renders faster depends on the scene.
            volumes = _pair(appearance)
            volumes.label(text="Cycles Volumes")
            volumes.prop(cycles, "volume_biased", toggle=True,
                         text="Biased" if cycles.volume_biased else "Unbiased")
        if scene.render.engine == "CYCLES":
            appearance.prop(props, "shader_smooth_voxels")
        if props.volume_material is not None and not props.volume_material.get("fumaris_generated_material"):
            appearance.label(
                text="Custom material overrides this",
                icon="INFO",
            )
        appearance.separator()
        preview, preview_open = _nested_foldout(appearance, props, "show_preview_display", "Live Preview")
        if preview_open:
            preview.prop(props, "preview_field_view")
            if props.preview_field_view == "blocks":
                # Block outlines only: the other preview options would only clutter it.
                preview.prop(props, "preview_block_line_width")
            else:
                preview.label(text="Quality")
                quality = preview.column(align=True)
                sharpness = _pair(quality)
                sharpness.prop(props, "preview_resolution_percent", text="Simulation", slider=True)
                sharpness.prop(props, "preview_image_scale", text="Image", slider=True)
                steps = quality.row(align=True)
                steps.prop(props, "preview_step_size")
                steps.prop(props, "preview_all_viewports", toggle=True)
                rate = quality.row(align=True)
                rate.prop(props, "preview_frame_rate", **({"text": "FPS"} if _NARROW else {}))
                if props.preview_frame_rate == "CUSTOM":
                    rate.prop(props, "preview_custom_fps")

                # Speed, Expansion and Fuel draw a field as fire: lighting does not apply.
                if props.preview_field_view == "look":
                    preview.label(text="Lighting")
                    lighting = preview.column(align=True)
                    lighting.row(align=True).prop(props, "preview_lighting", expand=True)
                    if props.preview_lighting == "CUSTOM":
                        direction = _pair(lighting)
                        direction.prop(props, "preview_light_azimuth")
                        direction.prop(props, "preview_light_elevation")
                        custom = _pair(lighting)
                        custom.prop(props, "preview_light_strength")
                        custom.prop(props, "preview_custom_sky")
                    toggles = lighting.row(align=True)
                    toggles.prop(props, "preview_shadows", toggle=True)
                    toggles.prop(props, "preview_occlusion", toggle=True)
                    fire = lighting.row(align=True)
                    fire.enabled = props.shader_flame_enabled
                    fire.prop(props, "preview_fire_lighting", slider=True)

                # View and Look side by side, as in Blender's Color Management
                # (named after it: "Look" is also this section's name).
                preview.label(text="Color Management")
                color = preview.column(align=True)
                view = color.row(align=True)
                view.prop(props, "preview_tone_mapping", text="")
                look = view.row(align=True)
                if props.preview_tone_mapping == "scene":
                    # Scene follows the scene's own look: shown, changed in Color Management.
                    look.enabled = False
                    look.prop(scene.view_settings, "look", text="")
                else:
                    look.prop(props, "preview_look", text="")
                color.prop(props, "preview_exposure_offset", slider=True)
            _wrapped(preview, "The preview keeps its VRAM until Stop", "INFO")

    behavior_box = layout.box().column(align=True)
    behavior_box.enabled = not is_baking
    if _section(behavior_box, props, "show_panel_behavior", "Smoke Behavior", "MOD_FLUID"):
        behavior = behavior_box.column(align=True)
        # How fast and how large the simulated world is.
        world = _pair(behavior)
        world.prop(props, "simulation_speed", text="Speed")
        world.prop(props, "simulation_scale", text="Scale")
        behavior.separator()
        # One split for both rows so their X / Y / Z columns line up; the Gravity
        # from Scene button sits just left of the fields (a blank slot for Wind).
        for name, label, button in (("gravity", "Gravity", True), ("wind", "Wind", False)):
            if _NARROW:
                # Three fields with units need the full width; the label and
                # the scene button go on the row above.
                header = behavior.row(align=True)
                header.label(text=label)
                if button:
                    header.operator("fumaris.gravity_from_scene", text="", icon="SCENE_DATA")
                # Narrower still, a third of the row cut "-9.8 m/s²" to
                # "-9.8 m/": one field per row, as Blender's Scene Gravity.
                fields = behavior.column(align=True) if _TIGHT else behavior.row(align=True)
                fields.prop(props, name, text="")
                continue
            split = _label_split(behavior, 0.12, 0.22)
            split.label(text=label)
            right = split.row(align=True)
            if button:
                right.operator("fumaris.gravity_from_scene", text="", icon="SCENE_DATA")
            else:
                right.label(text="", icon="BLANK1")
            right.prop(props, name, text="")
        # A domain that travels with its object (vehicles), and how much of
        # that motion the air shares.
        move = _pair(behavior)
        move.prop(props, "follow_object")
        if props.follow_object:
            move.prop(props, "carry_air", slider=True)
        behavior.separator()
        buoyancy = _pair(behavior)
        if props.physical_buoyancy:
            # The gas-law lift replaces the slider; shown greyed for reference.
            value = buoyancy.row(align=True)
            value.enabled = False
            value.label(text=f"Physical Buoyancy {physical_buoyancy(props):.1f}")
        else:
            buoyancy.prop(props, "buoyancy_per_temp")
        buoyancy.prop(props, "buoyancy_per_smoke")
        # Its own labelled row: an unlabelled checkbox beside the slider read
        # as nothing (2026-11-14 UX test).
        behavior.prop(props, "physical_buoyancy")
        behavior.separator()
        fade = _pair(behavior)
        fade.prop(props, "dissipation")
        fade.prop(props, "smoke_decay", text="Decay")
        behavior.prop(props, "velocity_damping")
        behavior.separator()
        vorticity_row = behavior.row(align=True)
        vorticity_row.prop(props, "vorticity")
        mask = vorticity_row.row(align=True)
        mask.active = props.vorticity > 0.0
        mask.prop(props, "vorticity_mask", text="")

    # Same card, but its own dropdown (not nested in Smoke Behavior).
    if _section(behavior_box, props, "show_domain_combustion", "Combustion", "LIGHT_SUN"):
        combustion = behavior_box.column(align=True)
        combustion.prop(props, "flame_temperature")
        combustion.prop(props, "fuel_energy")
        combustion.prop(props, "ignition_temperature_k")
        combustion.separator()
        times = _pair(combustion)
        times.prop(props, "burn_time")
        times.prop(props, "cooling_time")
        combustion.prop(props, "smoke_per_burn", text="Smoke Amount")
        combustion.prop(props, "gas_expansion")
        # The rest of the combustion model, folded (with a count of the
        # settings changed from their defaults, so none hides unnoticed).
        more_names = ("temperature_diffusion", "wisp_cooling", "fuel_threshold", "oxygen_limit",
                      "soot_radiation", "oxygen_field", "oxygen_use", "oxygen_mixing")
        changed = _changed_count(props, more_names)
        more, more_open = _nested_foldout(combustion, props, "show_combustion_more",
                                          f"More ({changed} changed)" if changed else "More")
        if more_open:
            more.prop(props, "temperature_diffusion")
            shaping = _pair(more)
            shaping.prop(props, "wisp_cooling")
            shaping.prop(props, "fuel_threshold")
            air = _pair(more)
            air.prop(props, "oxygen_limit")
            air.prop(props, "soot_radiation")
            oxygen = more.row(align=True)
            oxygen.active = props.oxygen_limit > 0.0
            oxygen.prop(props, "oxygen_field", text="", icon="FORCE_WIND", toggle=True)
            field = oxygen.row(align=True)
            field.active = props.oxygen_field and props.oxygen_limit > 0.0
            field.prop(props, "oxygen_use", text="Use")
            field.prop(props, "oxygen_mixing", text="Mixing")

    # Modifiers: one stack for detail (Disturb, Grain, Shred), rates
    # (Vorticity, Dissipation, Cooling, Soot Burn-off) and particles, each
    # with masks. Folded by default; its count shows while folded.
    modifiers_box = layout.box().column(align=True)
    modifiers_box.enabled = not is_baking
    count = detail_stack.header_count(props)
    if _section(modifiers_box, props, "show_domain_detail_stack", f"Modifiers ({count})" if count else "Modifiers",
                "MODIFIER"):
        detail_stack.draw(modifiers_box.column(align=True), props.id_data)

    participants_box = layout.box()
    participants_box.enabled = not is_baking
    if _section(participants_box, props, "show_panel_participants", "Participants", "OUTLINER_COLLECTION"):
        participants = participants_box.column(align=True)
        # Labels in a narrow column so the collection fields start close to them.
        for name, label in (("emitter_collection", "Emitters"), ("collider_collection", "Colliders"),
                            ("effector_collection", "Effectors"), ("outflow_collection", "Outflows")):
            split = _label_split(participants, 0.18, 0.3)
            split.label(text=label)
            split.prop(props, name, text="")
        participants.operator_menu_enum("fumaris.add_source", "kind", text="Add Source", icon="ADD")
        _draw_participant_list(participants, props)

    output_box = layout.box()
    output_box.enabled = not is_baking
    if _section(output_box, props, "show_cache_location", "Output", "OUTPUT"):
        output = output_box.column(align=True)
        # One label column for every row, so labels line up and none truncate.
        split = _label_split(output, 0.2, 0.3)
        labels = split.column(align=True)
        fields = split.column(align=True)
        # The bake the domain shows (chosen in Versions) and tools for it.
        # "Version", as in Versions below ("Bake: bake" read as a typo).
        labels.label(text="Version")
        fields.label(text=props.cache_slot)
        # Its tools, named where they fit (four bare icons were a guess).
        labels.label(text="")
        slot = fields.row(align=True)
        named = not _TIGHT
        slot.operator("fumaris.cache_make_lighter", icon="MOD_DECIM", text="Lighter" if named else "")
        sparks = slot.row(align=True)
        # Dimmed without baked frames (not disabled: it says what is missing
        # when clicked). From the folder scan Versions draws: Baked Frames
        # also counts what a live preview simulated.
        from .compare import list_versions
        shown = os.path.basename(peek_output_directory_for_object(props.id_data) or "")
        sparks.active = any(v["name"] == shown and v["frames"] for v in list_versions(props.id_data))
        sparks.operator("fumaris.add_sparks", icon="PARTICLES", text="Sparks" if named else "")
        slot.operator(
            "fumaris.cache_protect",
            icon="LOCKED" if slot_protected else "UNLOCKED",
            text="",
            depress=slot_protected,
        )
        delete = slot.row(align=True)
        delete.enabled = mode is None and not slot_protected
        delete.operator("fumaris.delete", icon="TRASH", text="")
        missing_for_sparks = domain.get(SPARKS_MISSING_MARKER)
        if missing_for_sparks:
            # This bake lacks a grid the domain's sparks need (sparks.follow_bakes).
            labels.label(text="Sparks")
            fields.label(text=f"No {missing_for_sparks}: bake again", icon="ERROR")
        labels.label(text="Directory")
        # Empty fields show the defaults they fall back to.
        fields.prop(props, "output_dir", text="", placeholder="//fumaris_cache/")
        labels.label(text="Prefix")
        fields.prop(props, "output_prefix", text="", placeholder="fumaris_")
        labels.label(text="VDB")
        if _TIGHT:
            # Side by side they read "Active... | Half (16...".
            labels.label(text="")
        vdb = fields.column(align=True) if _TIGHT else fields.row(align=True)
        vdb.prop(props, "vdb_compression", text="")
        vdb.prop(props, "vdb_precision", text="")
        labels.label(text="Material")
        fields.prop(props, "volume_material", text="")
        output.prop(props, "vdb_writers", **({"text": "Writers"} if _NARROW else {}))
        output.separator()
        output.prop(props, "volume_selectable")
        output.separator()
        # Each grid: write it, and at which resolution.
        channels = output.column(align=True)
        # The generated material reads density, so it is written whatever its
        # checkbox says: shown ticked and greyed. Temperature (its fire) is up
        # to the checkbox.
        needed = {"density"} if props.volume_material is None else set()
        for name in ("density", "temperature", "fuel", "burn", "velocity"):
            row = channels.split(factor=0.6, align=True)
            written = name in needed or getattr(props, f"export_{name}_vdb")
            if name in needed:
                forced = row.row(align=True)
                forced.enabled = False
                forced.label(text=name.title(), icon="CHECKBOX_HLT")
            else:
                row.prop(props, f"export_{name}_vdb", text=name.title())
            resolution = row.row(align=True)
            resolution.active = written
            resolution.prop(props, f"export_{name}_resolution", text="")
        row = channels.split(factor=0.6, align=True)
        # The Pyro look's fire is the baked glow: written whatever the toggle.
        glow_forced = props.volume_material is None and is_pyro_look(props)
        glow_written = props.export_glow_vdb or glow_forced
        if glow_forced:
            forced = row.row(align=True)
            forced.enabled = False
            forced.label(text="Glow", icon="CHECKBOX_HLT")
        else:
            row.prop(props, "export_glow_vdb", text="Glow")
        resolution = row.row(align=True)
        resolution.active = glow_written
        resolution.prop(props, "export_glow_resolution", text="")
        if glow_written:
            output.prop(props, "glow_radius")
            output.prop(props, "glow_source")
            if glow_source_heat(props):
                heat = output.row(align=True)
                heat.prop(props, "glow_heat_from", text="Heat From")
                heat.prop(props, "glow_heat_to", text="To")
        if glow_forced and not props.export_glow_vdb:
            _wrapped(output, "The Pyro look's fire needs Glow", "INFO")
        if needed:
            _wrapped(output, "The material needs Density", "INFO")
            if not props.export_temperature_vdb and _scene_heat(props):
                _wrapped(output, "Without Temperature the material shows no fire", "INFO")

    advanced_box = layout.box()
    advanced_box.enabled = not is_baking
    if _section(advanced_box, props, "show_panel_advanced", "Advanced", "PREFERENCES"):
        # Rarely touched. What a shot is tuned with lives above.
        advanced = advanced_box.column(align=True)
        detail = _pair(advanced)
        if _NARROW:
            choice = _label_split(detail, 0.4, 0.4)
            choice.label(text="Smoke Detail")
            choice.prop(props, "smoke_detail", text="")
        else:
            detail.prop(props, "smoke_detail", text="Smoke Detail")
        detail.prop(props, "edge_sharpness", text="Edges")
        if props.smoke_detail == "sharp":
            _wrapped(advanced, "Sharp: about 1.25x the simulation time", "INFO")
        advanced.prop(props, "projection_filter", slider=True)
        advanced.separator()
        # The on/off switches, two by two.
        grid = advanced.grid_flow(row_major=True, columns=1 if _NARROW else 2, even_columns=True, even_rows=True, align=True)
        grid.prop(props, "advection_reflection")
        # Full-Resolution Channels (the old Full-Resolution Velocity's name
        # in the file, which also counts that option's stored value)
        grid.prop(props, "full_resolution_velocity")
        grid.prop(props, "auto_cell_size")
        storage = _fire_storage(props)
        if props.full_resolution_velocity:
            _wrapped(advanced, "Motion at full resolution: about 3-4x the VRAM and 1.5-4x the time", "INFO")
            draw_full_resolution_forecast(advanced, props, diagnostics.snapshot(props.id_data),
                                          _fire_on_half_storage(props))
        elif storage == "half":
            _wrapped(advanced, "Motion, fuel and burn at half resolution: 1.6x the blocks for a fire", "INFO")
        advanced.separator()
        walls = _pair(advanced)
        walls.prop(props, "open_collider_wall")
        walls.prop(props, "closed_collider_margin")
        advanced.separator()
        # Settings that tune the solver itself; rarely worth changing.
        internal_names = ("trace_steps", "sparse_block_min_lifetime", "allocation_smoke_threshold",
                          "allocation_speed_threshold", "physics_convex_collision")
        changed = _changed_count(props, internal_names)
        internals, internals_open = _nested_foldout(advanced, props, "show_advanced_internals",
                                                    f"Solver Internals ({changed} changed)" if changed else "Solver Internals")
        if internals_open:
            internals.prop(props, "trace_steps")
            internals.prop(props, "sparse_block_min_lifetime")
            allocation = _pair(internals)
            allocation.prop(props, "allocation_smoke_threshold", text="Smoke Threshold")
            allocation.prop(props, "allocation_speed_threshold", text="Speed Threshold")
            internals.prop(props, "physics_convex_collision")

    _draw_diagnostics(layout, props)


class FUMARIS_PT_main(Panel):
    """Fumaris object settings in the Physics Properties editor."""

    bl_label = "Fumaris"
    bl_idname = "FUMARIS_PT_main"
    bl_space_type = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context = "physics"

    @classmethod
    def poll(cls, context):
        return context.object is not None

    def draw(self, context):
        global _NARROW, _TIGHT, _LINE_CHARS
        region = context.region
        scale = context.preferences.system.ui_scale or 1.0
        width = region.width / scale if region is not None else 1000.0
        _NARROW = region is not None and width < NARROW_WIDTH
        _TIGHT = region is not None and width < TIGHT_WIDTH
        _LINE_CHARS = int((width - 70) / 7.0)
        layout = self.layout
        obj = context.object
        props = obj.fumaris

        header = layout.box()
        if props.smoke_object_type != "domain":
            header.enabled = active_mode() != "baking"
        header.label(text=obj.name, icon="OBJECT_DATA")
        header.prop(props, "smoke_object_type", text="Role")
        if props.smoke_object_type != "none":
            presets = header.row(align=True)
            presets.menu("FUMARIS_MT_presets", text="Presets", icon="PRESET")
            presets.operator("fumaris.preset_save", text="", icon="ADD")
        if props.smoke_object_type == "none":
            setup = header.row()
            setup.enabled = blocking_job() is None
            setup.operator("fumaris.quick_setup", text="Quick Setup…", icon="ADD")

        if props.smoke_object_type == "domain":
            _draw_domain(layout, context.scene, props)
        else:
            from .playback import draw_controls, own_domain, scene_domains
            if active_job() or scene_domains(context):
                # Play and Bake here run this object's own domain, whichever
                # the sidebar has chosen.
                draw_controls(layout.box(), context, domain=own_domain(context, obj))
            content = layout.column()
            content.enabled = active_mode() != "baking"
            if props.smoke_object_type == "emitter":
                _draw_emitter(content, obj, props)
            elif props.smoke_object_type == "collider":
                _draw_collider(content, props)
            elif props.smoke_object_type == "effector":
                _draw_effector(content, props)
            elif props.smoke_object_type == "outflow":
                _draw_outflow(content, props)
            elif props.smoke_object_type == "none":
                content.label(text="Excluded from Fumaris simulations")


class FUMARIS_MT_presets(bpy.types.Menu):
    bl_label = "Fumaris Presets"
    bl_idname = "FUMARIS_MT_presets"

    def draw(self, context):
        from .presets import list_presets, preset_role
        layout = self.layout
        obj = context.object
        names = list_presets(preset_role(obj.fumaris)) if obj else []
        if not names:
            layout.label(text="No saved presets: use + to save one")
            return
        # Rows in an aligned column span the menu (as Blender's own preset
        # menus), so each delete button sits at the right edge.
        column = layout.column(align=True)
        for name in names:
            row = column.row(align=True)
            row.operator("fumaris.preset_apply", text=name).name = name
            row.operator("fumaris.preset_delete", text="", icon="X").name = name


_SIDEBAR_SECTIONS = ("FUMARIS_sidebar_domains", "FUMARIS_sidebar_add", "FUMARIS_sidebar_objects")


def _sidebar_section(layout, idname):
    """A collapsible section in a box, so lists stand out like the
    Properties editor's sections. Unlike layout.panel, no gap under the
    header. Returns (header row, body or None when folded)."""
    box = layout.box()
    wm = bpy.context.window_manager
    opened = getattr(wm, idname)
    header = box.row(align=True)
    header.alignment = "LEFT"
    header.prop(wm, idname, text="", icon="TRIA_DOWN" if opened else "TRIA_RIGHT", emboss=False)
    return header, box.column() if opened else None


class FUMARIS_PT_sidebar(bpy.types.Panel):
    """The 3D Viewport sidebar's Fumaris tab: the scene's domains, the chosen
    domain's transport and objects, and one-click setup. Clicking a name
    shows its settings in the Properties editor (no need to find it in the
    Outliner); the chosen domain stays chosen while its objects are selected."""
    bl_label = "Fumaris"
    bl_idname = "FUMARIS_PT_sidebar"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Fumaris"

    def draw(self, context):
        from . import playback
        layout = self.layout
        domains = playback.scene_domains(context)
        domain = playback.resolve_domain(context)
        # Collapsible sub-panels (like the Properties editor's) for the lists.
        header, body = _sidebar_section(layout, "FUMARIS_sidebar_domains")
        header.label(text="Domains", icon="MOD_FLUID")
        if body:
            create = body.row(align=True)
            try:
                create.prop(context.scene, "fumaris_new_domain_name", text="", placeholder="New domain name")
            except TypeError:   # no placeholder text in this Blender
                create.prop(context.scene, "fumaris_new_domain_name", text="")
            if domains:
                create.operator("fumaris.domain_create", text="", icon="ADD")
                create.operator_menu_enum("fumaris.quick_setup", "preset", text="", icon="MOD_FLUID")
            else:
                # First run: the two ways to start, named (icon-only buttons
                # read as unexplained, beta 3 review).
                body.label(text="No Fumaris domain in this scene", icon="INFO")
                start = body.row(align=True)
                start.operator("fumaris.domain_create", text="New Domain", icon="ADD")
                start.operator_menu_enum("fumaris.quick_setup", "preset", text="Quick Setup", icon="MOD_FLUID")
            from . import background
            listing = body.column(align=True)
            for obj in domains:
                row = listing.row(align=True)
                choose = row.row(align=True)
                choose.enabled = not blocking_job()
                _highlight_row(choose, "fumaris.domain_choose", obj.name,
                               "RADIOBUT_ON" if obj == domain else "RADIOBUT_OFF", obj == domain)
                # Show in Viewport: the domain, its preview and its imported volumes.
                row.prop(obj.fumaris, "viewport_visible", text="", emboss=False,
                         icon="HIDE_OFF" if obj.fumaris.viewport_visible else "HIDE_ON")
                # Its background bake: Stop on its row, progress under it.
                job = background_job(obj)
                if job is not None:
                    if job.state != "ended":
                        row.operator("fumaris.bake_background_stop", text="", icon="CANCEL").domain = obj.name
                    background.draw_status(listing, job)
            if domains:
                # Every domain, or the selected ones, baked in the background:
                # two buttons, so Bake All is there whatever is selected.
                selected = sum(1 for obj in domains if obj.select_get())
                bake = body.column(align=True)
                buttons = bake.row(align=True)
                buttons.operator("fumaris.bake_all", icon="RENDERLAYERS", text="Bake All").scope = "ALL"
                chosen = buttons.row(align=True)
                chosen.enabled = selected > 0
                chosen.operator("fumaris.bake_all", text=f"Bake Selected ({selected})").scope = "SELECTED"
                if background.jobs():
                    bake.operator("fumaris.bake_background_stop", text="Stop All", icon="CANCEL").everything = True
        if not domains:
            return
        layout.separator(factor=0.3)
        playback.draw_controls(layout, context, popover=False, details=False)
        layout.separator(factor=0.3)
        if domain is None:
            layout.label(text="Choose a domain", icon="INFO")
            return
        # The object list first, Add Objects under it (user, 2026-11-16; was the
        # reverse since 2026-11-15).
        header, body = _sidebar_section(layout, "FUMARIS_sidebar_objects")
        # The domain is named in the controls above: a long name cut this off.
        header.label(text="Objects", icon="OUTLINER_COLLECTION")
        if body:
            draw_participant_rows(body.column(align=True), domain.fumaris, activate=True)
        header, body = _sidebar_section(layout, "FUMARIS_sidebar_add")
        header.label(text="Add Objects", icon="ADD")
        if body:
            add = body.column(align=True)
            add.enabled = not blocking_job()
            for pair in (("emitter", "collider"), ("effector", "outflow")):
                row = add.row(align=True)
                for role in pair:
                    operator = row.operator("fumaris.participants_add_selected", text=role.title(),
                                            icon=_ROLE_ICONS[role])
                    operator.role = role
            source = body.row()
            source.enabled = not blocking_job()
            # Add Source acts on context.object: point it at the chosen domain.
            source.context_pointer_set("object", domain)
            source.operator_menu_enum("fumaris.add_source", "kind", text="New Source", icon="ADD")


CLASSES = (FUMARIS_MT_presets, FUMARIS_MT_bake_more, FUMARIS_OT_emitter_color_offer, FUMARIS_OT_color_mixing_enable,
           FUMARIS_OT_domain_show_look, FUMARIS_PT_main, FUMARIS_OT_participant_list_more, FUMARIS_PT_sidebar)


def register():
    for idname in _SIDEBAR_SECTIONS:
        setattr(bpy.types.WindowManager, idname, bpy.props.BoolProperty(default=True, options={"SKIP_SAVE"}))
    bpy.types.WindowManager.fumaris_participant_search = bpy.props.StringProperty(
        name="Search", description="Show only objects whose name contains this text",
        options={"TEXTEDIT_UPDATE", "SKIP_SAVE"})
    bpy.types.WindowManager.fumaris_participant_limit = bpy.props.IntProperty(
        default=_PAGE, min=_PAGE, options={"SKIP_SAVE"})
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
    for idname in _SIDEBAR_SECTIONS:
        delattr(bpy.types.WindowManager, idname)
    del bpy.types.WindowManager.fumaris_participant_search
    del bpy.types.WindowManager.fumaris_participant_limit
