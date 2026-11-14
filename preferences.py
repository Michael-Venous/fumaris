import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty, StringProperty

from .utils import runtime_validation_error, executable_path, process_environment
from .devices import discover_devices
from .memory_auto import update_readings as update_memory_readings


_devices = []
_items = []  # Blender keeps references to enum strings.
_item_strings = {}  # Retain generated strings across enum callback/getter calls.
_discovery_error = ""


def _enum_string(value):
    return _item_strings.setdefault(value, value)


def gpu_items(self, context):
    global _items
    _items = [("AUTO", "Automatic", "Prefer a discrete GPU; honor launch environment overrides", 0)]
    for device in _devices:
        _items.append((_enum_string(device["uuid"]), _enum_string(device["name"]),
                       _enum_string(device["type"] + " GPU · " + device["uuid"]), len(_items)))
    if self.gpu_device_uuid and not any(d["uuid"] == self.gpu_device_uuid for d in _devices):
        _items.append((_enum_string(self.gpu_device_uuid), _enum_string(self.gpu_device_name or "Saved GPU"),
                       "Saved GPU; refresh to check availability", len(_items)))
    return _items


def get_gpu(self):
    return next((item[3] for item in gpu_items(self, None) if item[0] == self.gpu_device_uuid), 0)


def set_gpu(self, value):
    item = next(item for item in gpu_items(self, None) if item[3] == value)
    self.gpu_device_uuid = "" if item[0] == "AUTO" else item[0]
    self.gpu_device_name = "" if item[0] == "AUTO" else item[1]
    # Saved with the preferences when Blender exits (Auto-Save Preferences).
    bpy.context.preferences.is_dirty = True


def _preferences_changed(_self, context):
    # Saved with the preferences when Blender exits (Auto-Save Preferences).
    context.preferences.is_dirty = True


class FumarisBakeGpu(bpy.types.PropertyGroup):
    """A GPU background bakes may use: saved by UUID, like the Simulation GPU."""
    uuid: StringProperty()
    name: StringProperty()
    use: BoolProperty(
        name="Use for Background Bakes",
        description="Background bakes take the ticked GPUs in turn. With none ticked they use the Simulation GPU",
        default=False, update=_preferences_changed,
    )


def _sync_bake_gpus(prefs):
    """After Refresh: a row for every GPU found; a GPU that is gone keeps its
    row only while it is ticked (shown as unavailable)."""
    found = {device["uuid"]: device["name"] for device in _devices}
    for index in reversed(range(len(prefs.bake_gpus))):
        gpu = prefs.bake_gpus[index]
        if gpu.uuid in found:
            gpu.name = found.pop(gpu.uuid)
        elif not gpu.use:
            prefs.bake_gpus.remove(index)
    for uuid, name in found.items():
        gpu = prefs.bake_gpus.add()
        gpu.uuid, gpu.name = uuid, name


class FUMARIS_OT_refresh_gpus(bpy.types.Operator):
    bl_idname = "fumaris.refresh_gpus"
    bl_label = "Refresh GPUs"
    bl_description = "List Vulkan GPUs without starting a simulation"

    def execute(self, context):
        global _devices, _discovery_error
        try:
            error = runtime_validation_error()
            if error:
                raise RuntimeError(error)
            executable = executable_path()
            _devices = discover_devices(executable, process_environment(executable))
            update_memory_readings(_devices)
            _discovery_error = "" if _devices else "No selectable Vulkan GPUs found"
            addon = context.preferences.addons.get(__package__)
            if addon is not None:
                _sync_bake_gpus(addon.preferences)
        except Exception as error:
            _devices = []
            _discovery_error = str(error)
            self.report({'ERROR'}, _discovery_error)
            return {'CANCELLED'}
        return {'FINISHED'}


class FumarisPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    executable_path: StringProperty(
        name="Bridge Executable",
        description="Path to a custom Fumaris bridge executable. Leave empty to use the bundled one",
        subtype="FILE_PATH",
    )

    gpu_device_uuid: StringProperty(name="Simulation GPU UUID", default="", options={'HIDDEN'})
    gpu_device_name: StringProperty(name="Simulation GPU name", default="", options={'HIDDEN'})
    simulation_gpu: EnumProperty(
        name="Simulation GPU", items=gpu_items, get=get_gpu, set=set_gpu,
        description="GPU the next Preview or Bake runs on; kept in Blender's preferences",
    )

    bake_workers: IntProperty(
        name="Background Bakes",
        description="How many domains Bake in Background and Bake All bake at once, each in its own Blender process. More domains wait their turn",
        default=1, min=1, max=8, update=_preferences_changed,
    )
    bake_gpus: CollectionProperty(type=FumarisBakeGpu)

    default_output_dir: StringProperty(
        name="Default Cache Directory",
        description="Cache directory new domains start with, in any scene. Setting a domain's Cache Directory updates it; empty uses a fumaris_cache folder next to the .blend file",
        default="", subtype="DIR_PATH",
    )

    preview_cache_memory_gb: FloatProperty(
        name="Preview Cache RAM",
        description="Most system memory a cached live preview keeps its frames in (compressed, full detail). 0 uses a quarter of the RAM, from 1 to 16 GB. When it is full the oldest frames are dropped, so the cache holds the most recent part of the preview. Whatever this is set to, the cache also drops its oldest frames when the system is short of free memory (it leaves 2 GB or an eighth of the RAM, whichever is more, free)",
        default=0.0, min=0.0, soft_max=64.0, step=100, precision=1, unit="NONE",
    )

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        box = layout.box()
        box.label(text="Simulation GPU", icon='MEMORY')
        row = box.row(align=True)
        row.prop(self, "simulation_gpu", text="GPU")
        row.operator("fumaris.refresh_gpus", text="", icon='FILE_REFRESH')
        if _discovery_error:
            box.label(text=_discovery_error, icon='ERROR')
        elif self.gpu_device_uuid and _devices and not any(d["uuid"] == self.gpu_device_uuid for d in _devices):
            box.label(text="Saved GPU unavailable; choose another GPU or Automatic", icon='ERROR')
        elif not _devices:
            box.label(text="Press Refresh to list the GPUs", icon='INFO')

        box = layout.box()
        box.label(text="Baking", icon='RENDER_ANIMATION')
        box.prop(self, "bake_workers")
        gpus = box.column(heading="On GPUs", align=True)
        for gpu in self.bake_gpus:
            row = gpus.row(align=True)
            row.prop(gpu, "use", text=gpu.name or "Saved GPU")
            if gpu.use and _devices and not any(d["uuid"] == gpu.uuid for d in _devices):
                row.label(text="unavailable", icon='ERROR')
        ticked = sum(1 for gpu in self.bake_gpus if gpu.use)
        if not self.bake_gpus:
            box.label(text="Press Refresh (above) to choose GPUs; without a choice, bakes use the Simulation GPU", icon='INFO')
        elif not ticked:
            box.label(text="None ticked: background bakes use the Simulation GPU", icon='INFO')
        if self.bake_workers > max(1, ticked):
            hint = box.column(align=True)
            hint.label(text="More bakes than GPUs: several bakes share a GPU.", icon='INFO')
            hint.label(text="Give each domain a fixed VRAM (GB) instead of Auto.", icon='BLANK1')

        box = layout.box()
        box.label(text="Cache", icon='FILE_FOLDER')
        box.prop(self, "default_output_dir")
        box.prop(self, "preview_cache_memory_gb", text="Preview Cache RAM (0 = Auto)")

        from .playback import draw_keymaps
        box = layout.box()
        box.label(text="Live Preview Shortcuts", icon='KEYINGSET')
        draw_keymaps(box, context)

        box = layout.box()
        box.label(text="Runtime", icon='SETTINGS')
        error = runtime_validation_error()
        status = box.row()
        status.alert = bool(error)
        status.label(
            text=error or "Bundled bridge and Flow runtime found",
            icon="ERROR" if error else "CHECKMARK",
        )
        if not error:
            # Cached loader check (Linux only): missing libraries, old glibc,
            # Vulkan drivers. Reading ELF headers, nothing is run.
            from .linux_runtime import runtime_diagnostics
            executable = executable_path()
            linux = runtime_diagnostics(executable, process_environment(executable))
            if linux is not None:
                row = box.row()
                row.alert = not linux["ok"]
                row.label(text=f"Linux runtime: {linux['summary']}",
                          icon="CHECKMARK" if linux["ok"] and not linux["warnings"] else "ERROR" if not linux["ok"] else "INFO")
        box.prop(self, "executable_path", text="Custom Bridge")


def register():
    try:
        bpy.utils.unregister_class(FumarisPreferences)
    except RuntimeError:
        pass
    try:
        bpy.utils.unregister_class(FumarisBakeGpu)
    except RuntimeError:
        pass
    bpy.utils.register_class(FumarisBakeGpu)
    bpy.utils.register_class(FumarisPreferences)
    bpy.utils.register_class(FUMARIS_OT_refresh_gpus)


def unregister():
    bpy.utils.unregister_class(FUMARIS_OT_refresh_gpus)
    try:
        bpy.utils.unregister_class(FumarisPreferences)
    except RuntimeError:
        pass
    try:
        bpy.utils.unregister_class(FumarisBakeGpu)
    except RuntimeError:
        pass
