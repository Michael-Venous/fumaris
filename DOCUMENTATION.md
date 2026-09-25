# Fumaris 1.2.0 Documentation

Fumaris is an interactive GPU smoke and fire simulator for Blender. Use its
live volume preview to develop motion quickly, then bake standard OpenVDB
sequences for Blender's native volume shading and rendering workflow.

Fumaris 1.2.0 adds Quick Setup, optional Bake on Preview recording, faster
first startup, and experimental smoke/fire Upres. See [CHANGELOG.md](CHANGELOG.md)
for release details and the Updating section below before replacing an installation.

## Requirements And Compatibility

- Blender 5.1 or newer
- The Windows x64 or Linux x64 Fumaris package for your operating system
- A Vulkan-capable GPU with a current driver
- Enough GPU memory for the selected resolution and sparse block capacity
- Local writable storage for baked OpenVDB sequences

Fumaris has been exercised on NVIDIA GPUs under Windows and Linux and Intel
Arc GPUs under Windows and Linux. Recorded release testing includes an NVIDIA
RTX 3090 on Linux and an Intel Arc 140V on Windows. AMD hardware has not yet
been qualified. It may work through the same Vulkan path, but this release does
not promise AMD compatibility. macOS and Apple Silicon are unsupported.

GPU and driver combinations vary considerably. If Fumaris cannot create a
device or complete its first preview, update the driver and send the console
log with a support request.

Fumaris automatically prefers a discrete GPU when a system also exposes
integrated graphics. Advanced multi-GPU users can set
`FUMARIS_GPU_DEVICE_INDEX` to an index shown in the device-probe console output.

## Installation

1. Download the ZIP matching your operating system. Do not extract it.
2. In Blender, open `Edit > Preferences > Extensions`.
3. Open the menu in the upper-right and choose `Install from Disk`.
4. Select the Fumaris ZIP and enable the extension if prompted.
5. Select an object and open `Physics Properties > Fumaris`.

The package is self-contained. Do not move or delete files inside the installed
extension directory.

The development runtime compiles GPU shaders only when a feature uses them.
Simple smoke previews avoid compiling unrelated features at startup. First use
of a more complex feature can still take longer, especially after a GPU driver
update. The driver's shader cache normally makes subsequent runs faster.

### Updating

Stop all Fumaris jobs before updating, then restart Blender. Keep old caches
and important scenes backed up. Do not share a writable cache directory between
1.0 and older Fumaris versions; their cache-lock protocols differ. New versions
can change simulation results, so retain an old installation if exact rebakes
of a previous project are required.

At high smoke density and flame brightness, the preview can show pronounced
boundaries as smoke hides the flame behind it. Preview shading is approximate;
check final appearance with a short VDB bake in your intended render engine.

Stop active Fumaris jobs, close Blender, and install the newer ZIP from disk.
Back up production `.blend` files before moving between versions.

Fumaris replaces the former Plume Forge extension ID. Do not enable both
extensions at once. Files made with development versions of Plume Forge may
need their custom properties migrated before that old extension is removed.

## The Basic Workflow

Fumaris uses one object as the domain settings host and collections to identify
simulation participants. Unlike Mantaflow, the domain object's dimensions do
not form a hard simulation box; Flow allocates a sparse grid around active
smoke.

1. Select any convenient object and set `Flow Object` to `Domain`.
2. Create an emitter collection and assign it under `Participants`.
3. Put source objects in that collection. Set each one's `Flow Object` to
   `Emitter` and choose its source shape.
4. Select the domain and press `Play` to see an interactive volume preview.
   `Pause` freezes simulation but keeps camera and Appearance rerenders active;
   `Resume` continues from the next frame.
5. Tune the domain, emitters, effectors, and other participants.
6. Press `Stop`, configure `Output`, and press `Bake` for final OpenVDB files.

Participant collections are searched recursively, so organized child
collections are supported. An object is only used when its `Flow Object` role
matches the collection role. Turn off a participant's `Enabled` setting to
exclude it without removing it from the collection.

