bl_info = {
    "name": "Fumaris",
    "author": "Fumaris",
    "version": (0, 2, 0),
    "blender": (5, 1, 0),
    "location": "Properties > Physics",
    "description": "Interactive GPU smoke and fire simulation",
    "category": "Physics",
}

from . import properties
from . import operators
from . import ui
from . import preferences


def register():
    properties.register()
    operators.register()
    ui.register()
    preferences.register()


def unregister():
    preferences.unregister()
    ui.unregister()
    operators.unregister()
    properties.unregister()
