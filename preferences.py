import bpy
from bpy.props import StringProperty

from .utils import runtime_validation_error


class FumarisPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    executable_path: StringProperty(
        name="Bridge Executable",
        description="Optional path to a custom Fumaris bridge executable",
        subtype="FILE_PATH",
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "executable_path")
        error = runtime_validation_error()
        status = layout.row()
        status.alert = bool(error)
        status.label(
            text=error or "Bundled bridge and Flow runtime found",
            icon="ERROR" if error else "CHECKMARK",
        )


def register():
    try:
        bpy.utils.unregister_class(FumarisPreferences)
    except RuntimeError:
        pass
    bpy.utils.register_class(FumarisPreferences)


def unregister():
    try:
        bpy.utils.unregister_class(FumarisPreferences)
    except RuntimeError:
        pass