## Preview

`Play` starts a persistent Flow session. Fumaris advances the timeline only
after each simulated frame is ready and loops over the domain's own Start and
End Frame range. These values are independent of Blender's timeline range.
The live grid and preview resources remain allocated in VRAM so camera-only
rerenders stay responsive. This is expected; press `Stop` to close the session
and release its GPU working set.

`Volume` mode raymarches the live sparse Flow grid and displays the resulting
image over the 3D viewport. With Bake on Preview off, it does not create NanoVDB
or OpenVDB output. Moving
the viewport or changing display settings rerenders the current Flow frame
without advancing the simulation. The former diagnostic points preview has
been removed; point-based emitters remain supported. A hidden domain can keep
simulating while its viewport overlay is suppressed.

Most participant and simulation settings can be adjusted while preview is
running and affect subsequent simulated frames. Changes that alter fundamental
sparse grid allocation restart the preview session.

Useful preview controls:

- Live preview uses the volume raymarch. The old diagnostic points path has been removed.
- `Preview Resolution` scales simulation resolution for preview only.
- `Viewport Scale` controls output pixels. Doubling the value renders, reads
  back, transfers, and uploads four times as many pixels.
- `Ray Steps` accepts up to 4096 and controls sampling through active sparse blocks. It stops adding
  work once spacing reaches 0.75 simulation voxel because finer samples cannot
  recover detail absent from the grid.
- `Tone Mapping` defaults to `Filmic`, which softens bright highlights instead
  of clipping them to white. `AgX (Preview)` adds an AgX approximation with
  highlight desaturation. `Off` restores the original preview look.
- Mapped modes are calibrated against Off at neutral display gray 0.18, reducing
  brightness jumps when switching modes while preserving highlight compression.
- `Exposure` adjusts brightness before tone mapping: +1 doubles the incoming
  light, -1 halves it. Try -1 or -2 if bright fire looks washed out. Both controls
  update while paused and affect only the preview, not baked grids or materials.
- The domain's `Appearance` group controls smoke density and color plus fire
  visibility, brightness, temperature range, and Blackbody scale. The same
  values drive the live raymarch and Fumaris's generated Blender material.
- `Self Shadows` traces directional light through the visible smoke density at
  additional GPU cost. Changing Appearance Density also rebuilds this field so
  light and camera attenuation stay consistent.
- `Shadow` sets the minimum light reaching smoke: zero permits deep shadows;
  raising it fills them in. `Azimuth` rotates light around world Z without
  changing its height; `Elevation` sets that height (0 degrees horizontal,
  +90 overhead). These controls do not move Blender scene lights.
- `Preview Bake` shows the selected preview during a final bake, with some
  performance cost.

The live volume preview uses a simple Flow shader and currently does not use
Blender's scene depth for object occlusion. Its Filmic option is an ACES-style
approximation; AgX (Preview) uses a polynomial approximation of the original
AgX transform. Neither is Blender's full OCIO view transform. The preview does not
follow scene color-management settings; final OpenVDB shading and rendering
inside Blender are unaffected.

Press `Pause` to inspect the frozen live grid from other angles without
advancing it. Press `Stop` to end preview, clear its viewport image, and release
its Flow working set. Native Blender
playback is separate from Fumaris preview and should not be started at the same
time.

## Baking And Cache Slots

`Bake` resets Flow and simulates every frame sequentially. It writes an
OpenVDB sequence and imports the completed frames as a Blender Volume object.
The status row reports elapsed time and peak RAM and VRAM observed during the
bake.

Under `Output`:

- Choose a local writable cache directory with enough free space. Before the
  project is saved, default/project-relative output uses Blender's user data
  directory (system temporary storage if unavailable). An explicit absolute
  output directory is respected. After saving, project-relative output is
  resolved beside the `.blend`; earlier caches stay at their original location.
- Use descriptive Cache Slot names to keep multiple bakes without overwriting
  earlier versions.
