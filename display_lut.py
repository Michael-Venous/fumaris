"""The scene's view transform as a table, so the live preview tone maps like the render.

Blender's colour management (its own OCIO config, the scene's display, view,
look and gamma) is evaluated with PyOpenColorIO on a lattice of scene-linear
Rec.709 colours spaced in log2 (25 stops from 2^-12.47, as AgX), red fastest.
The bridge samples it in the preview shader. Without PyOpenColorIO or for an
unknown view, the preview keeps its built-in curves.
"""
import os

import bpy

SIZE = 33
LOG2_MIN = -12.47393
STOPS = 25.0

_CACHE = {}
_FAILED = set()
# Tables kept (431 kB each, about 10 ms to build). Gamma is part of the key:
# without a limit, dragging it left a table for every value until Blender closed.
CACHE_LIMIT = 16


def _config_path():
    override = os.environ.get("OCIO")
    if override and os.path.isfile(override):
        return override
    for kind in ("LOCAL", "SYSTEM"):
        base = bpy.utils.resource_path(kind)
        path = os.path.join(base, "datafiles", "colormanagement", "config.ocio")
        if os.path.isfile(path):
            return path
    return None


def _build(display, view, look, gamma):
    import numpy as np
    import PyOpenColorIO as ocio

    path = _config_path()
    if path is None:
        raise RuntimeError("Blender's OCIO config was not found")
    config = ocio.Config.CreateFromFile(path)
    pipeline = ocio.LegacyViewingPipeline()
    pipeline.setDisplayViewTransform(ocio.DisplayViewTransform(
        src="Linear Rec.709", display=display, view=view))
    if look and look != "None":
        pipeline.setLooksOverrideEnabled(True)
        pipeline.setLooksOverride(look)
    processor = pipeline.getProcessor(config).getDefaultCPUProcessor()
    steps = np.linspace(0.0, 1.0, SIZE, dtype=np.float32)
    values = np.exp2(LOG2_MIN + steps * STOPS).astype(np.float32)
    blue, green, red = np.meshgrid(values, values, values, indexing="ij")
    rgb = np.ascontiguousarray(np.stack([red, green, blue], axis=-1).reshape(-1, 3), dtype=np.float32)
    processor.applyRGB(rgb)
    if gamma > 0.0 and abs(gamma - 1.0) > 1e-6:
        rgb = np.power(np.clip(rgb, 0.0, None), 1.0 / gamma)
    return np.clip(np.nan_to_num(rgb), 0.0, 1.0).astype(np.float32).ravel()


def scene_display_lut(scene, view_override=None, look_override=None):
    """(key, flat float32 array of SIZE^3 RGB) for the scene's view transform and
    look (or view_override with look_override, None for no look), or (None, None)."""
    view = getattr(scene, "view_settings", None)
    display_settings = getattr(scene, "display_settings", None)
    if view is None or display_settings is None:
        return None, None
    display = display_settings.display_device
    transform = view_override or view.view_transform
    look = (look_override or "None") if view_override else getattr(view, "look", "None")
    gamma = float(getattr(view, "gamma", 1.0))
    key = f"{display}|{transform}|{look}|{gamma:.4f}|{SIZE}"
    table = _CACHE.get(key)
    if table is not None:
        return key, table
    if key in _FAILED:
        return None, None
    try:
        try:
            table = _build(display, transform, look, gamma)
        except Exception:
            # A look the config does not know: the view alone is still closer
            # than the built-in curves.
            table = _build(display, transform, "None", gamma)
    except Exception as error:
        _FAILED.add(key)
        print(f"Fumaris: preview keeps its built-in tone mapping ({error})")
        return None, None
    _CACHE[key] = table
    while len(_CACHE) > CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    return key, table
