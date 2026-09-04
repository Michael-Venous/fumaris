import glob
import os
import shutil
import time

import bpy
try:
    import openvdb
except ImportError:
    openvdb = None

from .diagnostics import console_log

PREFIX = "fumaris_"
LEGACY_PREFIX = "plume_forge_"
IMPORT_DIRECTORY = ".fumaris_import"
VOLUME_MARKER = "fumaris_volume"
VOLUME_PREFIX_MARKER = "fumaris_volume_prefix"
LEGACY_IMPORT_DIRECTORY = ".plume_forge_import"
LEGACY_VOLUME_MARKER = "plume_forge_volume"
LEGACY_VOLUME_PREFIX_MARKER = "plume_forge_volume_prefix"
GENERATED_MATERIAL_MARKER = "fumaris_generated_material"

NODE_VOLUME_SHADER = "Fumaris Volume Shader"
NODE_FIRE_INTENSITY = "Fumaris Fire Intensity"
NODE_FIRE_TEMPERATURE = "Fumaris Fire Temperature"
NODE_TEMPERATURE_MULTIPLIER = "Fumaris Color Temperature"
NODE_TIMESTEP_NORMALIZATION = "Fumaris Timestep Normalization"


def hide_generated_volumes(directory, prefix=PREFIX):
    """Hide this cache's imported volume and return enough state to restore it."""
    normalized = os.path.normpath(directory)
    hidden = []
    for obj in bpy.data.objects:
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        hidden.append((obj.name, bool(obj.hide_viewport)))
        obj.hide_viewport = True
        obj.update_tag()
    return hidden


def restore_generated_volumes(directory, prefix, states):
    normalized = os.path.normpath(directory)
    for name, was_hidden in states:
        obj = bpy.data.objects.get(name)
        if obj is None or not _is_generated_volume(obj, normalized, prefix):
            continue
        obj.hide_viewport = was_hidden
        obj.update_tag()