- Assign a material to apply it automatically to the imported volume.
- Use `Selectable Volume` to control whether the imported object can be
  selected in the viewport.
- Enable only the VDB channels needed by your material. Each extra grid adds
  GPU readback, conversion time, memory use, and disk space.

`Stop` during a bake imports every valid frame already written. Flow's complete
solver state cannot currently be serialized, so a stopped bake cannot resume;
the next Bake starts from the beginning. `Delete` removes the selected cache
slot and its imported volume. It is unavailable while a job is active.

## Domain Settings

### Upres (Experimental)

Upres adds advected detail to smoke and optionally fire, using one shared detail
motion field and memory budget. It affects the live volume and baked fields;
it does not increase the resolution of the velocity/pressure or combustion
solve. Stop the simulation before enabling or disabling Upres. Pausing retains
its allocation, so the toggle remains unavailable until you press Stop.

- Smoke Detail controls density refinement. The default is 4; 0 uses original smoke.
- Fire Detail controls temperature and burn refinement. Its default is 4; set it to 0 to
  preserve base fire fields. Fuel and
  combustion remain on the base grid; this is visual refinement, not additional
  combustion physics. When enabled, requested temperature/burn exports use the
  refined grid too.
- Detail Size is shared by smoke and fire and measured in base cells, not an
  output-resolution multiplier. The default is 8. Larger values make broader swirls; values below
  2 share a minimum sampling size.
- Strength and size accept typed values beyond their slider ranges. Large values
  can distort the volume or expose patterns; more is not always better.
- Detail Memory Limit defaults to 4096 MiB (4 GiB). Its slider reaches 32768 MiB;
  larger values can be typed, but this does not create additional VRAM.
- The limit checks estimated peak allocation, not the current total shown by a
  GPU monitor. The base solver, temporary resources, and Blender also need room.
  System RAM does not replace GPU memory. Lower resolution or disable upres if
  the budget is reached; raise it only when the GPU has sufficient headroom.

Compare a short bake with upres disabled and with a higher base resolution.
Depending on the effect, higher base resolution can be the better tradeoff.
Upres is not a fix for sparse-boundary stepping or insufficient solver substeps.
The advection correction falls back to ordinary advection when it overshoots,
and applies coarse-field losses proportionally to reduce plateaus and holes
under strong shear. Very high detail strength/vorticity can still expose
unresolved base-grid structure.

### Resolution And Sparse Capacity

Resolution determines density cell size. Sparse Block Capacity controls how
much of the world may become active and is also a practical GPU memory ceiling.
Increasing resolution without enough blocks can clip growth or trigger Auto
Cell Size. Increase capacity carefully because a large value reserves more
GPU memory. Resolution has no artificial maximum: values above the slider's
suggested range can be typed directly, but extreme values can exhaust VRAM or
produce no extra detail when sparse capacity or Auto Cell Size is limiting.

Start with the defaults. For energetic or high-resolution effects, raise
Sub-Steps until motion and combustion stop changing materially. Sub-Steps are
complete solver steps and may be set from 1 to 40; cost rises roughly linearly.

`Small Sparse Blocks` usually reduces memory and improves speed, but larger
blocks can produce smoother continuity in demanding high-resolution effects at
a substantial performance cost. Leave `Allocate Neighbor Blocks` and
`Boundary-Safe Advection` enabled for fast-moving smoke.

### Smoke Behavior

- `Gravity` accelerates the velocity field.
- `Temperature Buoyancy` lifts hot gas.
- `Smoke Buoyancy` controls density-driven lift or sinking.
- `Vorticity` restores small rotational motion lost through advection.
- `Dissipation` fades smoke over time.

Combustion is active when its coefficients require it. Fuel burns above the
ignition threshold, producing configurable smoke, heat, and expansion. Large
changes can require retuning Sub-Steps because combustion adds velocity.

## Emitters

Fumaris supports:

