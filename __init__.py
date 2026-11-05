bl_info = {
    "name": "Fumaris",
    "author": "Fumaris",
    "version": (1, 4, 0),
    "blender": (5, 2, 0),
    "location": "Properties > Physics",
    "description": "Interactive GPU smoke and fire simulation",
    "category": "Physics",
}

from . import properties
from . import operators
from . import ui
from . import preferences
from . import playback
from . import quick_setup
from . import effector_display


def register():
    properties.register()
    operators.register()
    playback.register()
    quick_setup.register()
    ui.register()
    preferences.register()
    effector_display.register()


def unregister():
    effector_display.unregister()
    preferences.unregister()
    ui.unregister()
    quick_setup.unregister()
    playback.unregister()
    operators.unregister()
    properties.unregister()
