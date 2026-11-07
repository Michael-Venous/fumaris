import bpy
import textwrap
from bpy.types import Panel

from .runtime import active_job, active_mode
from . import diagnostics
from .exporters import simulation_memory_mb
from .memory_auto import draw_budget_summary
from .cache import is_protected
from .utils import peek_output_directory_for_object, get_addon_preferences


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
    from .operators import solo_object_name
    from .participants import participant_overview
    layout.separator(factor=0.5)
    content, opened = _nested_foldout(layout, props, "show_participant_list", "Objects")
    if not opened:
        return
    rows = participant_overview(props)
    if not rows:
        content.label(text="No objects in these collections", icon="INFO")
        return
    solo = solo_object_name(props.id_data)
    for obj, role, reason in rows:
        usable = reason is None or reason == "disabled"
        split = content.split(factor=0.8 if usable else 0.45, align=True)
        name = split.row(align=True)
        name.alignment = "LEFT"
        icon = _ROLE_ICONS.get(role, "OBJECT_DATA")
        select = name.operator("fumaris.participant_select", text=obj.name, icon=icon, emboss=False)
        select.name = obj.name
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
    if len(rows) >= 60:
        content.label(text="Showing the first 60 objects")


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
    box.prop(props, "mesh_velocity_attribute", text="Velocity")


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
        row = box.row(align=True)
        row.prop(props, "volume_use_sequence")
        sub = row.row(align=True)
        sub.enabled = props.volume_use_sequence
        sub.prop(props, "volume_frame_offset")
        box.prop(props, "volume_use_velocity")
        grids, grids_open = _nested_foldout(box, props, "show_volume_grids", "Grid Names")
        if grids_open:
            grids.prop(props, "volume_grid_density", text="Smoke")
            grids.prop(props, "volume_grid_temperature", text="Temperature")
            grids.prop(props, "volume_grid_fuel", text="Fuel")
            grids.prop(props, "volume_grid_burn", text="Burn")
            grids.prop(props, "volume_grid_velocity", text="Velocity")
    elif props.participant_type == "collection_points":
        box.prop(props, "source_collection")
        box.prop(props, "collection_point_center")
        box.prop(props, "collection_radius_mode")
        if props.collection_radius_mode == "bounds":
            box.prop(props, "collection_radius_scale")
        else:
            box.prop(props, "point_radius", text="Radius")
        box.label(text="One sphere per mesh")
        box.label(text="Automatic motion velocity")
        box.label(text="Source meshes need no Fumaris role")
    elif props.participant_type == "collection_mesh":
        box.prop(props, "source_collection")
        _draw_mesh_emitter_options(box, props)
        box.label(text="Combined geometry, shared settings")
        box.label(text="Source meshes need no Fumaris role")


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
    box.label(text="Emitter", icon="OUTLINER_OB_FORCE_FIELD")
    box.prop(props, "participant_enabled")
    _draw_emitter_shape(box, obj, props)

    box = layout.box()
    box.label(text="Emitted Channels", icon="MOD_FLUID")
    box.prop(props, "emitter_influence")
    box.prop(props, "emitter_burning")
    if props.emitter_burning:
        box.prop(props, "emitter_burn_rate")
    smoke_row = box.row()
    smoke_row.active = not props.emitter_influence
    smoke_row.prop(props, "emitter_smoke")
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
    box.label(text="Velocity", icon="FORCE_FORCE")
    box.prop(props, "velocity")
    if (props.participant_type in {"mesh", "collection_mesh"}
            or (props.participant_type == "particles" and props.particle_subtype == "mesh")
            or (props.participant_type == "geometry_nodes" and props.gn_subtype == "mesh")
            or (props.participant_type in {"sphere", "box"} and props.emitter_settings_version >= 1)
            or (props.participant_type in {"particles", "collection_points", "geometry_nodes"}
                and props.emitter_settings_version >= 2)):
        box.prop(props, "normal_velocity")
    box.prop(props, "motion_velocity_scale")

    box = layout.box()
    column, opened = _nested_foldout(box, props, "show_emitter_coupling", "Channel Coupling")
    if opened:
        for channel in ("smoke", "temperature", "fuel", "burn"):
            row = column.row(align=True)
            row.prop(props, f"couple_rate_{channel}")
            row.prop(props, f"source_op_{channel}", text="")
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
    text = f"Memory in use: {used / gib:.1f} of {allocated / gib:.1f} GB ({fraction:.0%})"
    if not state.get("active"):
        text = "Last run · " + text
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
        title.label(text="GPU memory full — simulation slowed", icon="ERROR")
        box.label(text="Lower VRAM or free")
        box.label(text="GPU memory, then restart.")
    status = diagnostics.capacity_status(state)
    if not status:
        return
    box = layout.column(align=True)
    title = box.row(align=True)
    title.alert = True
    if status == "full":
        title.label(text="Capacity reached — smoke may clip", icon="ERROR")
    else:
        title.label(text="Capacity almost full — smoke may clip", icon="ERROR")
    box.label(text="Increase VRAM")
    box.label(text="or lower Resolution, then restart.")
    if status == "full" and state and not state.get("active"):
        box.label(text="Last run", icon="INFO")