- Evaluated mesh surface or volume emission
- Transform-based sphere and box emitters
- Blender particle systems as points or instanced meshes
- Geometry Nodes point clouds and evaluated meshes
- File-based OpenVDB volume emission
- Experimental evaluated Geometry Nodes volume emission

Emitter Smoke, Temperature, Fuel, Burn, and Divergence define the source
channels. Initial Velocity adds a constant vector. Motion Velocity Scale
controls velocity inherited from source movement or mapped point velocities.
Normal Velocity (Experimental) uses averaged mesh vertex normals, including
particle and Geometry Nodes mesh instances. Emission can be uneven across faces
and hard edges; primitive and point/volume modes do not support this control.

Mesh `Surface` emission follows the evaluated surface and Emission Distance.
`Volume` fills a closed mesh. A named vertex group or evaluated mesh attribute
can vary emission strength continuously from 0 to 1. Mask Threshold skips
triangles whose average weight is too low; keep it near zero for soft painted
transitions.

Leave `Mask` empty to emit from the whole mesh. The mask must exist on the
evaluated output: new geometry made by a Geometry Nodes primitive does not
automatically inherit weights painted on the original object. Use `Store Named
Attribute` on the generated mesh and enter that attribute's name in `Mask`, or
clear `Mask`. If every face is below the threshold, Fumaris reports that the
mask excludes all faces rather than treating the node output as missing.

`Motion Sub-Steps` sample intermediate source poses for moving or deforming
sources. They do not improve a static emitter or the fluid solve and they
multiply emitter work inside each domain Sub-Step.

### Collection Points

For many separately animated mesh objects, use one controller object with
`Flow Object: Emitter` and `Shape: Collection Points`. Put this controller in
the domain's Emitters Collection and choose the meshes' collection as its
`Source Collection`. Child collections are included. Source meshes do not need
their own Fumaris role. Remove their old emitter roles or keep them outside the
domain's Emitters Collection to avoid emitting them twice.

Each source mesh becomes one spherical point in the existing batched particle
path. Emitted channels and velocity settings come from the controller. Choose
object origins or evaluated bounds centers for positions. Object Size derives
radius from half the largest scaled local bounds dimension, with a Radius Scale;
Fixed Radius uses one world-space radius. This approximates mesh shapes and does
not reproduce detailed surfaces, elongated shapes, or rotational surface flow.
The point path's minimum radius of 0.05 Blender units still applies.

Motion velocity is calculated from successive evaluated world-space centers and
elapsed frame time, including parented, constrained, and rigid-body transforms.
It is matched by object identity, not collection order. New sources and the first
frame have no inherited motion; explicit Initial Velocity still applies. Disabling
the controller or restarting/rewinding the simulation clears motion history.
Source membership and center-mode changes restart a live preview. Hide state is
not an emission mask: collection membership selects source meshes.

This avoids exporting and processing each mesh's triangles separately. It does
not make arbitrary smoke volumes free: large radii, high resolution, and widely
spread sources still increase solver work. Mesh emitters remain appropriate
when the emission must follow actual surfaces.

### Collection Mesh

Use `Shape: Collection Mesh` on the same kind of controller when you need actual
mesh emission rather than spherical approximations. Fumaris combines evaluated
source meshes into one mesh emitter, without joining or modifying scene objects.
Transforms and deformation contribute per-vertex world-space velocity; unchanged
local mesh geometry is cached where supported. Source settings come from the
controller, including surface/volume region, channels, mask, and normal velocity.

Use this for sources that can share settings. It behaves as one combined mesh,
not as independently layered emitters: overlapping surfaces/volumes can behave
differently, and different source channels need separate controllers. Deformation
motion assumes stable vertex correspondence. Changed connectivity resets that
object's inherited deformation velocity; changes that reorder vertices without
changing connectivity cannot be reliably identified. Keep separate emitters for
such sources or supply a stable-topology representation.

Combining meshes reduces per-emitter processing but still exports moving vertices
and processes triangles. Collection Points is the lighter choice when spheres
are sufficient. Empty source collections emit nothing. Remove individual source
emitter roles or keep them outside the domain's Emitters Collection to avoid
duplicate emission, as with Collection Points.

