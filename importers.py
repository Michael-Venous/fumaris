import os
import re
import shutil
import time

import bpy
try:
    import openvdb
except ImportError:
    openvdb = None

from .diagnostics import console_log
from .utils import WIEN_KELVIN, flame_temperature_range

PREFIX = "fumaris_"
_FRAME_DIGITS = re.compile(r"-?[0-9]+")
IMPORT_DIRECTORY = ".fumaris_import"
VOLUME_MARKER = "fumaris_volume"
VOLUME_PREFIX_MARKER = "fumaris_volume_prefix"
GENERATED_MATERIAL_MARKER = "fumaris_generated_material"
PREVIEW_OWNER_MARKER = "fumaris_preview_owner"

NODE_VOLUME_SHADER = "Fumaris Volume Shader"
NODE_FIRE_INTENSITY = "Fumaris Fire Intensity"
NODE_FIRE_TEMPERATURE = "Fumaris Fire Temperature"
NODE_TEMPERATURE_MULTIPLIER = "Fumaris Color Temperature"
NODE_TIMESTEP_NORMALIZATION = "Fumaris Timestep Normalization"
NODE_FLAME_SHARPNESS = "Fumaris Flame Sharpness"
# Measured 2026-11-02: flame source of burning cells in an explosion had
# median 0.02, 90th percentile 0.47, 99th 0.83.
FLAME_SHARPNESS_PIVOT = 0.5
NODE_FLAME_CORE = "Fumaris Flame Core"
# Temperature (Physical) look: soot absorbs, and by Kirchhoff emits in
# proportion, with radiance from Wien's law at 550 nm,
# exp(-c * 26160 K * (1/T - 1/Tref)), so gas at Flame Temperature glows at
# Brightness and cooler gas falls off as it does on a camera exposed for the
# hottest gas. Colour is blackbody, shifted and saturated like a camera's
# clipped highlights (plain blackbody orange turns peach under AgX).
NODE_FLAME_LOOK_STRENGTH = "Fumaris Flame Look Strength"
NODE_FLAME_LOOK_COLOR = "Fumaris Flame Look Color"
NODE_PHYSICAL_REFERENCE = "Fumaris Physical Reference"
NODE_PHYSICAL_CONTRAST = "Fumaris Physical Contrast"
NODE_PHYSICAL_GAIN = "Fumaris Physical Gain"
NODE_PHYSICAL_COLOR_SCALE = "Fumaris Physical Color Scale"
PHYSICAL_COLOR_SHIFT = 1.3
PHYSICAL_SATURATION = 1.8


def hide_generated_volumes(directory, prefix=PREFIX, *, hide_render=False):
    """Hide this cache's imported volume and return enough state to restore it."""
    normalized = os.path.normpath(directory)
    hidden = []
    for obj in bpy.data.objects:
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        hidden.append(
            (obj.name, bool(obj.hide_viewport), bool(obj.hide_render))
            if hide_render else (obj.name, bool(obj.hide_viewport))
        )
        obj.hide_viewport = True
        if hide_render:
            obj.hide_render = True
        obj.update_tag()
    return hidden


def restore_generated_volumes(directory, prefix, states):
    normalized = os.path.normpath(directory)
    for state in states:
        name, was_hidden = state[:2]
        obj = bpy.data.objects.get(name)
        if obj is None or not _is_generated_volume(obj, normalized, prefix):
            continue
        obj.hide_viewport = was_hidden
        if len(state) > 2:
            obj.hide_render = state[2]
        obj.update_tag()


def hide_recorded_previews(owner, except_volume=None):
    """Only one recorded take for this domain should be displayed at a time."""
    states = []
    for obj in bpy.data.objects:
        if obj == except_volume or obj.get(PREVIEW_OWNER_MARKER) != owner:
            continue
        states.append((obj.name, bool(obj.hide_viewport), bool(obj.hide_render)))
        obj.hide_viewport = obj.hide_render = True
        obj.update_tag()
    return states


def replace_recorded_previews(owner, volume):
    """Replace prior display objects, retaining their recorded files on disk."""
    for obj in list(bpy.data.objects):
        if (obj != volume and obj.type == "VOLUME"
                and obj.get(PREVIEW_OWNER_MARKER) == owner
                and obj.get(VOLUME_MARKER)):
            _remove_generated_volume_object(obj)


