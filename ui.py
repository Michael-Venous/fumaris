import bpy
from bpy.types import Panel

from .runtime import active_job, active_mode


def _foldout(layout, props, property_name, label):
    row = layout.row(align=True)
    opened = getattr(props, property_name)
    row.prop(
        props,
        property_name,
        text=label,
        icon="TRIA_DOWN" if opened else "TRIA_RIGHT",
        emboss=False,
    )
    return opened


def _nested_foldout(layout, props, property_name, label):
    row = layout.row(align=True)
    row.separator(factor=0.65)
    column = row.column(align=True)
    opened = _foldout(column, props, property_name, label)
    if opened:
        content = layout.row(align=True)
        content.separator(factor=1.3)
        return content.column(align=True), opened
    return column, opened


def _is_particle_point_cloud(props):
    return (
        props.participant_type == "particles"
        and props.particle_subtype == "point_cloud"
    )


def _draw_mesh_emitter_options(box, props):
    box.prop(props, "mesh_emission_mode", text="Region")
    box.prop(props, "mesh_emission_distance")
    box.prop(props, "mesh_emission_mask_attribute", text="Mask")
    if props.mesh_emission_mask_attribute.strip():
        box.prop(props, "mesh_emission_mask_threshold")
    box.prop(props, "normal_velocity")


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
        )
        box.prop(props, "particle_subtype", text="Type")
        if props.particle_subtype == "point_cloud":
            box.prop(props, "point_radius")
        elif props.particle_subtype == "mesh":
            _draw_mesh_emitter_options(box, props)
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
        box.prop(props, "volume_filepath")


def _draw_emitter(layout, obj, props):
    box = layout.box()
    box.label(text="Emitter", icon="OUTLINER_OB_FORCE_FIELD")
    box.prop(props, "participant_enabled")
    _draw_emitter_shape(box, obj, props)

    box = layout.box()
    box.label(text="Emitted Channels", icon="MOD_FLUID")
    box.prop(props, "emitter_smoke")
    box.prop(props, "emitter_temperature")
    box.prop(props, "emitter_fuel")
    box.prop(props, "emitter_burn")
    box.prop(props, "emitter_divergence")

    box = layout.box()
    box.label(text="Velocity", icon="FORCE_FORCE")
    box.prop(props, "velocity")
    box.prop(props, "motion_velocity_scale")

    box = layout.box()
    column, opened = _nested_foldout(box, props, "show_emitter_coupling", "Channel Coupling")
    if opened:
        column.prop(props, "couple_rate_smoke")
        column.prop(props, "couple_rate_temperature")
        column.prop(props, "couple_rate_fuel")
        column.prop(props, "couple_rate_burn")
        column.prop(props, "couple_rate_velocity")
        column.prop(props, "couple_rate_divergence")

    box = layout.box()
    column, opened = _nested_foldout(box, props, "show_emitter_advanced", "Advanced")
    if opened:
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
    box.label(text="Collider", icon="MOD_PHYSICS")
    box.prop(props, "participant_enabled")
    box.prop(props, "collider_type", text="Shape")
    if props.collider_type in {"mesh", "box"}:
        box.prop(props, "collider_margin")
    elif props.collider_type == "sphere":
        box.prop(props, "collider_radius")
    box.prop(props, "collider_velocity_influence")


def _draw_effector(layout, props):
    box = layout.box()
    box.label(text="Effector", icon="FORCE_FORCE")
    box.prop(props, "participant_enabled")
    box.prop(props, "effector_type")
    box.prop(props, "effector_strength")
    box.prop(props, "effector_radius")
    box.prop(props, "effector_coupling")
    box.prop(props, "effector_samples")

    if props.effector_type in {"force", "wind", "vortex", "turbulence"}:
        noise, noise_open = _nested_foldout(box, props, "show_panel_noise", "Noise")
        if noise_open:
            noise.prop(props, "effector_noise_amount")
            noise.prop(props, "effector_noise_size")
            noise.prop(props, "effector_noise_seed")

    falloff, falloff_open = _nested_foldout(box, props, "show_panel_falloff", "Falloff")
    if falloff_open:
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
    box.label(text="Outflow", icon="MOD_FLUID")
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
    return (
        f"{state}: {props.baked_frames} frames | {_compact_time(props.bake_elapsed)} | "
        f"VRAM {_compact_memory(props.bake_peak_vram_mb)} | "
        f"RAM {_compact_memory(props.bake_peak_ram_mb)}"
    )