### Geometry Nodes Attributes

Geometry Nodes point sources can map named attributes for:

- Position
- Velocity
- Radius
- Smoke, temperature, fuel, burn, and divergence
- Per-point smoke, temperature, and velocity coupling
- Emission mask

Store attributes on the point domain before Geometry Output. Missing optional
attributes fall back to the emitter's object-level values. Empty point clouds
are valid, allowing a particle source to disappear without ending the
simulation.

Point clouds use Flow's batched spherical emitter while preserving varying
radius and every mapped simulation channel. Very large point counts or a cloud
that mixes one unusually large radius with many small points can increase GPU
work; reduce point count or split widely different sizes into separate sources
if they become the frame bottleneck.

Geometry Nodes Volume emission is experimental because Blender currently
exposes evaluated volume grids through a temporary OpenVDB staging path rather
than a direct voxel buffer. File-based OpenVDB emission does not use that path.

## Colliders, Effectors, And Outflows

Assign optional collections to the domain's participant fields.

### Colliders

Mesh, box, and sphere colliders transfer boundary velocity into smoke. Mesh
`Convex Collision` treats a suitable closed convex mesh as a filled volume;
unsupported meshes fall back to the regular shell behavior. Increase Collider
Margin for fast smoke or thin geometry.

Fumaris strengthens Flow's stock collision emitters with pre- and post-pressure
velocity treatment and interior density removal. This improves practical
containment, but it is not a mathematically sealed no-flux pressure boundary.
Very fast smoke may still leak through thin or complex colliders.

### Effectors

Coupling defaults to **8** and is passed directly to the solver, without
rescaling. Explicitly saved values and animation use the original property.
Older objects without a stored value inherit the new default. Emitter Channel
Coupling is unchanged.

Effectors support force, wind, vortex, and turbulence behavior with strength,
Influence Radius, coupling, sampling, noise, and distance falloff controls.
Influence Radius changes the affected region, not detail or resolution. Vortex uses
a cylindrical field with a controllable height and rotating core; optional
Inflow confines smoke toward the axis and Updraft carries it along local Z.
Effectors use the object's complete rotation, including roll. Noise is a
smooth coherent field in the effector's local space, so rotating the object
rotates both its
direction and pattern. Noise Size controls the approximate feature size;
smaller values automatically increase the internal field resolution up to 64
samples per axis. Samples sets the minimum resolution and can be raised when a
very small pattern still looks coarse. `W` is an animatable fourth-dimensional
noise coordinate for changing or art-directing the pattern without moving the
effector. Turbulence uses Strength as its amplitude, while Noise Amount mixes
noise into Force, Wind, and Vortex.
Effectors change velocity; they do not emit smoke.

### Outflows

Outflows remove smoke and combustion channels in a mesh surface or volume
region. Their mask and motion controls follow the mesh participant workflow.

## VDB Channels And Shading

Density is the standard smoke grid. Additional outputs are:

- `temperature`: Kelvin-like values in a fixed `0-5000` range, suitable for a
  Blackbody node or a custom remap.
- `burn`: raw timestep-dependent combustion activity for advanced control.
- `fuel`: remaining unburned fuel.
- `velocity`: world-space motion used for volume motion blur and compositing.

When `Volume Material` is unset, Fumaris automatically includes temperature
and burn and creates a material whose fire response is evaluated at render
time:

```text
heat = smoothstep(Start Temperature, Full Temperature, temperature)
source = max(burn, 0) * heat * (fps * Sub-Steps / Simulation Speed)
flame = 1 - exp(-source)
```

`flame * Brightness` drives Emission Strength. Temperature multiplied by
`Blackbody Scale` drives a Blackbody node for Emission Color. The default scale
is `1.0`; changing it, the smoke settings, or the fire settings does not require
a rebake. Raw burn remains in the VDB for custom materials and compositing.