def restore_recorded_previews(states):
    for name, viewport, render in states or ():
        obj = bpy.data.objects.get(name)
        if obj is not None:
            obj.hide_viewport = viewport
            obj.hide_render = render
            obj.update_tag()


def baked_frame_count(directory, prefix=PREFIX):
    """Frames a bake has written to this cache slot folder."""
    return len(_frame_files(directory, prefix))


def show_only_slot_volumes(root, current):
    """Show the current slot's baked volume and hide the other slots' volumes.

    Every slot of a domain lives below `root`. Their imported volumes stay in
    the scene so switching back is instant; without this they would overlap.
    """
    root = os.path.normpath(root) + os.sep
    current = os.path.normpath(current)
    for obj in bpy.data.objects:
        marker = obj.get(VOLUME_MARKER)
        if not marker:
            continue
        marker = os.path.normpath(marker)
        if not marker.startswith(root):
            continue
        if marker == current:
            hidden = False
        elif marker.startswith(current + os.sep):
            continue  # this slot's preview recordings keep their own state
        else:
            hidden = True
        if obj.hide_viewport != hidden or obj.hide_render != hidden:
            obj.hide_viewport = hidden
            obj.hide_render = hidden
            obj.update_tag()


def delete_generated_data(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if obj.get(VOLUME_MARKER) == normalized:
            _remove_generated_volume_object(obj)

    if os.path.isdir(directory):
        for path in _frame_files(directory, prefix):
            os.remove(path)
        delete_temporary_writes(directory, prefix)
        _delete_import_copies(directory)


def delete_temporary_writes(directory, prefix=PREFIX):
    for path in _frame_files(directory, prefix, ".vdb.tmp"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def migrate_legacy_cache(legacy_directory, directory, prefix=PREFIX):
    legacy_directory = os.path.normpath(legacy_directory)
    directory = os.path.normpath(directory)
    if legacy_directory == directory:
        return False
    legacy_files = _frame_files(legacy_directory, prefix)
    if not legacy_files:
        return False
    if _frame_files(directory, prefix):
        return False

    os.makedirs(directory, exist_ok=True)
    for source in legacy_files:
        os.replace(source, os.path.join(directory, os.path.basename(source)))
    _remove_volume_for_directory(legacy_directory, prefix)
    console_log(
        f"Fumaris migrated {len(legacy_files)} legacy VDB frame(s) to "
        f"{directory}"
    )
    return True


def _socket(sockets, *names):
    for name in names:
        socket = sockets.get(name)
        if socket is not None:
            return socket
    raise KeyError(f"Missing node socket: {', '.join(names)}")


def _appearance_value(settings, name, default):
    return getattr(settings, name, default) if settings is not None else default


def apply_generated_material_settings(material, settings):
    if material is None or not material.use_nodes or material.node_tree is None:
        return False
    nodes = material.node_tree.nodes
    # Materials generated before Sharpness and Blue Core lack their nodes:
    # rebuild the graph in place, keeping its timestep normalization.
    timestep = nodes.get(NODE_TIMESTEP_NORMALIZATION)
    if timestep is not None and (nodes.get(NODE_FLAME_SHARPNESS) is None
                                 or nodes.get(NODE_FLAME_LOOK_STRENGTH) is None):
        _build_generated_material(material, timestep.inputs[1].default_value)
        nodes = material.node_tree.nodes
    shader = nodes.get(NODE_VOLUME_SHADER)
    fire_intensity = nodes.get(NODE_FIRE_INTENSITY)
    fire_temperature = nodes.get(NODE_FIRE_TEMPERATURE)
    temperature_multiplier = nodes.get(NODE_TEMPERATURE_MULTIPLIER)
    if not all((shader, fire_intensity, fire_temperature, temperature_multiplier)):
        return False

    smoke_color = tuple(
        float(value)
        for value in _appearance_value(
            settings,
            "shader_smoke_color",
            (0.6, 0.6, 0.6),
        )
    )
    shader.inputs["Color"].default_value = (*smoke_color[:3], 1.0)
    shader.inputs["Density"].default_value = max(
        0.0,
        float(_appearance_value(settings, "shader_smoke_density", 2.0)),
    )
    shader.inputs["Anisotropy"].default_value = max(
        -0.95, min(0.95, float(_appearance_value(settings, "shader_anisotropy", 0.3))))
    enabled = bool(_appearance_value(settings, "shader_flame_enabled", True))
    brightness = max(
        0.0,
        float(_appearance_value(settings, "shader_flame_brightness", 1.0)),
    )
    fire_intensity.inputs[1].default_value = brightness if enabled else 0.0
    fire_intensity.use_clamp = False
    start_temperature, full_temperature = flame_temperature_range(
        float(_appearance_value(settings, "flame_temperature_min", 800.0)),
        float(_appearance_value(settings, "flame_temperature_max", 3000.0)),
    )
    fire_temperature.inputs[1].default_value = start_temperature
    fire_temperature.inputs[2].default_value = full_temperature
    temperature_multiplier.inputs[1].default_value = max(
        0.01,
        float(_appearance_value(settings, "shader_temperature_multiplier", 1.0)),
    )
    # Materials generated before these controls lack the nodes; they keep
    # the soft, all-blackbody flame.
    sharpness = nodes.get(NODE_FLAME_SHARPNESS)
    if sharpness is not None:
        sharpness.inputs[1].default_value = max(
            0.25, float(_appearance_value(settings, "shader_flame_sharpness", 2.0)))
    core = nodes.get(NODE_FLAME_CORE)
    if core is not None:
        core.inputs[1].default_value = max(
            0.0, min(1.0, float(_appearance_value(settings, "shader_flame_core", 0.0))))
    look_strength = nodes.get(NODE_FLAME_LOOK_STRENGTH)
    look_color = nodes.get(NODE_FLAME_LOOK_COLOR)
    if look_strength is not None and look_color is not None:
        # Fire always glows by temperature (the Burn look was removed).
        for mix in (look_strength, look_color):
            mix.inputs["Factor"].default_value = 1.0
        reference = max(1.0, float(_appearance_value(settings, "shader_glow_temperature", 1600.0)))
        contrast = max(0.05, float(_appearance_value(settings, "shader_flame_contrast", 0.5)))
        nodes[NODE_PHYSICAL_REFERENCE].inputs[0].default_value = reference
        nodes[NODE_PHYSICAL_CONTRAST].inputs[1].default_value = -contrast * WIEN_KELVIN / reference
        density = shader.inputs["Density"].default_value
        nodes[NODE_PHYSICAL_GAIN].inputs[1].default_value = (brightness * density) if enabled else 0.0
        nodes[NODE_PHYSICAL_COLOR_SCALE].inputs[1].default_value = PHYSICAL_COLOR_SHIFT * max(
            0.01, float(_appearance_value(settings, "shader_temperature_multiplier", 1.0)))
    material.diffuse_color = (*smoke_color[:3], 1.0)
    material.node_tree.update_tag()
    return True


def add_density_material(volume_object, settings=None, flame_rate_scale=1.0):
    material = bpy.data.materials.new("Fumaris Material")
    material.use_nodes = True
    material[GENERATED_MATERIAL_MARKER] = True
    _build_generated_material(material, flame_rate_scale)
    apply_generated_material_settings(material, settings)
    volume_object.data.materials.append(material)


def _build_generated_material(material, flame_rate_scale):
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()

    volume_info = nodes.new("ShaderNodeVolumeInfo")
    burn = nodes.new("ShaderNodeAttribute")
    burn.attribute_name = "burn"
    burn.label = "Raw Burn"
    positive_burn = nodes.new("ShaderNodeMath")
    positive_burn.operation = "MAXIMUM"
    positive_burn.inputs[1].default_value = 0.0
    positive_burn.label = "Positive Burn"
    fire_temperature = nodes.new("ShaderNodeMapRange")
    fire_temperature.data_type = "FLOAT"
    fire_temperature.interpolation_type = "SMOOTHSTEP"
    fire_temperature.name = NODE_FIRE_TEMPERATURE
    fire_temperature.label = "Flame Temperature"
    fire_temperature.inputs[3].default_value = 0.0
    fire_temperature.inputs[4].default_value = 1.0
    burn_heat = nodes.new("ShaderNodeMath")
    burn_heat.operation = "MULTIPLY"
    burn_heat.label = "Burn x Heat"
    timestep = nodes.new("ShaderNodeMath")
    timestep.operation = "MULTIPLY"
    timestep.name = NODE_TIMESTEP_NORMALIZATION
    timestep.label = "Timestep Normalization"
    timestep.inputs[1].default_value = max(0.0, float(flame_rate_scale))
    negate = nodes.new("ShaderNodeMath")
    negate.operation = "MULTIPLY"
    negate.inputs[1].default_value = -1.0
    exponential = nodes.new("ShaderNodeMath")
    exponential.operation = "EXPONENT"
    smooth_flame = nodes.new("ShaderNodeMath")
    smooth_flame.operation = "SUBTRACT"
    smooth_flame.inputs[0].default_value = 1.0
    smooth_flame.label = "Smooth Flame"
    shader = nodes.new("ShaderNodeVolumePrincipled")
    shader.name = NODE_VOLUME_SHADER
    output = nodes.new("ShaderNodeOutputMaterial")
    blackbody = nodes.new("ShaderNodeBlackbody")
    temperature_multiplier = nodes.new("ShaderNodeMath")
    temperature_multiplier.operation = "MULTIPLY"
    temperature_multiplier.name = NODE_TEMPERATURE_MULTIPLIER
    temperature_multiplier.label = "Blackbody Scale"
    fire_intensity = nodes.new("ShaderNodeMath")
    fire_intensity.operation = "MULTIPLY"
    fire_intensity.name = NODE_FIRE_INTENSITY
    fire_intensity.label = "Fire Intensity"
    fire_intensity.use_clamp = False
    fire_intensity.inputs[1].default_value = 1.0
    sharpness = nodes.new("ShaderNodeMath")
    sharpness.operation = "POWER"
    sharpness.name = NODE_FLAME_SHARPNESS
    sharpness.label = "Flame Sharpness"
    sharpness.inputs[1].default_value = 1.0
    # Contrast around a typical strong-flame source (most burning cells are
    # far below 1), so Sharpness thins weak flame and brightens cores
    # instead of dimming everything: source' = k * (source / k)^Sharpness.
    pivot_in = nodes.new("ShaderNodeMath")
    pivot_in.operation = "DIVIDE"
    pivot_in.inputs[1].default_value = FLAME_SHARPNESS_PIVOT
    pivot_in.label = "Sharpness Pivot"
    pivot_out = nodes.new("ShaderNodeMath")
    pivot_out.operation = "MULTIPLY"
    pivot_out.inputs[1].default_value = FLAME_SHARPNESS_PIVOT
    pivot_out.label = "Sharpness Pivot"
    # Blue where fuel is still burning: fresh flame near the source.
    fuel = nodes.new("ShaderNodeAttribute")
    fuel.attribute_name = "fuel"
    fuel.label = "Fuel"
    fuel_presence = nodes.new("ShaderNodeMath")
    fuel_presence.operation = "MULTIPLY"
    fuel_presence.use_clamp = True
    fuel_presence.inputs[1].default_value = 4.0
    fuel_presence.label = "Fuel Presence"
    # Blue only where fuel is still near ignition temperature.
    cool = nodes.new("ShaderNodeMath")
    cool.operation = "SUBTRACT"
    cool.inputs[0].default_value = 1.0
    cool.label = "Near Ignition"
    fresh_fuel = nodes.new("ShaderNodeMath")
    fresh_fuel.operation = "MULTIPLY"
    fresh_fuel.label = "Fresh Fuel"
    core = nodes.new("ShaderNodeMath")
    core.operation = "MULTIPLY"
    core.use_clamp = True
    core.name = NODE_FLAME_CORE
    core.label = "Blue Core"
    core.inputs[1].default_value = 0.0
    core_mix = nodes.new("ShaderNodeMix")
    core_mix.data_type = "RGBA"
    core_mix.label = "Core Color"
    for socket in core_mix.inputs:
        if socket.name == "B" and socket.type == "RGBA":
            socket.default_value = (0.12, 0.3, 1.0, 1.0)

    burn.location = (-1080, 300)
    positive_burn.location = (-880, 300)
    volume_info.location = (-1080, -120)
    fire_temperature.location = (-880, 80)
    burn_heat.location = (-650, 300)
    timestep.location = (-450, 300)
    negate.location = (-250, 300)
    exponential.location = (-50, 300)
    smooth_flame.location = (150, 300)
    pivot_in.location = (-450, 520)
    sharpness.location = (-350, 420)
    pivot_out.location = (-250, 520)
    fire_intensity.location = (350, 300)
    fuel.location = (-650, -320)
    fuel_presence.location = (-430, -320)
    cool.location = (-650, -480)
    fresh_fuel.location = (-430, -480)
    core.location = (-230, -320)
    core_mix.location = (-200, -120)
    temperature_multiplier.location = (-650, -120)
    blackbody.location = (-430, -120)
    shader.location = (590, 40)
    output.location = (850, 40)

    links.new(_socket(burn.outputs, "Factor", "Fac"), positive_burn.inputs[0])
    links.new(positive_burn.outputs[0], burn_heat.inputs[0])
    links.new(volume_info.outputs["Temperature"], fire_temperature.inputs[0])
    links.new(_socket(fire_temperature.outputs, "Result"), burn_heat.inputs[1])
    links.new(burn_heat.outputs[0], timestep.inputs[0])
    links.new(timestep.outputs[0], pivot_in.inputs[0])
    links.new(pivot_in.outputs[0], sharpness.inputs[0])
    links.new(sharpness.outputs[0], pivot_out.inputs[0])
    links.new(pivot_out.outputs[0], negate.inputs[0])
    links.new(negate.outputs[0], exponential.inputs[0])
    links.new(exponential.outputs[0], smooth_flame.inputs[1])
    links.new(smooth_flame.outputs[0], fire_intensity.inputs[0])
    links.new(volume_info.outputs["Temperature"], temperature_multiplier.inputs[0])
    links.new(temperature_multiplier.outputs[0], blackbody.inputs["Temperature"])
    links.new(_socket(fuel.outputs, "Factor", "Fac"), fuel_presence.inputs[0])
    links.new(_socket(fire_temperature.outputs, "Result"), cool.inputs[1])
    links.new(fuel_presence.outputs[0], fresh_fuel.inputs[0])
    links.new(cool.outputs[0], fresh_fuel.inputs[1])
    links.new(fresh_fuel.outputs[0], core.inputs[0])
    links.new(core.outputs[0], core_mix.inputs["Factor"])
    rgba_inputs = [socket for socket in core_mix.inputs if socket.type == "RGBA"]
    links.new(blackbody.outputs["Color"], rgba_inputs[0])
    rgba_outputs = [socket for socket in core_mix.outputs if socket.type == "RGBA"]

    # Temperature (Physical) path; the look mixes pick it or the burn flame.
    def math_node(operation, location, label, name=None):
        node = nodes.new("ShaderNodeMath")
        node.operation = operation
        node.label = label
        node.location = location
        if name:
            node.name = name
        return node

    floor = math_node("MAXIMUM", (-880, -700), "Temperature Floor")
    floor.inputs[1].default_value = 300.0
    reference = math_node("DIVIDE", (-680, -700), "Flame Temperature / T", NODE_PHYSICAL_REFERENCE)
    reference.inputs[0].default_value = 2000.0
    offset = math_node("SUBTRACT", (-480, -700), "Below Flame")
    offset.inputs[1].default_value = 1.0
    contrast = math_node("MULTIPLY", (-280, -700), "Wien Contrast", NODE_PHYSICAL_CONTRAST)
    contrast.inputs[1].default_value = -WIEN_KELVIN / 2000.0
    radiance = math_node("EXPONENT", (-80, -700), "Radiance")
    absorbed = math_node("MULTIPLY", (120, -700), "Soot Emission")
    gain = math_node("MULTIPLY", (320, -700), "Brightness x Density", NODE_PHYSICAL_GAIN)
    color_scale = math_node("MULTIPLY", (-680, -900), "Camera Color Shift", NODE_PHYSICAL_COLOR_SCALE)
    color_scale.inputs[1].default_value = PHYSICAL_COLOR_SHIFT
    physical_blackbody = nodes.new("ShaderNodeBlackbody")
    physical_blackbody.location = (-480, -900)
    saturation = nodes.new("ShaderNodeHueSaturation")
    saturation.location = (-280, -900)
    saturation.inputs["Saturation"].default_value = PHYSICAL_SATURATION
    look_strength = nodes.new("ShaderNodeMix")
    look_strength.data_type = "FLOAT"
    look_strength.name = NODE_FLAME_LOOK_STRENGTH
    look_strength.label = "Flame Look"
    look_strength.location = (420, 200)
    look_color = nodes.new("ShaderNodeMix")
    look_color.data_type = "RGBA"
    look_color.name = NODE_FLAME_LOOK_COLOR
    look_color.label = "Flame Look Color"
    look_color.location = (420, -60)

    links.new(volume_info.outputs["Temperature"], floor.inputs[0])
    links.new(floor.outputs[0], reference.inputs[1])
    links.new(reference.outputs[0], offset.inputs[0])
    links.new(offset.outputs[0], contrast.inputs[0])
    links.new(contrast.outputs[0], radiance.inputs[0])
    links.new(radiance.outputs[0], absorbed.inputs[0])
    links.new(volume_info.outputs["Density"], absorbed.inputs[1])
    links.new(absorbed.outputs[0], gain.inputs[0])
    links.new(volume_info.outputs["Temperature"], color_scale.inputs[0])
    links.new(color_scale.outputs[0], physical_blackbody.inputs["Temperature"])
    links.new(physical_blackbody.outputs["Color"], saturation.inputs["Color"])
    float_inputs = [socket for socket in look_strength.inputs if socket.type == "VALUE" and socket.name in ("A", "B")]
    links.new(fire_intensity.outputs[0], float_inputs[0])
    links.new(gain.outputs[0], float_inputs[1])
    color_inputs = [socket for socket in look_color.inputs if socket.type == "RGBA"]
    links.new(rgba_outputs[0], color_inputs[0])
    links.new(saturation.outputs["Color"], color_inputs[1])
    links.new(_socket(look_strength.outputs, "Result"), shader.inputs["Emission Strength"])
    color_outputs = [socket for socket in look_color.outputs if socket.type == "RGBA"]
    links.new(color_outputs[0], shader.inputs["Emission Color"])
    shader.inputs["Density Attribute"].default_value = "density"
    shader.inputs["Temperature Attribute"].default_value = "temperature"
    shader.inputs["Blackbody Intensity"].default_value = 0.0
    links.new(shader.outputs["Volume"], output.inputs["Volume"])


def update_generated_materials(domain, settings):
    updated = 0
    seen = set()
    # A generated material picked as the domain's Volume Material is still
    # Fumaris's own graph: keep it following Appearance too.
    custom = getattr(settings, "volume_material", None)
    if custom is not None and custom.get(GENERATED_MATERIAL_MARKER):
        updated += int(apply_generated_material_settings(custom, settings))
        seen.add(custom.name)
    cache_id = str(getattr(settings, "cache_id", "")).strip()
    if not cache_id:
        return updated
    from .utils import output_directory_for_object

    directory = os.path.normpath(output_directory_for_object(domain))
    prefix = str(getattr(settings, "output_prefix", PREFIX))
    for obj in bpy.data.objects:
        if not _is_generated_volume(obj, directory, prefix):
            continue
        for material in obj.data.materials:
            if material and material.get(GENERATED_MATERIAL_MARKER) and material.name not in seen:
                updated += int(apply_generated_material_settings(material, settings))
                seen.add(material.name)
    return updated


def import_sequence(
    directory,
    frame_start,
    prefix=PREFIX,
    material=None,
    selectable=True,
    appearance=None,
    flame_rate_scale=1.0,
    frame_end=None,
):
    files = _sequence_files(directory, prefix)
    if frame_end is not None:
        files = [path for path in files if frame_start <= _frame_number(path, prefix) <= frame_end]
        expected = list(range(frame_start, frame_end + 1))
        if [_frame_number(path, prefix) for path in files] != expected:
            raise RuntimeError("Recorded preview has missing committed VDB frames")
    if not files:
        raise RuntimeError("The bridge completed without writing VDB files")
    readable = _files_with_grids(files)
    if frame_end is not None and len(readable) != len(files):
        raise RuntimeError("Recorded preview contains an unreadable committed VDB frame")
    files = readable
    if not files:
        raise RuntimeError("The bridge wrote VDB files, but none contained readable grids")

    previous_active = bpy.context.view_layer.objects.active
    previous_selection = list(bpy.context.selected_objects)
    _remove_volume_for_directory(directory, prefix)
    import_directory, import_files = _stage_import_sequence(directory, files, prefix)
    existing = set(bpy.data.objects)
    bpy.ops.object.volume_import(
        filepath=import_files[0],
        files=[{"name": os.path.basename(import_files[0])}],
        directory=import_directory,
        use_sequence_detection=False,
    )

    created = [obj for obj in bpy.data.objects if obj not in existing and obj.type == "VOLUME"]
    if not created:
        _restore_selection(previous_selection, previous_active)
        raise RuntimeError("Blender did not create a Volume object")

    volume_object = created[0]
    volume_object.name = "Fumaris Volume"
    volume_object.location = (0.0, 0.0, 0.0)
    volume_object[VOLUME_MARKER] = os.path.normpath(directory)
    volume_object[VOLUME_PREFIX_MARKER] = prefix
    sequence_start = _frame_number(files[0], prefix)
    sequence_end = _frame_number(files[-1], prefix)
    _configure_volume(
        volume_object,
        import_files[0],
        sequence_end - sequence_start + 1,
        sequence_start,
    )
    volume_object.select_set(False)
    volume_object.hide_select = not bool(selectable)
    if material is not None:
        volume_object.data.materials.append(material)
        if material.get(GENERATED_MATERIAL_MARKER):
            apply_generated_material_settings(material, appearance)
    else:
        add_density_material(
            volume_object,
            settings=appearance,
            flame_rate_scale=flame_rate_scale,
        )
    _restore_selection(previous_selection, previous_active)
    return volume_object


def _files_with_grids(files):
    started = time.perf_counter()
    valid = []
    skipped = []
    for path in files:
        try:
            if _grid_metadata(path):
                valid.append(path)
        except Exception:
            skipped.append(path)
    if skipped:
        console_log(
            f"Fumaris skipped {len(skipped)} unreadable VDB frame(s): "
            + ", ".join(os.path.basename(path) for path in skipped)
        )
    console_log(
        "Fumaris VDB metadata validation: "
        f"{len(valid)}/{len(files)} frames in "
        f"{(time.perf_counter() - started) * 1000.0:.3f}ms"
    )
    return valid


def _grid_metadata(path):
    if openvdb is not None:
        return openvdb.readAllGridMetadata(path)
    volume = bpy.data.volumes.new("Fumaris VDB Probe")
    try:
        volume.filepath = path
        volume.grids.load()
        return tuple(volume.grids)
    finally:
        bpy.data.volumes.remove(volume)


def _frame_number(path, prefix, suffix=".vdb"):
    name = os.path.basename(path)
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    digits = name[len(prefix):len(name) - len(suffix)]
    if not _FRAME_DIGITS.fullmatch(digits):
        return None
    return int(digits)


def _frame_files(directory, prefix, suffix=".vdb"):
    """Bridge-written frame files only; other files sharing the prefix are the user's."""
    try:
        with os.scandir(directory) as entries:
            numbered = [
                (frame, entry.path)
                for entry in entries
                if (frame := _frame_number(entry.name, prefix, suffix)) is not None
            ]
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [path for _frame, path in sorted(numbered)]


def _sequence_files(directory, prefix):
    return _frame_files(directory, prefix)


def _stage_import_sequence(directory, files, prefix=PREFIX):
    first_frame = _frame_number(files[0], prefix)
    if first_frame is None:
        raise RuntimeError("Cannot stage an unnumbered Fumaris VDB frame")
    root = os.path.join(directory, IMPORT_DIRECTORY)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, str(time.time_ns()))
    os.makedirs(target, exist_ok=False)

    staged = []
    try:
        for path in files:
            frame = _frame_number(path, prefix)
            if frame is None:
                raise RuntimeError("Cannot stage an unnumbered Fumaris VDB frame")
            # Blender treats a minus sign as part of the sequence prefix.
            # Positive import-only indices keep warmup frames and gaps intact.
            filename = f"{prefix}{frame - first_frame + 1:04d}.vdb"
            destination = os.path.join(target, filename)
            try:
                os.symlink(os.path.abspath(path), destination)
            except OSError:
                try:
                    # Windows normally needs extra permission for symlinks.
                    # Cache frames are published by replacement, so hard links
                    # also preserve the imported version without copying it.
                    os.link(path, destination)
                except OSError:
                    shutil.copy2(path, destination)
            staged.append(destination)
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise

    _prune_import_copies(root, target)
    return target, staged


def _delete_import_copies(directory):
    root = os.path.join(directory, IMPORT_DIRECTORY)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


def _prune_import_copies(root, keep):
    keep = os.path.normpath(keep)
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.normpath(path) == keep:
            continue
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)


