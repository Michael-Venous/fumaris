# Fumaris Blender Integration

Current release: **1.1.0**.

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

This repository contains the GPL-licensed Python integration. GitHub's source
ZIP does not include the native bridge or runtime and is not a complete runnable
Fumaris package. Customer packages provide the matching components for each
operating system.

## Workflow

1. Set an object's **Flow Object** role to **Domain**.
2. Assign its emitter collection and set source objects in that collection to
   **Emitter**. Add collider, effector, and outflow collections as needed.
3. Start live preview, adjust settings, and pause to inspect the volume.
4. Stop preview and **Bake** an OpenVDB sequence for final rendering.

Domain participant collections include nested child collections and select only
objects with the matching role. Geometry Nodes, particle, collection, mesh,
primitive, and volume emission workflows are described in the manual.

Live preview uses a temporary volume overlay with approximate shading. It does
not save VDB frames or replace Blender's final rendering. Only one Fumaris
preview or bake runs at a time.

## What's New In 1.1.0

- Preview controls in viewport and timeline headers, a domain picker, and
  configurable play/pause/resume and stop shortcuts.
- AgX (Preview) tone mapping alongside Filmic and Off; these are preview
  approximations, separate from Blender's full color management.
- Volume-only preview, with hidden-domain overlay handling. Geometry Nodes and
  Collection Points emitters remain supported.
- Improved Linux runtime compatibility, writable cache paths for unsaved
  projects, and revised defaults for newly configured simulations.

See the [changelog](CHANGELOG.md) for the complete release notes.

## Source Map

| Files | Responsibility |
| --- | --- |
| `__init__.py`, `blender_manifest.toml` | Registration and extension metadata |
| `properties.py`, `ui.py`, `playback.py` | Object settings, panels, playback controls |
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