When a custom `Volume Material` is assigned, enable the additional grids that
material needs explicitly. Fumaris does not export a separate derived `flame`
grid.

If a volume disappears only when motion blur is enabled, lower Blender's
volume velocity scale and inspect the velocity grid. Excessive motion bounds
can cause the renderer to reject or miss a volume frame.

## Performance Guidance

Preview with Bake on Preview off is normally much faster than Bake because it reads back one
viewport-sized RGBA image. Bake must read complete grids from the GPU,
convert NanoVDB to OpenVDB, and write them to disk.

For a faster or more memory-efficient simulation:

1. Disable VDB channels not used by the final material.
2. Keep Preview Bake off unless you need to watch the final bake.
3. Lower Preview Resolution, Viewport Scale, or Ray Steps for viewport work.
4. Increase resolution and sparse capacity in measured steps.
5. Use the lowest Sub-Step count that gives a converged result.
6. Keep caches on a fast local drive.
7. Split point clouds with widely different radii and reduce excessive point
   counts when point emission dominates a frame.

## Known Limitations

- Hardware coverage is limited and AMD GPUs are currently unqualified.
- Geometry Nodes Volume emission is experimental.
- Unrecorded preview is temporary. Bake on Preview preserves VDB playback, but
  recorded VDBs are not solver checkpoints for resuming from arbitrary frames.
- Volume preview shading is intentionally simpler than a final Blender volume
  material and does not yet use scene depth for occlusion.
- Stopped bakes cannot resume solver state.
- Only one Fumaris preview or bake may run at a time.
- Colliders are improved Flow collision emitters, not sealed pressure
  boundaries.
- Results do not match Mantaflow parameter-for-parameter.
- macOS and Apple Silicon are unsupported.

## Troubleshooting And Support

Before reporting a problem:

1. Confirm that the package matches the operating system.
2. Update the GPU driver and restart Blender.
3. Allow shader compilation to finish when first using a feature; report the
   feature and whether the delay repeats on subsequent runs.
4. Try the included demo or a minimal domain and one emitter.
5. Reduce resolution and sparse capacity if memory is exhausted.
6. Check that the cache path is local, writable, and has free space.

Use the support channel on the purchase receipt. Include:

- Fumaris version and downloaded ZIP filename
- Blender version and build hash
- Operating system, GPU, driver, and Vulkan details
- Whether the issue occurs in Preview, Bake, or both
- Blender console output from job start through failure
- Exact reproduction steps and the smallest `.blend` that reproduces it

Remove proprietary assets before sending a project file. Hardware reports
are welcome even when the issue is specific to an unqualified GPU.


### Linux C++ runtime selection

The separate Fumaris worker prefers the system libstdc++ and libgcc runtimes
when they pass a Flow runtime-load check. This lets newer system Vulkan drivers
use the C++ symbols they require. Flow and Slang keep their private bundled
libraries. Older systems retain the bundled GCC fallback if the native runtime
cannot load Fumaris. Blender's own loaded libraries and environment are unchanged.

Motion Velocity Scale 1 uses source velocity in Blender units per second.
For deforming meshes, motion is estimated from corresponding evaluated vertices
on consecutive simulation frames. Keep vertex count and ordering stable; topology
changes or arbitrary frame jumps do not provide reliable deformation velocities.
Geometry Nodes velocity attributes must use local units per second, not displacement
per frame; object transforms convert them to world space. Coupling controls how
quickly the fluid approaches this target, and pressure can change the resulting
fluid velocity. An attribute does not automatically include additional host-object
motion that was not represented in the attribute.


### Playback while editing emitters

Use the Fumaris controls in the 3D Viewport or Timeline header, or the controls
above an emitter/collider/effector's Physics settings. Object selection stays
unchanged. Shift–Alt–Space starts, pauses or resumes the live preview;
Shift–Alt–X stops it. Both shortcuts can be used with one hand. The current
bindings appear in the button tooltips. Edit these bindings in Preferences → Add-ons →
Fumaris, or search Fumaris in Preferences → Keymap.

