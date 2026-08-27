# Fumaris 0.2.0 Beta Documentation

Fumaris is an interactive GPU smoke and fire simulator for Blender. Use its
live point preview to develop motion quickly, then bake standard OpenVDB
sequences for Blender's native volume shading and rendering workflow.

Fumaris 0.2.0 is a public beta. Save copies of important project files, keep
your GPU driver current, and report reproducible problems with the system
details requested at the end of this document.

## Requirements And Compatibility

- Blender 5.1 or newer
- The Windows x64 or Linux x64 Fumaris package for your operating system
- A Vulkan-capable GPU with a current driver
- Enough GPU memory for the selected resolution and sparse block capacity
- Local writable storage for baked OpenVDB sequences

Fumaris has been exercised on NVIDIA GPUs under Windows and Linux and Intel
Arc GPUs under Windows and Linux. Recorded release testing includes an NVIDIA
RTX 3090 on Linux and an Intel Arc 140V on Windows. AMD hardware has not yet
been qualified. It may work through the same Vulkan path, but this beta does
not promise AMD compatibility. macOS and Apple Silicon are unsupported.

GPU and driver combinations vary considerably. If Fumaris cannot create a
device or complete its first preview, update the driver and send the console
log with a support request.

## Installation

1. Download the ZIP matching your operating system. Do not extract it.
2. In Blender, open `Edit > Preferences > Extensions`.
3. Open the menu in the upper-right and choose `Install from Disk`.
4. Select the Fumaris ZIP and enable the extension if prompted.
5. Select an object and open `Physics Properties > Fumaris`.

The package is self-contained. Do not move or delete files inside the installed
extension directory.

The first simulation after installing Fumaris or changing GPU drivers can take
several minutes while Flow compiles GPU shaders. Blender displays an
initialization notice during this one-time work. Later sessions should start
much faster.

### Updating

Stop active Fumaris jobs, close Blender, and install the newer ZIP from disk.
Back up production `.blend` files before moving between beta versions.

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
4. Select the domain and press `Play` to see an interactive point preview.
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

The dots are a sampled density display, not a rendered volume or durable
cache. Most participant and simulation settings can be adjusted while preview
is running and affect subsequent frames. Changes that alter fundamental sparse
grid allocation may require a new preview session.

Useful preview controls:

- `Preview Resolution` scales the simulation resolution used only by preview.
- `Preview Dots` changes the density-derived point budget.
- `Preview Max Dots` places a hard limit on viewport points.
- Dot size, color, and opacity affect display only.
- `Preview Bake` shows dots during a final bake, with some performance cost.

Press `Stop` to end preview. Native Blender playback is separate from Fumaris
preview and should not be started at the same time.

## Baking And Cache Slots

`Bake` resets Flow and simulates every frame sequentially. It writes an
OpenVDB sequence and imports the completed frames as a Blender Volume object.
The status row reports elapsed time and peak RAM and VRAM observed during the
bake.

Under `Output`:

- Choose a local writable cache directory with enough free space.
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

### Resolution And Sparse Capacity

Resolution determines density cell size. Sparse Block Capacity controls how
much of the world may become active and is also a practical GPU memory ceiling.
Increasing resolution without enough blocks can clip growth or trigger Auto
Cell Size. Increase capacity carefully because a large value reserves more
GPU memory.

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
Normal Velocity pushes away from mesh surfaces.

Mesh `Surface` emission follows the evaluated surface and Emission Distance.
`Volume` fills a closed mesh. A named vertex group or evaluated mesh attribute
can vary emission strength continuously from 0 to 1. Mask Threshold skips
triangles whose average weight is too low; keep it near zero for soft painted
transitions.

`Motion Sub-Steps` sample intermediate source poses for moving or deforming
sources. They do not improve a static emitter or the fluid solve and they
multiply emitter work inside each domain Sub-Step.

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

Exact per-point spheres preserve varying radius. Very large point counts can
increase export and emitter setup cost; reduce point count or radius detail if
they become the frame bottleneck.

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

Effectors support force, wind, vortex, and turbulence behavior with strength,
radius, coupling, sampling, noise, and distance falloff controls. Effectors
change velocity; they do not emit smoke.

### Outflows

Outflows remove smoke and combustion channels in a mesh surface or volume
region. Their mask and motion controls follow the mesh participant workflow.

## VDB Channels And Shading

Density is the standard smoke grid. Optional outputs are:

- `temperature`: Kelvin-like values in a fixed `0-5000` range, suitable for a
  Blackbody node or a custom remap.
- `burn`: raw timestep-dependent combustion activity for advanced control.
- `flame`: a render-oriented emission mask derived from burn and temperature,
  with extreme values smoothly compressed.
- `fuel`: remaining unburned fuel.
- `velocity`: world-space motion used for volume motion blur and compositing.

A simple fire material uses density for Principled Volume Density,
temperature through Blackbody for Emission Color, and flame for Emission
Strength. If you need complete control, export burn instead of flame and build
your own burn/temperature response in the shader.

If a volume disappears only when motion blur is enabled, lower Blender's
volume velocity scale and inspect the velocity grid. Excessive motion bounds
can cause the renderer to reject or miss a volume frame.

## Performance Guidance

Preview is normally much faster than Bake because it reads back only sampled
density points. Bake must read complete grids from the GPU, convert NanoVDB to
OpenVDB, and write them to disk.

For a faster or more memory-efficient simulation:

1. Disable VDB channels not used by the final material.
2. Keep Preview Bake off unless you need to watch the final bake.
3. Lower Preview Resolution or Preview Max Dots for viewport work.
4. Increase resolution and sparse capacity in measured steps.
5. Use the lowest Sub-Step count that gives a converged result.
6. Keep caches on a fast local drive.
7. Reduce exact point count when point-sphere setup dominates a frame.

## Known Beta Limitations

- Hardware coverage is limited and AMD GPUs are currently unqualified.
- Geometry Nodes Volume emission is experimental.
- Preview is temporary and cannot be scrubbed as a durable simulation cache.
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
3. Allow the first shader compilation several minutes to complete.
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

Remove proprietary assets before sending a project file. Beta hardware reports
are welcome even when the issue is specific to an unqualified GPU.