def _draw_domain(layout, scene, props):
    job = active_job()
    mode = active_mode()
    is_baking = mode == "baking"
    is_previewing = mode == "previewing"
    is_preview_paused = is_previewing and bool(
        getattr(job, "_pause_requested", False)
    )
    is_initializing = job is not None and not getattr(job, "_accepted", False)

    controls = layout.box().column(align=True)
    controls.label(text="Simulate", icon="PHYSICS")
    prefs = get_addon_preferences()
    if prefs:
        gpu = controls.row(align=True)
        gpu.enabled = mode is None
        gpu.prop(prefs, "simulation_gpu", text="GPU")
        gpu.operator("fumaris.refresh_gpus", text="", icon='FILE_REFRESH')
    slot_directory = peek_output_directory_for_object(props.id_data)
    slot_protected = is_protected(slot_directory)
    row = controls.row(align=True)
    sub = row.row(align=True)
    sub.enabled = mode is None and not slot_protected
    sub.operator("fumaris.bake", icon="RENDER_ANIMATION", text="Bake")
    stop = row.row(align=True)
    stop.enabled = is_baking
    stop.operator("fumaris.stop", icon="CANCEL", text="Stop")
    delete = row.row(align=True)
    delete.enabled = mode is None and not slot_protected
    delete.operator("fumaris.delete", icon="TRASH", text="Delete")
    if slot_protected:
        controls.label(text=f"Cache slot '{props.cache_slot}' is protected", icon="LOCKED")

    controls.separator(factor=0.6)
    header = controls.row(align=True)
    header.label(text="Preview Controls")
    if is_previewing:
        header.label(text="Viewport preview approximates the Cycles render")
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
    # A preview that is still starting (or paused, or finishing a recording) can always be stopped.
    preview_stop.enabled = job is not None and not is_baking
    preview_stop.operator("fumaris.preview_stop", icon="CANCEL", text="Stop")
    controls.separator(factor=0.5)
    options = controls.row(align=True)
    record = options.row(align=True)
    record.enabled = mode is None
    record.prop(props, "bake_on_preview")
    controls.separator(factor=0.5)
    # Keep transport rows together; all status text lives below their options.
    status = controls.column(align=True)
    failure = diagnostics.error_summary(diagnostics.snapshot(props.id_data))
    if failure:
        error_row = status.row(align=True)
        error_row.alert = True
        error_row.label(text=failure, icon="ERROR")
        status.label(text="See Diagnostics below for details.")
    elif is_initializing:
        status.label(text="Initializing Flow GPU…", icon="INFO")
        status.label(text="First-run shaders can take several minutes")
    elif is_baking:
        status.label(text="Baking…", icon="TIME")
    elif mode is None and props.simulation_state in {"baked", "stopped"}:
        completed = props.simulation_state == "baked"
        status.label(
            text=_bake_summary(props, "Baked" if completed else "Stopped"),
            icon="CHECKMARK" if completed else "PAUSE",
        )
        if completed:
            # Why Play is greyed out.
            status.label(text="Delete the bake or pick a new cache slot to preview again")
    elif is_previewing and not getattr(job, "_recording_enabled", False):
        status.label(text="Live preview · Paused" if is_preview_paused else "Live preview",
                     icon="PAUSE" if is_preview_paused else "PLAY")
    if is_previewing and getattr(job, "_recording_enabled", False):
        if getattr(job, "_recording_flush_pending", False) or getattr(job, "_recording_stop_requested", False):
            status.label(text="Finishing recorded frames…", icon="TIME")
        elif getattr(job, "_recording_showing_cache", False):
            first = getattr(job, "_progress_start_frame", 1)
            last = getattr(job, "_recording_committed_end", -1)
            status.label(text=f"Recorded frames {first}–{last} · Paused", icon="CHECKMARK")
            if getattr(job, "_recording_at_end", False):
                status.label(text="Resume starts a new recording")
        else:
            status.label(text="Recording preview to VDB", icon="REC")
    domain = props.id_data
    if not domain.visible_get(view_layer=bpy.context.view_layer):
        status.label(text="Domain hidden: simulation can still run", icon="INFO")
        status.label(text="Unhide the domain to see the preview")

    _draw_capacity_warning(status, domain)
    _draw_memory_usage(status, domain)

    settings = controls.column(align=True)
    settings.enabled = not is_baking
    row = settings.row(align=True)
    row.prop(props, "use_scene_frame_range", text="", icon="TIME")
    if props.use_scene_frame_range:
        row.prop(props, "warmup_frames")
    else:
        row.prop(props, "sim_start_frame", text="Start")
        row.prop(props, "sim_end_frame", text="End")
    grid_row = settings.row(align=True)
    grid_row.prop(props, "resolution")
    memory_field = grid_row.row(align=True)
    memory_field.active = not props.simulation_memory_auto
    memory_field.prop(props, "simulation_memory", text="VRAM (GB)")
    auto = grid_row.row(align=True)
    auto.ui_units_x = 2.2
    auto.prop(props, "simulation_memory_auto", text="Auto", toggle=True)
    if simulation_memory_mb(props) == 0.0 and not props.simulation_memory_auto:
        legacy = settings.row()
        legacy.label(text=f"Using saved block capacity {props.sparse_block_capacity:,}", icon="INFO")
    else:
        draw_budget_summary(settings, props, diagnostics.snapshot(domain))
    settings.prop(props, "air_margin")
    substeps_row = settings.row(align=True)
    substeps_row.prop(props, "num_sub_steps", text="Max Sub-Steps" if props.adaptive_cfl > 0.0 else "Sub-Steps")
    substeps_row.prop(props, "adaptive_cfl")

    appearance, appearance_open = _nested_foldout(
        settings,
        props,
        "show_appearance",
        "Appearance",
    )
    if appearance_open:
        appearance.label(text="Smoke")
        appearance.prop(props, "shader_smoke_density")
        color = appearance.split(factor=0.2, align=True)
        color.label(text="Color")
        color.prop(props, "shader_smoke_color", text="")
        appearance.prop(props, "shader_anisotropy")
        appearance.separator()
        appearance.prop(props, "shader_flame_enabled")
        fire = appearance.column(align=True)
        fire.enabled = props.shader_flame_enabled
        fire.prop(props, "shader_flame_brightness")
        fire.prop(props, "shader_glow_temperature")
        fire.prop(props, "shader_flame_contrast")
        fire.prop(props, "shader_color_spread")
        fire.prop(props, "shader_temperature_multiplier")
        tint = fire.split(factor=0.4, align=True)
        tint.label(text="Flame Tint")
        tint.prop(props, "shader_flame_tint", text="")
        fire.prop(props, "shader_scatter_glow")
        glow_spread = fire.row(align=True)
        glow_spread.enabled = props.shader_scatter_glow > 0.0
        glow_spread.prop(props, "shader_glow_spread", slider=True)
        glow_shadowing = fire.row(align=True)
        glow_shadowing.enabled = props.shader_scatter_glow > 0.0
        glow_shadowing.prop(props, "shader_glow_shadowing")
        if (props.shader_scatter_glow > 0.0 and scene.render.engine == "CYCLES"
                and not getattr(getattr(scene, "cycles", None), "volume_biased", True)):
            # Cycles' unbiased volumes drop the glow in square patches
            # (2026-11-06); Biased renders it correctly and faster.
            hint = appearance.row(align=True)
            hint.label(text="Scatter Glow shows square patches in unbiased Cycles volumes", icon="ERROR")
            hint.operator("fumaris.use_biased_volumes", text="Use Biased")
        if props.volume_material is not None and not props.volume_material.get("fumaris_generated_material"):
            appearance.label(
                text="Custom Volume Material overrides baked appearance",
                icon="INFO",
            )

    output, output_open = _nested_foldout(settings, props, "show_cache_location", "Output")
    if output_open:
        # One label column for every row, so labels line up and none truncate.
        split = output.split(factor=0.2, align=True)
        labels = split.column(align=True)
        fields = split.column(align=True)
        labels.label(text="Cache Slot")
        slot = fields.row(align=True)
        slot.prop(props, "cache_slot", text="")
        slot.operator("fumaris.cache_slot_new", icon="ADD", text="")
        slot.operator("fumaris.cache_slot_restore_settings", icon="LOOP_BACK", text="")
        slot.operator("fumaris.cache_make_lighter", icon="MOD_DECIM", text="")
        slot.operator("fumaris.add_sparks", icon="PARTICLES", text="")
        slot.operator(
            "fumaris.cache_protect",
            icon="LOCKED" if slot_protected else "UNLOCKED",
            text="",
            depress=slot_protected,
        )
        labels.label(text="Directory")
        fields.prop(props, "output_dir", text="")
        labels.label(text="Prefix")
        fields.prop(props, "output_prefix", text="")
        labels.label(text="VDB")
        vdb = fields.row(align=True)
        vdb.prop(props, "vdb_compression", text="")
        vdb.prop(props, "vdb_precision", text="")
        labels.label(text="Material")
        fields.prop(props, "volume_material", text="")
        output.prop(props, "low_memory_bake")
        output.separator()
        output.prop(props, "volume_selectable")
        output.separator()
        temperature = output.row(align=True)
        temperature.prop(props, "export_temperature_vdb")
        half = temperature.row(align=True)
        half.active = props.export_temperature_vdb
        half.prop(props, "export_temperature_half_res", text="Half Res", toggle=True)
        output.prop(props, "export_fuel_vdb")
        output.prop(props, "export_burn_vdb")
        velocity = output.row(align=True)
        velocity.prop(props, "export_velocity_vdb")
        half = velocity.row(align=True)
        half.active = props.export_velocity_vdb
        half.prop(props, "export_velocity_half_res", text="Half Res", toggle=True)
        output.prop(props, "export_glow_vdb")
        if props.export_glow_vdb:
            output.prop(props, "glow_radius")
        if props.volume_material is None:
            output.label(
                text="Generated material always includes temperature",
                icon="INFO",
            )

    preview, preview_open = _nested_foldout(settings, props, "show_preview_display", "Preview Settings")
    if preview_open:
        preview.prop(props, "preview_field_view")

        preview.label(text="Quality")
        quality = preview.column(align=True)
        sharpness = quality.row(align=True)
        sharpness.prop(props, "preview_resolution_percent", text="Simulation", slider=True)
        sharpness.prop(props, "preview_image_scale", text="Image", slider=True)
        steps = quality.row(align=True)
        steps.prop(props, "preview_step_size")
        steps.prop(props, "preview_all_viewports", toggle=True)

        preview.label(text="Lighting")
        lighting = preview.column(align=True)
        lighting.row(align=True).prop(props, "preview_lighting", expand=True)
        if props.preview_lighting == "CUSTOM":
            direction = lighting.row(align=True)
            direction.prop(props, "preview_light_azimuth")
            direction.prop(props, "preview_light_elevation")
            lighting.prop(props, "preview_light_strength")
        toggles = lighting.row(align=True)
        toggles.prop(props, "preview_shadows", toggle=True)
        toggles.prop(props, "preview_occlusion", toggle=True)
        fire = lighting.row(align=True)
        fire.enabled = props.shader_flame_enabled
        fire.prop(props, "preview_fire_lighting", slider=True)

        preview.label(text="Color")
        color = preview.row(align=True)
        color.prop(props, "preview_tone_mapping", text="")
        color.prop(props, "preview_exposure_offset", slider=True)
        preview.label(text="Play/Pause keeps live Flow data in VRAM", icon="INFO")

    participants = layout.box()
    participants.enabled = not is_baking
    participants.label(text="Participants", icon="OUTLINER_COLLECTION")
    participants = participants.column(align=True)
    participants.prop(props, "emitter_collection")
    participants.prop(props, "collider_collection")
    participants.prop(props, "effector_collection")
    participants.prop(props, "outflow_collection")
    participants.operator_menu_enum("fumaris.add_source", "kind", text="Add Source", icon="ADD")
    _draw_participant_list(participants, props)

    behavior_box = layout.box()
    behavior_box.enabled = not is_baking
    behavior_box.label(text="Smoke Behavior", icon="MOD_FLUID")
    behavior = behavior_box.column(align=True)
    # One split for both rows so their X / Y / Z columns line up; the Gravity
    # from Scene button sits at the left edge (a blank slot under it for Wind).
    for name, label, button in (("gravity", "Gravity", True), ("wind", "Wind", False)):
        split = behavior.split(factor=0.25, align=True)
        left = split.row(align=True)
        if button:
            left.operator("fumaris.gravity_from_scene", text="", icon="SCENE_DATA")
        else:
            left.label(text="", icon="BLANK1")
        left.label(text=label)
        split.row(align=True).prop(props, name, text="")
    behavior.separator()
    buoyancy = behavior.row(align=True)
    buoyancy.prop(props, "buoyancy_per_temp")
    buoyancy.prop(props, "buoyancy_per_smoke")
    behavior.separator()
    fade = behavior.row(align=True)
    fade.prop(props, "dissipation")
    fade.prop(props, "smoke_decay", text="Decay")
    behavior.prop(props, "velocity_damping")
    behavior.separator()
    vorticity_row = behavior.row(align=True)
    vorticity_row.prop(props, "vorticity")
    mask = vorticity_row.row(align=True)
    mask.active = props.vorticity > 0.0
    mask.prop(props, "vorticity_mask", text="")
    behavior.separator()

    turbulence, turbulence_open = _nested_foldout(behavior, props, "show_domain_turbulence", "Turbulence Detail")
    if turbulence_open:
        turbulence.label(text="Disturb")
        disturb_row = turbulence.row(align=True)
        disturb_row.prop(props, "disturb")
        size = disturb_row.row(align=True)
        size.active = props.disturb > 0.0
        size.prop(props, "disturb_size", text="Size")
        detail = turbulence.row(align=True)
        detail.active = props.disturb > 0.0
        detail.prop(props, "disturb_detail", text="Detail")
        detail.prop(props, "disturb_min_speed", text="Min Speed")
        turbulence.separator()
        turbulence.label(text="Grain")
        grain_row = turbulence.row(align=True)
        grain_row.prop(props, "grain")
        grain_size = grain_row.row(align=True)
        grain_size.active = props.grain > 0.0
        grain_size.prop(props, "grain_size", text="Size")
        grain_edge = turbulence.row(align=True)
        grain_edge.active = props.grain > 0.0
        grain_edge.prop(props, "grain_edge", text="Edges Only")
        grain_extra = turbulence.row(align=True)
        grain_extra.active = props.grain > 0.0
        grain_extra.prop(props, "grain_min_speed", text="Min Speed")
        grain_extra.prop(props, "grain_layers", text="Layers")
        turbulence.separator()
        turbulence.prop(props, "shred")

    combustion, combustion_open = _nested_foldout(behavior, props, "show_domain_combustion", "Combustion")
    if combustion_open:
        combustion.prop(props, "flame_temperature")
        combustion.prop(props, "fuel_energy")
        combustion.prop(props, "ignition_temperature_k")
        combustion.separator()
        combustion.prop(props, "burn_time")
        combustion.prop(props, "cooling_time")
        combustion.prop(props, "smoke_per_burn", text="Smoke Amount")
        combustion.separator()
        combustion.prop(props, "gas_expansion")
        shaping = combustion.row(align=True)
        shaping.prop(props, "wisp_cooling")
        shaping.prop(props, "fuel_threshold")
        air = combustion.row(align=True)
        air.prop(props, "oxygen_limit")
        air.prop(props, "soot_radiation")
        oxygen = combustion.row(align=True)
        oxygen.active = props.oxygen_limit > 0.0
        oxygen.prop(props, "oxygen_field", text="", icon="FORCE_WIND", toggle=True)
        field = oxygen.row(align=True)
        field.active = props.oxygen_field and props.oxygen_limit > 0.0
        field.prop(props, "oxygen_use", text="Use")
        field.prop(props, "oxygen_mixing", text="Mixing")

    advanced_box = layout.box()
    advanced_box.enabled = not is_baking
    if _foldout(advanced_box, props, "show_panel_advanced", "Advanced"):
        # Rarely touched. What a shot is tuned with lives with Resolution above.
        advanced = advanced_box.column(align=True)
        # Solver
        advanced.prop(props, "simulation_speed")
        advanced.prop(props, "advection_reflection")
        detail = advanced.row(align=True)
        detail.prop(props, "smoke_detail", text="Smoke Detail")
        detail.prop(props, "edge_sharpness", text="Edges")
        advanced.prop(props, "projection_filter", slider=True)
        advanced.prop(props, "full_resolution_velocity")
        advanced.separator()
        # Moving domain
        advanced.prop(props, "follow_object")
        if props.follow_object:
            advanced.prop(props, "carry_air", slider=True)
        advanced.separator()
        # Colliders
        advanced.prop(props, "physics_convex_collision")
        walls = advanced.row(align=True)
        walls.prop(props, "open_collider_wall")
        walls.prop(props, "closed_collider_margin")
        advanced.separator()
        # Memory
        advanced.prop(props, "auto_cell_size")
        advanced.prop(props, "sparse_block_min_lifetime")
        allocation = advanced.row(align=True)
        allocation.prop(props, "allocation_smoke_threshold")
        allocation.prop(props, "allocation_speed_threshold")

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
        layout = self.layout
        obj = context.object
        props = obj.fumaris

        header = layout.box()
        if props.smoke_object_type != "domain":
            header.enabled = active_mode() != "baking"
        header.label(text=obj.name, icon="OBJECT_DATA")
        header.prop(props, "smoke_object_type", text="Flow Object")
        if props.smoke_object_type != "none":
            presets = header.row(align=True)
            presets.menu("FUMARIS_MT_presets", text="Presets", icon="PRESET")
            presets.operator("fumaris.preset_save", text="", icon="ADD")
        if props.smoke_object_type == "none":
            setup = header.row()
            setup.enabled = active_job() is None
            setup.operator("fumaris.quick_setup", text="Quick Setup…", icon="ADD")

        if props.smoke_object_type == "domain":
            _draw_domain(layout, context.scene, props)
        else:
            from .playback import draw_controls, scene_domains
            if active_job() or scene_domains(context):
                draw_controls(layout.box(), context)
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
        for name in names:
            row = layout.row(align=True)
            row.operator("fumaris.preset_apply", text=name).name = name
            row.operator("fumaris.preset_delete", text="", icon="X").name = name


CLASSES = (FUMARIS_MT_presets, FUMARIS_PT_main)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
