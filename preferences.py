import bpy
from bpy.props import EnumProperty, FloatProperty, StringProperty

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
        description="Optional path to a custom Fumaris bridge executable",
        subtype="FILE_PATH",
    )

    gpu_device_uuid: StringProperty(name="Simulation GPU UUID", default="", options={'HIDDEN'})
    gpu_device_name: StringProperty(name="Simulation GPU name", default="", options={'HIDDEN'})
    simulation_gpu: EnumProperty(
        name="Simulation GPU", items=gpu_items, get=get_gpu, set=set_gpu,
        description="GPU for new Preview and Bake jobs; saved in Blender preferences",
    )

    preview_cache_mode: EnumProperty(
        name="Preview Cache",
        description="Whether the live preview keeps its frames, so a paused preview can scrub, play back and export them",
        items=(
            ("BALANCED", "Balanced", "As many frames as copying them to system memory allows in about a fifth of the frame time, spread evenly so playback stays smooth: small scenes keep most frames, large ones every few (fewer on slower PCIe links)"),
            ("EVERY", "Every Frame", "Every frame, whatever the cost: copying a frame takes about 40 ms per GB on a PCIe 4.0 x16 link and several times that on narrower ones, so large scenes preview slower"),
            ("OFF", "Off", "No cache: nothing to scrub or export"),
        ),
        default="BALANCED",
    )

    preview_cache_memory_gb: FloatProperty(
        name="Preview Cache RAM",
        description="System memory the live preview keeps its frames in, so a paused preview can scrub and play back what it simulated (compressed, full detail). 0 uses a quarter of the RAM, 1 to 16 GB. When full, every other older frame is dropped",
        default=0.0, min=0.0, soft_max=64.0, step=100, precision=1, unit="NONE",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "executable_path")
        box = layout.box()
        row = box.row(align=True)
        row.prop(self, "simulation_gpu")
        row.operator("fumaris.refresh_gpus", text="", icon='FILE_REFRESH')
        box.label(text="Applies to the next Preview or Bake. Save Preferences to keep this choice.")
        cache = layout.row(align=True)
        cache.prop(self, "preview_cache_mode")
        memory = cache.row(align=True)
        memory.enabled = self.preview_cache_mode != "OFF"
        memory.prop(self, "preview_cache_memory_gb", text="RAM (GB, 0 = Auto)")
        if not _devices:
            box.label(text="Refresh GPUs to list available devices", icon='INFO')
        if self.gpu_device_uuid and _devices and not any(d["uuid"] == self.gpu_device_uuid for d in _devices):
            box.label(text="Saved GPU unavailable; choose another GPU or Automatic", icon='ERROR')
        if _discovery_error:
            box.label(text=_discovery_error, icon='ERROR')
        from .playback import draw_keymaps
        draw_keymaps(layout.box(), context)
        error = runtime_validation_error()
        status = layout.row()
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
                row = layout.row()
                row.alert = not linux["ok"]
                row.label(text=f"Linux runtime: {linux['summary']}",
                          icon="CHECKMARK" if linux["ok"] and not linux["warnings"] else "ERROR" if not linux["ok"] else "INFO")


def register():
    try:
        bpy.utils.unregister_class(FumarisPreferences)
    except RuntimeError:
        pass
    bpy.utils.register_class(FumarisPreferences)
    bpy.utils.register_class(FUMARIS_OT_refresh_gpus)


def unregister():
    bpy.utils.unregister_class(FUMARIS_OT_refresh_gpus)
    try:
        bpy.utils.unregister_class(FumarisPreferences)
    except RuntimeError:
        pass