def delete_generated_data(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if (
            obj.get(VOLUME_MARKER) == normalized
            or obj.get(LEGACY_VOLUME_MARKER) == normalized
        ):
            volume = obj.data if obj.type == "VOLUME" else None
            bpy.data.objects.remove(obj, do_unlink=True)
            if volume and volume.users == 0:
                bpy.data.volumes.remove(volume)

    if os.path.isdir(directory):
        for candidate_prefix in {prefix, LEGACY_PREFIX}:
            for path in glob.glob(
                os.path.join(directory, f"{candidate_prefix}*.vdb")
            ):
                os.remove(path)
            delete_temporary_writes(directory, candidate_prefix)
        _delete_import_copies(directory)


def delete_temporary_writes(directory, prefix=PREFIX):
    for path in glob.glob(os.path.join(directory, f"{prefix}*.vdb.tmp")):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def migrate_legacy_cache(legacy_directory, directory, prefix=PREFIX):
    legacy_directory = os.path.normpath(legacy_directory)
    directory = os.path.normpath(directory)
    if legacy_directory == directory:
        return False
    legacy_files = sorted(
        glob.glob(os.path.join(legacy_directory, f"{prefix}*.vdb"))
    )
    source_prefix = prefix
    if not legacy_files and prefix != LEGACY_PREFIX:
        legacy_files = sorted(
            glob.glob(
                os.path.join(legacy_directory, f"{LEGACY_PREFIX}*.vdb")
            )
        )
        source_prefix = LEGACY_PREFIX
    if not legacy_files:
        return False
    if glob.glob(os.path.join(directory, f"{prefix}*.vdb")):
        return False

    os.makedirs(directory, exist_ok=True)
    for source in legacy_files:
        name = os.path.basename(source)
        if source_prefix != prefix:
            name = prefix + name[len(source_prefix):]
        os.replace(source, os.path.join(directory, name))
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
    enabled = bool(_appearance_value(settings, "shader_flame_enabled", True))
    brightness = max(
        0.0,
        float(_appearance_value(settings, "shader_flame_brightness", 1.0)),
    )
    fire_intensity.inputs[1].default_value = brightness if enabled else 0.0
    start_temperature = max(
        0.0,
        float(_appearance_value(settings, "flame_temperature_min", 800.0)),
    )
    full_temperature = max(
        start_temperature + 1e-5,
        float(_appearance_value(settings, "flame_temperature_max", 3000.0)),
    )
    fire_temperature.inputs[1].default_value = start_temperature
    fire_temperature.inputs[2].default_value = full_temperature
    temperature_multiplier.inputs[1].default_value = max(
        0.01,
        float(_appearance_value(settings, "shader_temperature_multiplier", 1.0)),
    )
    material.diffuse_color = (*smoke_color[:3], 1.0)
    material.node_tree.update_tag()
    return True


def add_density_material(volume_object, settings=None, flame_rate_scale=1.0):
    material = bpy.data.materials.new("Fumaris Material")
    material.use_nodes = True
    material[GENERATED_MATERIAL_MARKER] = True
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
    fire_intensity.use_clamp = True
    fire_intensity.inputs[1].default_value = 1.0

    burn.location = (-1080, 300)
    positive_burn.location = (-880, 300)
    volume_info.location = (-1080, -120)
    fire_temperature.location = (-880, 80)
    burn_heat.location = (-650, 300)
    timestep.location = (-450, 300)
    negate.location = (-250, 300)
    exponential.location = (-50, 300)
    smooth_flame.location = (150, 300)
    fire_intensity.location = (350, 300)
    temperature_multiplier.location = (-650, -120)
    blackbody.location = (-430, -120)
    shader.location = (590, 40)
    output.location = (850, 40)

    links.new(_socket(burn.outputs, "Factor", "Fac"), positive_burn.inputs[0])
    links.new(positive_burn.outputs[0], burn_heat.inputs[0])
    links.new(volume_info.outputs["Temperature"], fire_temperature.inputs[0])
    links.new(_socket(fire_temperature.outputs, "Result"), burn_heat.inputs[1])
    links.new(burn_heat.outputs[0], timestep.inputs[0])
    links.new(timestep.outputs[0], negate.inputs[0])
    links.new(negate.outputs[0], exponential.inputs[0])
    links.new(exponential.outputs[0], smooth_flame.inputs[1])
    links.new(smooth_flame.outputs[0], fire_intensity.inputs[0])
    links.new(volume_info.outputs["Temperature"], temperature_multiplier.inputs[0])
    links.new(temperature_multiplier.outputs[0], blackbody.inputs["Temperature"])
    links.new(blackbody.outputs["Color"], shader.inputs["Emission Color"])
    links.new(fire_intensity.outputs[0], shader.inputs["Emission Strength"])
    shader.inputs["Density Attribute"].default_value = "density"
    shader.inputs["Temperature Attribute"].default_value = "temperature"
    shader.inputs["Blackbody Intensity"].default_value = 0.0
    links.new(shader.outputs["Volume"], output.inputs["Volume"])
    apply_generated_material_settings(material, settings)
    volume_object.data.materials.append(material)


def update_generated_materials(domain, settings):
    cache_id = str(getattr(settings, "cache_id", "")).strip()
    if not cache_id:
        return 0
    from .utils import output_directory_for_object

    directory = os.path.normpath(output_directory_for_object(domain))
    prefix = str(getattr(settings, "output_prefix", PREFIX))
    updated = 0
    for obj in bpy.data.objects:
        if not _is_generated_volume(obj, directory, prefix):
            continue
        for material in obj.data.materials:
            if material and material.get(GENERATED_MATERIAL_MARKER):
                updated += int(apply_generated_material_settings(material, settings))
    return updated


def import_sequence(
    directory,
    frame_start,
    prefix=PREFIX,
    material=None,
    selectable=True,
    appearance=None,
    flame_rate_scale=1.0,
):
    files = sorted(glob.glob(os.path.join(directory, f"{prefix}*.vdb")))
    if not files:
        raise RuntimeError("The bridge completed without writing VDB files")
    files = _files_with_grids(files)
    if not files:
        raise RuntimeError("The bridge wrote VDB files, but none contained readable grids")

    previous_active = bpy.context.view_layer.objects.active
    previous_selection = list(bpy.context.selected_objects)
    _remove_volume_for_directory(directory, prefix)
    import_directory, import_files = _stage_import_sequence(directory, files)
    existing = set(bpy.data.objects)
    bpy.ops.object.volume_import(
        filepath=import_files[0],
        files=[{"name": os.path.basename(path)} for path in import_files],
        directory=import_directory,
        use_sequence_detection=True,
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
    sequence_start = _frame_number(files[0], prefix) or frame_start
    _configure_volume(volume_object, import_files[0], len(import_files), sequence_start)
    volume_object.select_set(False)
    volume_object.hide_select = not bool(selectable)
    if material is not None:
        volume_object.data.materials.append(material)
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


def _frame_number(path, prefix):
    name = os.path.basename(path)
    if not name.startswith(prefix) or not name.endswith(".vdb"):
        return None
    try:
        return int(name[len(prefix):-4])
    except ValueError:
        return None


def _stage_import_sequence(directory, files):
    root = os.path.join(directory, IMPORT_DIRECTORY)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, str(time.time_ns()))
    os.makedirs(target, exist_ok=False)

    staged = []
    for path in files:
        destination = os.path.join(target, os.path.basename(path))
        try:
            os.symlink(path, destination)
        except OSError:
            shutil.copy2(path, destination)
        staged.append(destination)

    _prune_import_copies(root, target)
    return target, staged


def _delete_import_copies(directory):
    for name in (IMPORT_DIRECTORY, LEGACY_IMPORT_DIRECTORY):
        root = os.path.join(directory, name)
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
    volume.filepath = bpy.path.relpath(filepath) if bpy.data.filepath else filepath
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
    try:
        if current_frame != load_frame:
            scene.frame_set(load_frame)
        volume.grids.load()
    finally:
        if scene.frame_current != current_frame:
            scene.frame_set(current_frame)
    if len(volume.grids) == 0:
        volume.grids.load()
    if any(grid.name == "velocity" for grid in volume.grids):
        volume.velocity_grid = "velocity"
        volume.velocity_unit = "SECOND"
    volume.update_tag()
    volume_object.update_tag()


def _remove_volume_for_directory(directory, prefix=PREFIX):
    normalized = os.path.normpath(directory)
    for obj in list(bpy.data.objects):
        if not _is_generated_volume(obj, normalized, prefix):
            continue
        volume = obj.data if obj.type == "VOLUME" else None
        bpy.data.objects.remove(obj, do_unlink=True)
        if volume and volume.users == 0:
            bpy.data.volumes.remove(volume)


def _is_generated_volume(obj, directory, prefix):
    marker = obj.get(VOLUME_MARKER) or obj.get(LEGACY_VOLUME_MARKER)
    if marker:
        marker_prefix = obj.get(
            VOLUME_PREFIX_MARKER,
            obj.get(LEGACY_VOLUME_PREFIX_MARKER, prefix),
        )
        return (
            os.path.normpath(marker) == directory
            and marker_prefix in {prefix, LEGACY_PREFIX}
        )
    if obj.type != "VOLUME":
        return False
    filepath = getattr(obj.data, "filepath", "")
    if not filepath:
        return False
    absolute = os.path.normpath(bpy.path.abspath(filepath))
    import_roots = (
        os.path.normpath(os.path.join(directory, IMPORT_DIRECTORY)),
        os.path.normpath(os.path.join(directory, LEGACY_IMPORT_DIRECTORY)),
    )
    try:
        in_import_root = any(
            os.path.commonpath((absolute, root)) == root
            for root in import_roots
        )
    except ValueError:
        in_import_root = False
    return (
        (os.path.dirname(absolute) == directory or in_import_root)
        and os.path.basename(absolute).startswith((prefix, LEGACY_PREFIX))
    )
