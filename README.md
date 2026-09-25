# Fumaris Blender Integration

Current release: **1.2.0**.

Fumaris is an interactive GPU smoke and fire simulator for Blender. Develop
motion with a live volume preview, then bake OpenVDB sequences for Blender's
native volume shading and rendering.

## Requirements And Installation

- Blender 5.1 or newer
- Windows x64 or Linux x64
- A Vulkan-capable GPU and sufficient GPU memory
- The matching Fumaris package, including its native simulation runtime

Install the platform ZIP through **Edit > Preferences > Extensions > Install
from Disk**. Stop active Fumaris jobs and restart Blender when updating.
See [compatibility and installation guidance](DOCUMENTATION.md#requirements-and-compatibility)
for hardware qualifications and first-run shader compilation.

The development runtime compiles GPU shaders when a feature first uses them.
Your graphics driver can cache the results for later runs; first use of a new
feature or a driver update may still require compilation.

This repository contains the GPL-licensed Python integration. GitHub's source
ZIP does not include the native bridge or runtime and is not a complete runnable
Fumaris package. Customer packages provide the matching components for each
operating system.

## Workflow

1. Choose **Quick Setup…** in Fumaris Physics settings or the 3D Viewport's Add
   menu. Pick **Rising Smoke**, **Steady Fire**, or **Explosion**, using selected
   meshes or a new source. It creates a simulation empty and connects
   its participant collections.
2. Press **Play** and adjust the simulation. **Shift–Alt–Space** plays,
   pauses, or resumes; **Shift–Alt–X** stops. The controls show your current
   bindings, which can be changed in Fumaris preferences.
3. Optionally enable **Bake on Preview** before starting. It records VDBs at
   preview resolution and imports them on pause or stop. Recording adds export
   and disk costs; leave it off for the fastest iteration.
4. Use **Bake** for a final sequence at the domain's full resolution.

Domain participant collections include nested child collections and select only
objects with the matching role. Geometry Nodes, particle, collection, mesh,
primitive, and volume emission workflows are described in the manual.

Live preview uses a volume overlay with approximate shading. With Bake on Preview
off it is temporary; with it on, recorded volumes use Blender's scene shading
when imported. Only one Fumaris preview or bake runs at a time.

The simulation empty is a settings reference, not a hard smoke
boundary. If **Simulation capacity reached** appears, increase **Sparse Block
Capacity** or lower **Resolution**, then restart to recover a complete result.
Doubling resolution needs approximately eight times the cells for equal coverage.

## What's New In 1.2.0

- Quick Setup for Rising Smoke, Steady Fire, and Explosion.
- Optional Bake on Preview recording, imported when paused or stopped.
- Faster first startup through on-demand GPU pipeline compilation and faster
  export of shared mesh instances.
- Experimental smoke/fire Upres with a detail-memory budget and visible failure guidance.
- Compact playback controls, clearer shortcut tooltips, and actionable capacity warnings.
- Four demo scenes: Explosion, Mushroom, Tornado, and Effector Vacuum, supplied
  in the separate `fumaris-1.2.0-demos.zip` download. Extract before opening;
  install the matching addon, select the simulation, and press Play.

See the [changelog](CHANGELOG.md) for the complete release notes.

## Source Map

| Files | Responsibility |
| --- | --- |
| `__init__.py`, `blender_manifest.toml` | Registration and extension metadata |
| `properties.py`, `ui.py`, `playback.py`, `quick_setup.py` | Settings, panels, playback controls, starting setups |
| `participants.py`, `exporters.py`, `*_export.py` | Participant discovery and evaluated scene export |
| `operators.py`, `jobs.py`, `runtime.py` | Actions and simulation job lifecycle |
| `bridge.py`, `protocol.py`, `linux_runtime.py` | Native worker startup and communication |
| `preview.py` | Viewport volume overlay |
| `cache.py`, `importers.py` | Cache ownership and Blender volume import |

The Python addon sends scene inputs to a separate native simulation process.
The native bridge, Flow sources, platform binaries, and private build tooling
are maintained separately from this public integration repository.

## Documentation And Support

- [User manual](DOCUMENTATION.md): installation, workflow, settings, shading,
  performance, troubleshooting, and known limitations.
- [Changelog](CHANGELOG.md): customer-visible changes by version.

Use the support channel listed on the product page and include the Fumaris
version, Blender version, operating system, GPU, driver version, console log,
and a minimal reproduction file.

## License

The Python integration is free software under GPL-3.0-or-later. You may use,
modify, and redistribute it under that license. See [LICENSE](LICENSE).

Customer packages also contain a separately licensed native runtime and
third-party libraries. See [LICENSE_OVERVIEW.md](LICENSE_OVERVIEW.md),
[COMMERCIAL_RUNTIME_LICENSE.txt](COMMERCIAL_RUNTIME_LICENSE.txt), and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Third-party names and marks belong to their respective owners. Fumaris is
not affiliated with or endorsed by Blender Foundation or NVIDIA.