A single domain in the current view layer is picked automatically. With multiple
domains, choose one in the header's Fumaris menu; the choice is saved in the scene.
During playback, controls target the running preview regardless of selection.
Stop before changing the target. A baked domain must have its bake deleted before
starting live preview, as with the existing domain controls.

Fumaris owns sequential live simulation timing; it waits for each worker step
before submitting the next frame. Normal Blender playback is independent and
starting it stops live preview. Use it for baked VDB playback. The new shortcuts
do not replace Blender's normal Spacebar behavior.

### Quick Setup

With Flow Object set to **None**, click **Quick Setup…** in Physics → Fumaris, or choose it from the 3D Viewport's
Add menu. **Rising Smoke**, **Steady Fire**, and **Explosion** create a complete
starting setup. Choose **Selected Objects** for unassigned mesh sources, or
**New Source** to create an icosphere at the 3D cursor. Existing participants are
left intact; the new setup has separate emitter, collider, effector and outflow
collections. The new simulation object is an axes empty, with no physical domain bounds.

Rising Smoke emits continuously without fuel; Steady Fire emits smoke and fuel.
Explosion emits for 10 frames, then disables its source, with a 100-frame range.
Ranges start at the scene's Start Frame. These are editable starting points.
The simulation empty stores settings; smoke can extend freely around it.

### Bake on Preview

Enable **Bake on Preview** beside the preview controls before pressing Play. It records VDBs into a new take while the fast Flow overlay is displayed.
This uses **Preview Resolution**, including any reduction below 100%, and the
configured output channels/compression. Export, readback, conversion and writing
add work even though writing is asynchronous. The option is off by default.

- **Pause** lets an in-flight frame finish, waits for complete VDB files, and
  imports the recorded sequence. The live simulation stays in GPU memory.
- **Resume** hides the imported take and continues from the live simulation's
  next frame. Scrubbing a recorded frame while paused does not rewind that state.
- **Stop** finishes accepted work, imports the committed frames, and releases the
  live simulation. Wait for finalization to finish before closing Blender.
- At the end of the range, recording pauses. Resume starts a new take, preserving
  the previous files rather than overwriting them in a loop.
- Changes that require restarting the sparse grid finalize the old take and
  start a new one. Other live edits affect subsequent frames in that take; the
  recording preserves what you actually previewed, not a promise that the final
  settings alone can reproduce its history.

Imported VDBs use Blender materials and lighting, so they may look different
from the live overlay. Only one of the recorded take and live overlay is shown
at once. Publishing a new take replaces the previous preview Volume object;
older take files remain on disk. Preview takes live below the domain cache in `preview_takes`; they do
not overwrite the final bake. **Delete** clears this domain's generated bake and
owned preview recordings. Interrupted writes may leave completed files for
recovery; no partial file is presented as a committed frame.

`Preview Bake` is a different option: it displays the live overlay during a
normal final bake. `Bake on Preview` records the interactive preview. These
options sit side by side below the playback buttons.

### Capacity warning and defaults

A capacity warning appears beside the simulation controls even when Diagnostics
is collapsed. At full capacity smoke may be cut off: increase **Sparse Block
Capacity** or lower **Resolution**, then restart. A full-capacity warning remains
for that run because freeing blocks later does not recover missing smoke. A new
run or Delete clears it. Upres memory-limit failures also appear here with
recovery guidance. Diagnostics sits below Advanced on the panel's darker background;
its exact appearance follows your Blender theme.

Current defaults are Dissipation **0.5**, emitter Fuel **1**, Temperature Per Burn
**1**, and preview Ray Steps **256**. Explicitly stored settings retain their
values; older unset properties inherit the new defaults. The Rising Smoke setup
explicitly uses zero fuel. All Quick Effects use dissipation 1 and emitter
divergence 2; Steady Fire fuel is 0.5 and Explosion fuel is 2. The general emitter
divergence default is 2.