def _draw_domain(layout, scene, props):
    job = active_job()
    mode = active_mode()
    is_baking = mode == "baking"
    is_previewing = mode == "previewing"
    is_initializing = job is not None and not getattr(job, "_accepted", False)

    controls = layout.box()
    controls.label(text="Simulate", icon="PHYSICS")
    row = controls.row(align=True)
    sub = row.row(align=True)
    sub.enabled = mode is None
    sub.operator("fumaris.bake", icon="RENDER_ANIMATION", text="Bake")
    stop = row.row(align=True)
    stop.enabled = is_baking
    stop.operator("fumaris.stop", icon="CANCEL", text="Stop")
    delete = row.row(align=True)
    delete.enabled = mode is None
    delete.operator("fumaris.delete", icon="TRASH", text="Delete")

    if props.simulation_state == "baking":
        controls.label(text="Simulation is baking", icon="TIME")
    elif props.simulation_state == "baked":
        controls.label(text=_bake_summary(props, "Baked"), icon="CHECKMARK")
    elif props.simulation_state == "stopped":
        controls.label(text=_bake_summary(props, "Stopped"), icon="PAUSE")

    if is_initializing:
        notice = controls.box()
        notice.label(text="Initializing Flow GPU...", icon="INFO")
        notice.label(text="First run compiles GPU shaders")
        notice.label(text="This may take several minutes")

    preview = controls.row(align=True)
    play = preview.row(align=True)
    play.enabled = mode is None and props.simulation_state != "baked"
    play.operator("fumaris.preview_play", icon="PLAY", text="Play")
    preview_stop = preview.row(align=True)
    preview_stop.enabled = is_previewing
    preview_stop.operator("fumaris.preview_stop", icon="CANCEL", text="Stop")

    settings = controls.column(align=True)
    settings.enabled = not is_baking
    settings.separator()
    row = settings.row(align=True)
    row.prop(props, "sim_start_frame")
    row.prop(props, "sim_end_frame")
    settings.prop(props, "resolution")
    settings.prop(props, "sparse_block_capacity")
    settings.prop(props, "num_sub_steps")

    output, output_open = _nested_foldout(settings, props, "show_cache_location", "Output")
    if output_open:
        output.prop(props, "output_dir")
        output.prop(props, "cache_slot")
        output.prop(props, "output_prefix")
        output.prop(props, "vdb_compression")
        output.prop(props, "volume_material")
        output.prop(props, "volume_selectable")
        output.separator()
        output.prop(props, "export_temperature_vdb")
        output.prop(props, "export_fuel_vdb")
        output.prop(props, "export_burn_vdb")
        output.prop(props, "export_flame_vdb")
        if props.export_flame_vdb:
            output.prop(props, "flame_temperature_min")
            output.prop(props, "flame_temperature_max")
        output.prop(props, "export_velocity_vdb")

    preview, preview_open = _nested_foldout(settings, props, "show_preview_display", "Preview Settings")
    if preview_open:
        preview.prop(props, "preview_bake")
        preview.prop(props, "preview_resolution_percent", slider=True)
        preview.prop(props, "preview_dot_resolution")
        preview.prop(props, "preview_max_points")
        if props.preview_max_points > 4_000_000:
            preview.label(text="High dot limits use substantial transfer and GPU memory", icon="ERROR")
        preview.prop(props, "preview_dot_size")
        preview.prop(props, "preview_color")
        preview.prop(props, "preview_opacity")

    participants = layout.box()
    participants.enabled = not is_baking
    participants.label(text="Participants", icon="OUTLINER_COLLECTION")
    participants.prop(props, "emitter_collection")
    participants.prop(props, "collider_collection")
    participants.prop(props, "effector_collection")
    participants.prop(props, "outflow_collection")

    behavior = layout.box()
    behavior.enabled = not is_baking
    behavior.label(text="Smoke Behavior", icon="MOD_FLUID")
    behavior.prop(props, "gravity")
    behavior.prop(props, "buoyancy_per_temp")
    behavior.prop(props, "buoyancy_per_smoke")
    behavior.prop(props, "vorticity")
    behavior.prop(props, "dissipation")

    combustion, combustion_open = _nested_foldout(behavior, props, "show_domain_combustion", "Combustion")
    if combustion_open:
        combustion.prop(props, "temperature_input_scale")
        combustion.prop(props, "ignition_temperature")
        combustion.prop(props, "burn_per_temp")
        combustion.prop(props, "fuel_per_burn")
        combustion.prop(props, "temp_per_burn")
        combustion.prop(props, "smoke_per_burn")
        combustion.prop(props, "divergence_per_burn")
        combustion.prop(props, "cooling_rate")

    advanced = layout.box()
    advanced.enabled = not is_baking
    if _foldout(advanced, props, "show_panel_advanced", "Advanced"):
        advanced.prop(props, "simulation_speed")
        advanced.prop(props, "auto_cell_size")
        advanced.prop(props, "small_sparse_blocks")
        advanced.prop(props, "physics_convex_collision")
        advanced.prop(props, "sparse_block_min_lifetime")
        advanced.prop(props, "allocation_smoke_threshold")
        advanced.prop(props, "allocation_speed_threshold")
        advanced.prop(props, "allocation_speed_min_smoke")
        advanced.prop(props, "allocate_neighbor_blocks")
        advanced.prop(props, "boundary_safe_advection")

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
        layout = self.layout
        obj = context.object
        props = obj.fumaris

        header = layout.box()
        if props.smoke_object_type != "domain":
            header.enabled = active_mode() != "baking"
        header.label(text=obj.name, icon="OBJECT_DATA")
        header.prop(props, "smoke_object_type", text="Flow Object")

        if props.smoke_object_type == "domain":
            _draw_domain(layout, context.scene, props)
        else:
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


CLASSES = (FUMARIS_PT_main,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