def _restore_selection(selected, active):
    for obj in bpy.context.selected_objects:
        obj.select_set(False)
    for obj in selected:
        if obj.name in bpy.data.objects:
            obj.select_set(True)
    if active and active.name in bpy.data.objects:
        bpy.context.view_layer.objects.active = active


def _configure_volume(volume_object, filepath, frame_count, frame_start):
    volume = volume_object.data
    volume.filepath = filepath
    if bpy.data.filepath:
        try:
            volume.filepath = bpy.path.relpath(filepath)
        except ValueError:
            # Windows cannot express a path on another drive relative to the file.
            pass
    volume.is_sequence = True
    volume.frame_offset = 0
    volume.frame_start = frame_start
    volume.frame_duration = frame_count
    volume.sequence_mode = "EXTEND"
    volume.display.density = 1.0
    volume.display.use_slice = False
    volume.display.interpolation_method = "LINEAR"
    volume.render.space = "OBJECT"
    volume.render.step_size = 0.0
    scene = bpy.context.scene
    current_frame = scene.frame_current
    load_frame = frame_start + max(0, frame_count - 1)
    grid_names = ()
    try:
        if current_frame != load_frame:
            scene.frame_set(load_frame)
        # Sequence frame selection lives on evaluated Volume data. Loading
        # the original datablock can try frame 0 (a nonexistent import file),
        # especially when the scene already sits at this take's last frame.
        volume.update_tag()
        volume_object.update_tag()
        bpy.context.view_layer.update()
        depsgraph = bpy.context.evaluated_depsgraph_get()
        evaluated_volume = volume_object.evaluated_get(depsgraph).data
        evaluated_volume.grids.load()
        grid_names = tuple(grid.name for grid in evaluated_volume.grids)
    finally:
        if scene.frame_current != current_frame:
            scene.frame_set(current_frame)
    # OpenVDB's read order can be alphabetical even when density is written
    # first. Select the smoke field for newly imported volumes; otherwise burn
    # may be selected and make a successful bake look empty in Solid shading.
    for index, name in enumerate(grid_names):
        if name == "density":
            volume.grids.active_index = index
            break
    if "velocity" in grid_names:
        volume.velocity_grid = "velocity"
        volume.velocity_unit = "SECOND"
    volume.update_tag()
    volume_object.update_tag()


