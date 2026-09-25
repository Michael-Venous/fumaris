# Changelog

All notable customer-facing changes are recorded here.

## 1.2.0 - 2026-09-25

- Experimental Upres adds shared smoke/fire detail motion with separate strengths
  and an explicit detail-memory safety limit. Failures appear beside playback controls.
- Extend experimental Normal Velocity to particle and Geometry Nodes mesh instances;
  move its control into Velocity. Averaged normals can still emit unevenly across faces.
- Default emitter divergence to 2; Quick Effects use dissipation 1, Steady Fire fuel
  0.5, and Explosion fuel 2 with a ten-frame source burst.
- Default Upres Detail Size to 8 and Smoke/Fire Detail to 4; Upres remains opt-in.
- Delete clears the selected simulation's warning history.
- Include four replacement demo scenes in a separate demo download.


- Replace previous recorded-preview display objects on replay, retaining take files.
- Show Quick Setup only for unassigned objects; use axes empties as simulations.
- Compact preview options into two columns and move Upres guidance inline.

- Compile Vulkan pipelines only when used, avoiding costly first-start
  compilation of unrelated features on Intel and other Vulkan GPUs.
- Add Quick Setup for Rising Smoke, Steady Fire and Explosion, with linked
  participant collections and simulation empties.
- Add optional Bake on Preview: record VDB takes during preview, import on
  pause/stop, and preserve live state for Resume. Requires the matching updated
  native bridge.
- Show action-specific shortcut tooltips; move Stop to Shift–Alt–X for one-hand use.
  Play/Pause/Resume remains Shift–Alt–Space. Keep the compact Play button label.
- Show actionable capacity warnings outside Diagnostics; move Diagnostics below
  Advanced using the darker panel background.
- Default dissipation to 0.5, fuel to 1, temperature per burn to 1, and ray steps
  to 256. Select Density after importing VDBs.
- Batch shared instanced-mesh extraction without reducing geometry fidelity.

## 1.1.0 - 2026-09-17

- Control live preview without selecting the domain: viewport/timeline header
  controls, participant controls, a scene domain picker, and configurable
  Shift–Alt–Space (play/pause/resume) and Shift–Alt–Backspace (stop) shortcuts.
- Add AgX (Preview) tone mapping and calibrate AgX/Filmic brightness against
  Off (Legacy). These are preview approximations, not Blender's full OCIO transform.
- Remove the obsolete points preview renderer. Volume preview remains;
  Geometry Nodes and Collection Points emitters are unaffected.
- Allow hidden domains to simulate while suppressing their overlay. Explain when
  live preview needs a 3D Viewport instead of failing with a density-readback error.
- Prefer compatible system GCC runtimes for the isolated Linux worker so newer
  graphics drivers can load, retaining the bundled fallback on older systems.
- Explain why Smoke Upres is disabled during running/paused previews. Rename
  effector Radius to Influence Radius to distinguish coverage from detail.
- Default emitter and effector coupling to 8, Expansion Per Burn and Vorticity
  to 2, and temperature/burn exports to enabled. Preview density defaults to 2,
  brightness to 5, and smoke color to (0.5, 0.5, 0.5).
  Existing explicitly stored settings and animation are preserved.
- Resolve unsaved-project cache paths to writable user storage.
- Remove orphaned preview code and update diagnostics and profiling tools.

Stop active jobs and restart Blender after updating the extension and native
worker together. Existing caches are preserved. Native Blender playback remains
independent; use it for baked VDB playback.

## 1.0.1 - 2026-09-13

- Collection Points combines source meshes into one sphere-cloud emitter, with
  object-size or fixed radii and automatic per-object motion velocity.
- Collection Mesh combines evaluated meshes into one emitter with shared channels,
  motion velocity, nested collection support, and reflected winding correction.
- Source meshes do not need individual Fumaris roles. Membership changes, renamed
  objects, disabled sources and timeline rewinds reset motion history safely.
- Fixed Copy Diagnostics failing when installed as a Blender extension.
- Fixed Linux packaging when a required library is already in the runtime bundle.

Collection meshes share emission settings; intersecting closed meshes and changing
vertex correspondence retain the limitations described in the documentation.
Stop active jobs and restart Blender after updating. Existing caches are preserved.

## 1.0.0 - 2026-09-08

- Optional smoke detail/upres with an explicit GPU-memory budget.
- Preview light azimuth/elevation, shadow floor, exposure, and up to 4096 ray steps.
- More compact diagnostics and improved GPU/memory reporting.
- Faster native point-cloud emission, reused resources, and bounded cache writing.
- Improved preview pause/resume, collection visibility, cleanup, and loop handling.
- Fixed equal/reversed flame temperature controls rejecting preview requests.
- Cache locks now use OS-held guards to prevent concurrent writers and safely
  release after process crashes. Avoid sharing a cache with older Fumaris versions.
- Read-only cache recovery no longer interrupts scene loading.

- Geometry Nodes mesh errors now identify masks that exclude every face or
  reference missing attributes, instead of incorrectly reporting missing geometry.
- Filmic tone mapping in Preview Settings, with adjustable exposure and an
  Off (Legacy) option. Works on paused volumes, preserves smoke opacity and
  smoke-free fire, and does not change baked materials or simulation data.

## 0.2.0 Beta - 2026-08-20

### Added

- Renamed the product and extension package to Fumaris.
- Migrated bundled beta scenes and legacy cache names to Fumaris.
- Windows x64 runtime support validated on Intel Arc 140V.
- Per-bake elapsed time, peak system RAM, and peak GPU memory reporting.
- First-run GPU shader-compilation notice.
- Multiple independent cache slots for preserving separate bakes.
- Detailed Flow, preview, readback, and OpenVDB timing diagnostics.
- Convex filled-volume collision mode and participant motion sub-steps.
- Raymarched live volume preview with shared smoke and fire Appearance controls.
- Animatable fourth-dimensional Noise W control for effectors.
- Cylindrical vortex fields with core radius, height, inflow, and updraft controls.
- Pause/Resume for freezing live simulation while retaining interactive camera
  and Appearance rerenders.

### Improved

- Consolidated installation, workflow, shading, performance, and
  troubleshooting guidance into one customer manual.
- Faster bounded asynchronous OpenVDB conversion and writing.
- Lower-memory density readback and output-channel handling.
- Preview loop reset, point scaling, cache paths, and lifecycle cleanup.
- Boundary-safe sparse advection for fast smoke and corrected full-solver
  substep semantics.
- Linux release compatibility through an Ubuntu 22.04 build baseline.
- Kelvin-like temperature output and generated burn/temperature fire shading
  that can be adjusted without rebaking or exporting a derived flame grid.
- Stronger pre/post-pressure collider constraints and interior density cleanup.
- Reused static effector textures, parallel dynamic-noise generation, and
  sparse-bounded non-allocating effector dispatches.

### Fixed

- Relative cache failures for unsaved Windows projects.
- Stale preview and bake state after stopping, loading files, or bridge errors.
- Runtime metric persistence and bake cleanup.
- Add-on startup migration during Blender's restricted registration phase.
- Live self-shadowing that could uniformly darken dense simulations because
  camera and shadow rays used different density ranges and stale shader layouts.

### Known Limitations

- Geometry Nodes Volume emission is experimental.
- Preview is temporary feedback and does not create a durable cache.
- Stopped bakes import completed frames but cannot resume solver state.
- AMD GPUs are unqualified; macOS and Apple Silicon are unsupported.

## 0.1.0 Beta - 2026-07-12

- Initial public beta.
