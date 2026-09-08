import bpy
import textwrap
from bpy.types import Panel

from .runtime import active_job, active_mode
from . import diagnostics


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


def _draw_diagnostics(layout, props):
    state = diagnostics.snapshot(props.id_data)
    box = layout.box()
    opened = _foldout(box, props, "show_diagnostics", "Diagnostics")
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
        if opened:
            box.label(text=f"Effective resolution: {number('flow_effective_resolution'):.0f} / {number('flow_requested_resolution'):.0f}")
            box.label(text=f"CFL (previous field): {number('flow_previous_courant'):.2f} cells/step")
            box.label(text=f"Voxel size: {number('flow_density_voxel_size_x'):.5g} m")
            if number("flow_detail_working_bytes"):
                box.label(text=f"Derived smoke resolution: {number('flow_detail_effective_resolution'):.0f}")
                box.label(text="Detail working estimate: " + _compact_memory(number("flow_detail_working_bytes") / 1048576))
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


def _draw_domain(layout, scene, props):
    job = active_job()
    mode = active_mode()
    is_baking = mode == "baking"
    is_previewing = mode == "previewing"
    is_preview_paused = is_previewing and bool(
        getattr(job, "_pause_requested", False)
    )
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
    pause = preview.row(align=True)
    pause.enabled = is_previewing and not is_initializing
    pause.operator(
        "fumaris.preview_pause",
        icon="PLAY" if is_preview_paused else "PAUSE",
        text="Resume" if is_preview_paused else "Pause",
    )
    preview_stop = preview.row(align=True)
    preview_stop.enabled = is_previewing
    preview_stop.operator("fumaris.preview_stop", icon="CANCEL", text="Stop")
    _draw_diagnostics(controls, props)

    settings = controls.column(align=True)
    settings.enabled = not is_baking
    settings.separator()
    row = settings.row(align=True)
    row.prop(props, "sim_start_frame")
    row.prop(props, "sim_end_frame")
    settings.prop(props, "resolution")
    settings.prop(props, "sparse_block_capacity")
    settings.prop(props, "num_sub_steps")

    appearance, appearance_open = _nested_foldout(
        settings,
        props,
        "show_appearance",
        "Appearance",
    )
    if appearance_open:
        appearance.label(text="Smoke")
        appearance.prop(props, "shader_smoke_density")
        appearance.prop(props, "shader_smoke_color", text="Color")
        appearance.separator()
        appearance.prop(props, "shader_flame_enabled")
        fire = appearance.column(align=True)
        fire.enabled = props.shader_flame_enabled
        fire.prop(props, "shader_flame_brightness")
        row = fire.row(align=True)
        row.prop(props, "flame_temperature_min", text="Start")
        row.prop(props, "flame_temperature_max", text="Full")
        fire.prop(props, "shader_temperature_multiplier")
        if props.volume_material is not None:
            appearance.label(
                text="Custom Volume Material overrides baked appearance",
                icon="INFO",
            )

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
        output.prop(props, "export_velocity_vdb")
        if props.volume_material is None:
            output.label(
                text="Generated material always includes temperature and burn",
                icon="INFO",
            )

    preview, preview_open = _nested_foldout(settings, props, "show_preview_display", "Preview Settings")
    if preview_open:
        preview.prop(props, "preview_bake")
        preview.prop(props, "preview_mode")
        preview.prop(props, "preview_resolution_percent", slider=True)
        if props.preview_mode == "volume":
            preview.prop(props, "preview_image_scale", slider=True)
            preview.prop(props, "preview_max_ray_steps")
            preview.prop(props, "preview_tone_mapping")
            preview.prop(props, "preview_exposure", slider=True)
            preview.prop(props, "preview_shadows")
            light = preview.column()
            light.enabled = props.preview_shadows
            light.prop(props, "preview_shadow_min_light", slider=True)
            light.label(text="Light Direction")
            light.prop(props, "preview_light_azimuth")
            light.prop(props, "preview_light_elevation")
            preview.label(text="Play/Pause keeps live Flow data in VRAM", icon="INFO")
        else:
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
    detail_toggle = behavior.row()
    detail_toggle.enabled = not is_previewing
    detail_toggle.prop(props, "upres_enabled")
    if props.upres_enabled:
        detail = behavior.box()
        detail.prop(props, "upres_strength")
        detail.prop(props, "upres_scale")
        detail.prop(props, "upres_memory_mb")
        detail.label(text="2x smoke detail; extra VRAM, base fire fields unchanged", icon="INFO")
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
        row = advanced.row()
        row.enabled = props.allocate_neighbor_blocks
        row.prop(props, "adaptive_boundary_padding")
        if props.allocate_neighbor_blocks and props.adaptive_boundary_padding:
            advanced.label(text="Extra GPU time and memory", icon="INFO")
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