def _remove_volume_for_directory(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        _remove_generated_volume_object(obj)


def _remove_generated_volume_object(obj):
    volume = obj.data if obj.type == "VOLUME" else None
    materials = tuple(volume.materials) if volume else ()
    bpy.data.objects.remove(obj, do_unlink=True)
    if volume and volume.users == 0:
        bpy.data.volumes.remove(volume)
    # Repeated pause imports replace the volume. Only discard our now-orphan
    # materials; keep user materials, shared materials and explicit fake users.
    for material in dict.fromkeys(materials):
        if (material is not None and material.get(GENERATED_MATERIAL_MARKER)
                and material.users == 0 and not material.use_fake_user):
            bpy.data.materials.remove(material)


def _is_generated_volume(obj, directory, prefix):
    marker = obj.get(VOLUME_MARKER)
    if marker:
        return (
            os.path.normpath(marker) == directory
            and obj.get(VOLUME_PREFIX_MARKER, prefix) == prefix
        )
    if obj.type != "VOLUME":
        return False
    filepath = getattr(obj.data, "filepath", "")
    if not filepath:
        return False
    absolute = os.path.normpath(bpy.path.abspath(filepath))
    import_root = os.path.normpath(os.path.join(directory, IMPORT_DIRECTORY))
    try:
        in_import_root = os.path.commonpath((absolute, import_root)) == import_root
    except ValueError:
        in_import_root = False
    return (
        (os.path.dirname(absolute) == directory or in_import_root)
        and os.path.basename(absolute).startswith(prefix)
    )
