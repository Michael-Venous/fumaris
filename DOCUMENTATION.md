# Fumaris 2.0 Documentation

Fumaris is an interactive GPU smoke and fire simulator for Blender. Use its
live volume preview to develop motion quickly, then bake standard OpenVDB
sequences for Blender's native volume shading and rendering workflow.

Fumaris 2.0 lights the live preview like Cycles (the scene's lights and world,
fire light, Scatter Glow, Blender's own colour management), adds Realistic
combustion for every domain, and smaller VDB options (temperature floor, Half
Res grids). Preview shading controls do not change baked VDB materials.
See [CHANGELOG.md](CHANGELOG.md) for release details and the Updating section
below before replacing an installation.

## Requirements And Compatibility

- Blender 5.2 or newer (officially supported on all platforms)
- The Windows x64, Linux x64, or macOS ARM64 Fumaris package for your operating system
- Windows/Linux: a Vulkan-capable GPU with a current driver; Mac: Apple Silicon
- Enough GPU memory for the selected Simulation Memory
- Local writable storage for baked OpenVDB sequences

Fumaris has been exercised on NVIDIA GPUs under Windows and Linux and Intel
Arc GPUs under Windows and Linux. Recorded release testing includes an NVIDIA
RTX 3090 on Linux and an Intel Arc 140V on Windows. AMD hardware has not yet
been qualified. It may work through the same Vulkan path, but this release does
not promise AMD compatibility. Apple Silicon support uses Metal through bundled
MoltenVK and was tested on an M4 Mac mini with 16 GB unified memory, macOS
26.6.1 and Blender 5.2.2. Intel Macs are not supported.

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

## The Basic Workflow

Fumaris uses one object as the domain settings host and collections to identify
simulation participants. Unlike Mantaflow, the domain object's dimensions do
not form a hard simulation box; Flow allocates a sparse grid around active
smoke.

1. Select any convenient object and set `Role` to `Domain`.
2. Create an emitter collection and assign it under `Participants`.
3. Put source objects in that collection. Set each one's `Role` to
   `Emitter` and choose its source shape.
4. Select the domain and press `Play` to see an interactive volume preview.
   `Pause` freezes simulation but keeps camera and Appearance rerenders active;
   `Resume` continues from the next frame.
5. Tune the domain, emitters, effectors, and other participants.
6. Press `Stop`, configure `Output`, and press `Bake` for final OpenVDB files.

Participant collections are searched recursively, so organized child
collections are supported. An object is only used when its `Role`
matches the collection role. Turn off a participant's `Enabled` setting to
exclude it without removing it from the collection.

## Preview

`Play` starts a persistent Flow session. Fumaris advances the timeline only
after each simulated frame is ready and loops over the domain's own Start and
End Frame range. These values are independent of Blender's timeline range.

To follow the timeline instead, press the clock button beside Start and End
(`Use Timeline`). The simulation then runs the scene's Start to End frames, and
the Start and End fields are replaced by `Warmup`: the number of frames
simulated before the timeline starts, so the smoke is already moving on the
first visible frame. Warmup frames are baked like any other frame, before the
timeline start. Changing the timeline restarts a running preview. Turning the
button off brings back the domain's own Start and End, unchanged.

New domains, and Quick Setup presets without a fixed length, start with
`Use Timeline` on. Explosion keeps its own short range, and domains in scenes
saved by earlier versions keep the range they had.
The live grid and preview resources remain allocated in VRAM so camera-only
rerenders stay responsive. This is expected; press `Stop` to close the session
and release its GPU working set.

`Volume` mode raymarches the live sparse Flow grid and displays the resulting
image over the 3D viewport. It writes no NanoVDB or OpenVDB output (Export
Cache to VDB writes its cache on request, see Preview Cache). Moving
the viewport or changing display settings rerenders the current Flow frame
without advancing the simulation. The former diagnostic points preview has
been removed; point-based emitters remain supported. A hidden domain can keep
simulating while its viewport overlay is suppressed.

`Show` (Preview Settings) switches the live preview from the Look to Speed:
how fast the air moves, drawn as fire (still air dark, about 5 m/s glowing like
gas at 1600 K). Use it to see where emitters, effectors and expansion push.

The live preview shows in one 3D viewport: the one picked with View > Show
Fumaris Preview Here (until Blender closes), otherwise the largest in the
window the preview was started from. `All Viewports` (Preview Settings) shows
it in every 3D viewport that shows the domain, in any window or on any
monitor; each extra viewport renders the volume again every frame.

`Scene Occlusion` (Preview Settings, on by default) hides preview smoke behind
solid objects, using the viewport's own depth. It follows whatever the viewport
draws, so wireframe objects and overlays that write depth also hide smoke, and
a transparent object occludes like a solid one. Smoke in front of an object is
drawn over it as before. Turn the option off to see the whole simulation.

Most participant and simulation settings can be adjusted while preview is
running and affect subsequent simulated frames. Changes that alter fundamental
sparse grid allocation restart the preview session.

Useful preview controls:

- Live preview uses the volume raymarch. The old diagnostic points path has been removed.
- `Preview Resolution` scales simulation resolution for preview only.
- `Viewport Scale` controls output pixels. Doubling the value renders, reads
  back, transfers, and uploads four times as many pixels.
- `Step Size` is the distance between preview ray samples in simulation cells
  (default 1, like Cycles' Step Rate 1). Preview cost is about proportional to
  the number of samples: smaller is smoother and slower, larger is faster and
  starts to show bands.
- `Frame Rate` paces preview playback: `Scene` plays at the scene's frame rate
  (real speed), `Uncapped` as fast as the GPU simulates, `Custom` at your own
  FPS. Bakes are not paced.
- `Tone Mapping` uses Blender's own colour management: `Scene` follows the
  scene's view transform, look and display; `AgX`, `Filmic` and `Standard` use
  those view transforms. The 8-bit preview image is dithered so dark smoke does
  not band.
- `Exposure` follows the scene's exposure; the offset here adds to it (+1 doubles
  the light). It affects only the preview, not baked grids or materials.
- The domain's `Appearance` group controls smoke density and color plus fire
  visibility, brightness, temperature range, and Blackbody scale. The same
  values drive the live raymarch and Fumaris's generated Blender material.
- `Lighting: Scene` lights the preview with the scene's lights and world in
  Cycles units: the strongest light (or the sun in an HDRI world) with the
  material's anisotropy, the world's light where the smoke sees the sky, and
  multiple scattering when Cycles has volume bounces. `Custom` replaces them
  with one preview-only light (Strength, Azimuth, Elevation) and a uniform grey
  `Sky` of its own, which the smoke shadows (soft ambient darkening in crevices):
  the same lighting in every scene. Strength 0 and Sky 0 show only the fire.
- `Self Shadows` shadows the smoke from that light and from the sky, at some GPU
  cost; off lights every voxel fully. Changing Appearance Density rebuilds the
  shadow field so light and camera attenuation stay consistent.
- `Flame Light` makes flames light the smoke around them. The glow takes the
  flame's color, is strongest beside the fire, falls off with distance and is
  dimmed by smoke in between; darker Smoke Color reflects less of it. 1 is the
  default, 0 turns it off, and it follows Flame Brightness. It is an
  approximation for the preview only: light spreads equally in all directions
  from each flame and does not cast sharp shadows, and it does not light
  objects in the scene. Baked volumes and the generated material are not
  changed; Cycles and EEVEE compute their own fire lighting.
- `Scatter Glow` (Appearance) also shows in the preview: smoke around flames
  glows with their light as in renders.

The live volume preview uses a simplified Flow shader. `Scene Occlusion` uses
viewport depth to hide smoke behind solid objects; transparent objects also
occlude. Its Filmic option is an ACES-style
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
- `Cache Slot` names an independent bake. Each name has its own folder, so
  changing it before baking keeps the earlier bake. Switching slots shows that
  slot's bake, hides the others' volumes and updates the status line; nothing
  is deleted, and switching back brings the earlier bake up again.
  - The `+` button (`New Slot`) moves to the next unused version of the name:
    `main`, `main_001`, `main_002`, or `v2`, `v3`. Versions that already hold
    a bake are skipped.
  - The lock button (`Protect`) locks the current slot. While it is locked,
    Bake and Delete are refused and the Simulate box says so. The lock is a
    small file in the slot's folder, so it also holds if another scene points
    at the same folder.
- Bake clears the current slot before writing. If the slot already holds a
  bake, the Bake button asks first. Delete asks before removing a bake, and
  removes only the current slot's files. Scripts
  that call the operators directly are not asked, but a locked slot still
  refuses them.
- Assign a material to apply it automatically to the imported volume.
- Use `Selectable Volume` to control whether the imported object can be
  selected in the viewport.
- Enable only the VDB channels needed by your material. Each extra grid adds
  GPU readback, conversion time and disk space; velocity also adds GPU memory.
- `VDB Precision` sets how values are stored in the files. **Half** (the
  default) halves file size and write time and is ample for rendering;
  **Full** keeps 32-bit floats for downstream processing that needs them.

`Stop` during a bake imports every valid frame already written. Flow's complete
solver state cannot currently be serialized, so a stopped bake cannot resume;
the next Bake starts from the beginning. `Delete` removes the selected cache
slot and its imported volume. It is unavailable while a job is active.

## Presets & Compare

The `Presets & Compare` section at the top of the domain panel holds the
domain's presets and works with its cache slots as versions of the shot.
Emitters, colliders, effectors and outflows keep a `Presets` menu at the top
of their panel (apply, `+` to save, `X` to delete).

- **Presets**: `+` saves the domain's settings as a preset, available in every
  file; the A and B menus list the saved presets (`X` deletes one). Pick a
  preset for A and for B and apply either, or
  press `Swap` to flip between them. A left as `Current` stands for the
  settings you had before first switching to B, so a preset can be tried and
  undone with one click.
- **Versions** lists every cache slot of the domain that holds frames,
  snapshots or a lock: frame range, and in the tooltip size, date, snapshots
  and note. Click a name to make it the current slot (A);
  its bake is imported if it was not already. The pencil sets a note.
  - `Keep Frame` (image button) keeps the current frame of the bake (files
    hard-linked where the disk allows, so it costs no space), or of a paused
    preview's cache, as a one-frame slot shown on every frame.
  - `Export Cache to VDB` (below the Versions header) writes a paused
    preview's cache as a slot of its own (see Preview Cache).
- **B** on a version shows its bake as B. `Show` picks A only, B only, or Side
  by Side (B moved along X beside the domain); `Swap` makes B current and the
  old current B; `Diff` lists the settings that differ between the two bakes
  (by the settings stored with each), and above them measures of both at the
  current frame (smoke voxels, extent, top height, file size) and what each
  bake cost (time, time per frame, peak VRAM and RAM; bakes from this version
  on); `Use B` takes B's settings. The X stops
  comparing and puts B's volume back. The live preview is not affected by
  Show.
- **Snapshot** saves what the 3D Viewport shows, live preview included, as a
  PNG in the current slot's `snapshots` folder. `A|B Image` puts the latest
  snapshot of A and of B (or A's last two) side by side in an Image Editor.

## Domain Settings

### Resolution And Simulation Memory

Resolution determines density cell size. `VRAM` (Simulation Memory) is the GPU memory,
in GB, given to the simulation. Fumaris allocates all of it when the
simulation starts and turns it into space for smoke, accounting for the
settings that change how much each piece of space costs (Full-Resolution
Velocity, velocity export, mesh emitters and colliders). Memory use therefore stays
flat for the whole bake instead of spiking as the smoke grows. When the smoke
fills that space it is cut off or, with Auto Cell Size, simulated more
coarsely; the panel then asks for more Simulation Memory. At the same memory,
higher resolution covers a smaller region.

Leave room for Blender and anything else on the same GPU, such as Cycles
renders. If the simulation does not fit, Fumaris shows "GPU memory full": the
driver moves data to system memory and the simulation runs very slowly, so
lower Simulation Memory and restart.

`Auto` next to Memory reserves 80% of the GPU memory that is free when the
simulation starts (at least 0.5 GB), read from the driver; the GB value then
only applies if no reading is available. Close other GPU work before starting
for the most space. Drivers without a free-memory reading (some macOS
versions) use half the GPU memory. New domains start on Auto; saved scenes
keep their GB value. The panel shows the amount Auto resolved to at the last
start; Diagnostics shows the voxel size (32 scene units divided by Resolution),
the voxel count and about how much space the budget lets smoke fill. The live
preview's shadow and flame light volumes are part of the same budget.

Resolution has no artificial maximum: values above the slider's suggested
range can be typed directly, but extreme values produce no extra detail when
Simulation Memory or Auto Cell Size is limiting.

Start with the defaults. For energetic or high-resolution effects, raise
Sub-Steps until motion and combustion stop changing materially. Sub-Steps are
complete solver steps and may be set from 1 to 40; cost rises roughly linearly.
`Adaptive` (beside Sub-Steps) makes Sub-Steps the maximum: each frame uses only
as many as the previous frame's fastest flow needs, all of them while the
motion is fast. `Cells per Step` (shown when Adaptive is on, default 4) is how
many voxels the fastest flow may cross per step: lower is more accurate and
slower; around 8 is fast but changes the result noticeably. With Sub-Steps at 6 the test
scenes used 3–5 on average and baked 23–34% faster, with the fast phases close
to always using 6; the result is not identical to a fixed count.

### Air Margin

Only the space near smoke is simulated. Smoke that reaches the edge of that
space before the surrounding air exists shows horizontal steps or block-shaped
terraces, and a plume with too little air around it rises as a column instead
of rolling into a mushroom cap. Both are most visible at high resolution and
low Sub-Steps, where blocks are small and the smoke crosses several per frame.

`Air Margin` (below Simulation Memory) is the distance of clear air kept
on every side of the smoke. It also reaches further ahead of moving smoke, by
how far the smoke will travel before the next allocation. The default of 0.5 m
suits most plumes; large explosions can use 1 m or more. At 0 the smoke still
gets one complete layer of blocks around it, the least the solver needs.

Air Margin adds sparse blocks (typically 1.5x to 2.5x over a margin of 0), so
watch the capacity readout; when capacity runs out, smoke and emitters keep
their blocks and the margin shrinks. The pressure solve always reaches across
the whole plume, so no separate setting is needed for large shapes.

A domain saved by an earlier version uses 0.5 m unless its Air Margin was set
by hand, and so simulates differently from 1.2.1. `Adaptive Boundary Padding`,
the earlier answer to the same stepping, has been removed.

### Fast Emitters, Vehicles And Move With Object

An emitter or collider that crosses many voxels per frame is the hardest case
for any grid solver. The solver guards against it in every scene, and Move
With Object with Carry Air (below Sub-Steps) removes most of the motion in the
first place.

The built-in safeguards keep such scenes stable and compact:

- Vorticity can sharpen swirls up to the speeds around them but cannot push
  past them, so a high Vorticity value with few Sub-Steps no longer runs away
  into exploding motion and smoke thrown out of the simulation.
- A fast emitter is stamped along its whole path each step, however few
  Sub-Steps are used, instead of leaving separate puffs.
- A collider at rest does not reserve simulation space on its own. A long road
  or a large wall used to consume blocks along its entire length before any
  smoke existed.
- Moving air with only a trace of smoke no longer holds its blocks; the Air
  Margin keeps the air that real smoke needs.

`Move With Object` makes the domain simulate in its own object's space. Parent
the domain to the vehicle (or animate the domain itself) and the simulation
grid travels and turns with it. Emitters and colliders that ride along are then
at rest in the grid, so the flow around them is resolved cleanly with a
fraction of the Sub-Steps a scene-space simulation needs. Only the domain
object's location and rotation are used; its scale and its mesh are ignored.

- `Carry Air` at 0 (the default) leaves the air still in the scene. Smoke
  trails behind the moving domain exactly as in a scene-space simulation, and
  objects that do not move in the scene, such as the road, are at rest in that
  air. At 1 the air travels with the domain, as inside a closed vehicle or a
  sealed container; values between mix the two.
- Bakes are written in scene space: each VDB carries its own placement, so the
  imported volume needs no parenting and velocity grids hold scene-space
  motion for motion blur.
- A domain that only travels moves its grid in whole voxels, which costs no
  sharpness. A domain that also turns resamples the smoke every step, which
  softens fine detail slightly; the faster it turns, the more.
- Smoke that is left behind still occupies blocks. For a long run, limit the
  trail with Dissipation or an Outflow parented to the vehicle.
- Sphere, box and mesh emitters and colliders are followed exactly through
  each frame. Particle and VDB sources that are fixed in the scene are placed
  once per frame, so they smear slightly when the domain moves fast.
- Changing Move With Object during preview restarts the simulation. Keep it
  fixed throughout a bake; animating this setting stops the bake with an error.

For a vehicle the camera follows, Move With Object is usually the better
choice. For a vehicle that passes a still camera and leaves a long trail in
view, either works; scene space keeps the resting trail perfectly sharp.

### Smoke Behavior

- `Gravity` is the domain's own gravity (the button beside it copies the
  Scene's). It acts through buoyancy only: hot gas rises against it, and cold
  smoke ignores it unless Smoke Buoyancy is negative.
- `Wind` carries all air along at a steady speed, so plumes lean and trail
  while sources stay put. It costs nothing; use Wind effectors for gusts.
- `Temperature Buoyancy` lifts hot gas. The checkbox beside it, `Physical
  Buoyancy`, replaces it with the gas-law lift for Flame Temperature (gas at
  2000 K is 6.8 times lighter than 293 K air: about 5.8 g).
- `Smoke Buoyancy` controls density-driven lift or sinking.
- `Vorticity` restores small rotational motion lost through advection.
  `Vorticity Mask` chooses where: `Fast Motion` (default), `Smoke` (in
  proportion to density, so thin edges stay calm), `Flames` (hot gas) or
  `Everywhere`.
- `Detail & Shaping` (folded) holds layers of artistic forces and per-cell
  rates (see Detail & Shaping below). A `Disturb` layer pushes smoke and hot
  gas with small random swirls of its `Size` (scene units; keep it a few voxels
  or larger), for breakup the base motion lacks: a Strength of about 50 is
  subtle and 300 strongly mixed on a fireball a few metres across. `Shred`
  stretches the motion across sharp temperature edges, tearing flames into
  tongues; values around 1–5 are typical.
- `Preserve Turbulence` (Advanced)
  (advection–reflection, Zehnder et al. 2018) runs Sub-Steps in pairs that
  lose far less swirling energy to the solver itself: plumes stay lively
  longer and grow larger (in the test scenes kinetic energy ×1.1–2.7, 10–50%
  more space used) for 6–26% more time. It needs 2 or more Sub-Steps.
- `Dissipation` fades smoke over time, by that much density per second. The
  slider stops at 1; type a larger value for dense smoke that must clear fast.
- `Smoke Decay` removes a fraction of the smoke per second, in proportion to
  its density.
- `Velocity Damping` removes a fraction of the motion per second. The default
  **0.01** is close to free air; raise it to calm turbulence and shorten how
  far smoke travels.

### Detail & Shaping

`Detail & Shaping` (in Smoke Behavior) is a stack of layers applied in order.
Add a layer with `+`, reorder with the arrows; the checkbox turns one off.

Layers that add motion:

- `Disturb`: swirling pushes of a set `Size` (scene units; a few voxels or
  larger), with `Detail` adding smaller swirls, each half the size. Strength
  is an acceleration: about 50 is subtle, 300 strong on a fireball a few
  metres across. Stack a large and a fine Disturb for detail at two scales.
- `Grain`: small random kicks, one direction per block of `Size`, renewed
  every frame: the gritty breakup of fast fronts. `Layers` adds blocks three
  times larger (up to 3).
- `Shred`: stretches the flow across steep temperature changes, tearing flame
  edges into strands.

Layers that scale a rate per cell (by their masks):

- `Vorticity`, `Dissipation` and `Cooling` multiply the domain's Vorticity,
  Dissipation and cooling, for example stronger swirls in hot gas or cold
  smoke that fades faster.
- `Soot Burn-off` thins smoke at its rate per second where the masks allow
  (by default where gas is hot and air is left), as soot oxidises.

Masks decide where a layer acts. Each layer holds up to six; each maps a
field from `From Min`–`From Max` to `To Min`–`To Max` and combines with the
masks above it by Multiply, Minimum, Maximum, Add, Subtract or Replace (the
first starts from 1). Fields: Temperature (K), Speed (m/s), Smoke, Fuel,
Burn, Air Left and Noise. New Disturb and Grain layers start with masks that
keep them to smoke or heat (up to 2500 K), so clear air skips the work; a
layer without masks acts everywhere (a scaling layer without masks changes
nothing).

### Combustion

Every domain uses one physical combustion model (the Classic model was removed
in 2.0).

- Gas with fuel that is hotter than `Ignition Temperature` burns a fixed share
  of its fuel each second: 95% within `Burn Time`, so Burn Time is how long
  fire lasts.
- Burning one unit of fuel releases `Fuel Energy` times the heat that takes
  cold air to `Flame Temperature`. Fuel 1 at Fuel Energy 1 burns as hot as fire
  gets; thinner, mixed fuel burns cooler. No gas gets hotter than Flame
  Temperature (emitters included; Emitter Temperature 1 means Flame
  Temperature), which sets the colour: about 1500 K deep orange, 2000–2400 K
  orange to yellow, 3000 K and above pale yellow-white.
- Gas that is still burning keeps its heat; once its fuel runs out it loses
  95% of its heat within `Cooling Time`, so flames fade into embers and smoke.
  Neither Burn Time nor Cooling Time changes how hot fire gets.
- `Temperature Diffusion` evens heat out between neighbouring voxels (the
  share per 1/24 s), smoothing speckle in fire at about 7% more simulation
  time; 0 turns it off. New domains use 0.1.
- `Smoke Amount` is soot per unit of fuel burnt; `Gas Expansion` is how hard
  burning gas pushes outward, per unit of fuel burnt per second, the same at
  any resolution.
- `Wisp Cooling` makes thin gas cool faster, so edges go dark around glowing
  cores. `Flame Sheets` lets dense fuel burn only as fast as that much fuel
  would, so rich cores smoulder and fuel burns in sheets where it mixes with
  air.
- `Oxygen Limit` makes fire need fresh air (0 burns evenly everywhere), so it
  burns in shells where fuel meets air. `Oxygen Field` tracks the air's oxygen
  as it moves: burning uses it up (`Use`) and fresh air mixes back in
  (`Mixing`), so fire starves inside thick burnt gas.
- `Soot Radiation` lets sooty gas radiate its heat away, so dense pockets cool
  and darken while thin hot gas stays bright.
- Temperature Buoyancy is measured at Flame Temperature, so a cooler flame
  lifts less.

Fuel amounts of about 1–3 suit a source that burns where it emits; a source
that expands a lot before burning (a growing fireball) needs more, about
10–30, because its fuel thins out as it mixes with air.

### Advanced

- `Simulation Speed` scales time.
- `Smoke Detail` chooses how smoke and heat are carried between cells:
  Standard (linear) or Sharp (cubic, finer billows, slower). `Edges` (Edge
  Sharpness) sets how strongly each step restores detail lost to
  interpolation; about 0.5 avoids stair-stepped edges in close-ups.
- `Projection Filter` (1 = Flow's) is how strongly the pressure solve smooths
  out cell-to-cell motion; lower lets fire flicker and puff (about 0.25) at
  the risk of grid-scale noise.
- `Preserve Turbulence` (see Smoke Behavior), `Full-Resolution Velocity`
  (motion at the full Resolution: rounder, finer billows at about twice the
  memory per block; changing it restarts the simulation), `Move With Object`
  and `Convex Collision`.
- `Open Collider Wall` is the minimum thickness, in velocity cells, of the
  wall behind open collider meshes such as planes, so smoke cannot leak through
  when Collider Margin is thin. `Closed Collider Margin` (0 = the full Collider
  Margin) holds the air still only that many velocity cells out from closed
  colliders, so fire resting on them rises freely.

## Emitters

Fumaris supports:

- Evaluated mesh surface or volume emission
- Transform-based sphere and box emitters
- Blender particle systems as points or instanced meshes
- Geometry Nodes point clouds and evaluated meshes
- File-based OpenVDB volume emission
- Experimental evaluated Geometry Nodes volume emission

Emitter Smoke, Temperature, Fuel, Burn, and Expansion define the source
channels. Expansion is how fast the emitting region adds volume, per second:
1 makes the gas there grow by its own volume each second and pushes the smoke
around it outward, the same at any Resolution and Sub-Steps. Negative values
pull inward. Emission Velocity adds a constant vector. Motion
Velocity Scale controls velocity inherited from source movement or mapped point
velocities.

Normal Velocity blows out (or, negative, sucks in) along the mesh normals. On a
closed mesh (every edge shared by two faces) it acts as a real source: the air
leaves the surface at that speed and spreads out evenly, falling off with
distance. A closed mesh that grows or shrinks (animated scale, shape keys,
modifiers) pushes or pulls the smoke around it the same way. On open meshes,
particle and Geometry Nodes mesh instances it pushes along averaged vertex
normals, which can be uneven across faces and hard edges. Sphere and Box
emitters made in 2.0 or later blow out evenly from their whole volume.
Point and volume modes do not support this control.

### Source Operations

Each of Smoke, Temperature, Fuel and Burn has an operation beside its rate under
`Channel Coupling`. It decides how the emitter writes that channel:

- `Blend` (default) pulls the field toward the emitter's value at the coupling
  rate. Overlapping emitters settle on a mix, and an emitter with a low value
  thins denser smoke that drifts into it. Existing scenes use this.
- `Add` injects value × coupling every second and removes nothing. Overlapping
  emitters accumulate, and smoke that stays inside the emitter keeps growing.
- `Maximum` raises the field toward the emitter's value but never lowers what
  is already there. Use it to keep a weak source from eroding a thick plume, or
  to ignite with Temperature without cooling hotter fire.
- `Replace` sets the field to the emitter's value everywhere inside it.

A channel whose coupling is 0 stays off in every mode. Velocity and Expansion
always blend. Colliders and outflows are unaffected.

### OpenVDB Sources

An OpenVDB emitter reads `density`, `temperature`, `fuel`, `burn` and
`velocity` grids. `Grid Names` maps differently named grids from other tools;
leave a name empty for the default. Temperature grids are read in Kelvin, as
Fumaris writes them (Temperature 1 re-emits a bake at its own heat); scale
grids in other units with the emitter's Temperature. Fuel and Burn grids are read only when the
emitter's Fuel or Burn value and coupling are above zero. Grids may each have
their own transform and voxel size, as Fumaris bakes do for velocity; they are
resampled onto the smoke grid.

`Sequence` treats the trailing number in the file name as a frame and loads the
matching file for every simulated frame. `Frame Offset` shifts which file a
scene frame reads. Missing files and valid empty grids emit nothing, so a
sequence can start late, contain gaps, or end early. Unreadable files still
report an error. To feed a Fumaris bake back in, pick any frame of the bake,
enable `Sequence`, and keep the original cache untouched while it is in use.

`Emit Velocity Grid` drives the simulation with the file's velocity at the
Velocity coupling rate. It is off by default, so a stationary volume does not
slow or push the fluid. Volume emitters start in `Blend`, which also pulls
smoke inside the file's bounding box down toward the file's density; choose
`Maximum` or `Add` for Smoke when the source should only contribute.

Mesh `Surface` emission follows the evaluated surface and Emission Distance.
`Volume` fills a closed mesh inward from its surface up to Emission Distance;
set the distance to at least half the mesh's thickness to fill it completely.
A named vertex group or evaluated mesh attribute
can vary emission strength continuously from 0 to 1. Mask Threshold skips
triangles whose average weight is too low; keep it near zero for soft painted
transitions.

Leave `Mask` empty to emit from the whole mesh. The mask must exist on the
evaluated output: new geometry made by a Geometry Nodes primitive does not
automatically inherit weights painted on the original object. Use `Store Named
Attribute` on the generated mesh and enter that attribute's name in `Mask`, or
clear `Mask`. If every face is below the threshold, Fumaris reports that the
mask excludes all faces rather than treating the node output as missing.

`Velocity` names a vector attribute on the evaluated mesh that sets the
source's own velocity, in object-space units per second, on points, faces or
face corners (faces and corners are averaged onto the vertices). Store it with
`Store Named Attribute` in Geometry Nodes, or use one imported with a cache.
It replaces the velocity Fumaris measures from the mesh's deformation, which is
needed when the mesh's vertices change from frame to frame, and is scaled by
Motion Velocity Scale. Moving the object itself still adds its motion.
Instanced meshes need `Realize Instances` for their attributes to be read.

`Motion Sub-Steps` sample intermediate source poses for moving or deforming
sources. They do not improve a static emitter or the fluid solve and they
multiply emitter work inside each domain Sub-Step.

### Collection Points

For many separately animated mesh objects, use one controller object with
`Role: Emitter` and `Shape: Collection Points`. Put this controller in
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
frame have no inherited motion; explicit Emission Velocity still applies. Disabling
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

### Participant list

`Objects` under Participants lists everything in the domain's collections.
Click a name to select it (Shift adds to the selection), toggle Enabled, or
Solo one emitter, collider, effector or outflow: the others of its kind are
disabled until you click Solo again, also while previewing. Objects the
simulation skips are shown in red with the reason, for example an object in
the emitter collection that is not set as a Fumaris Emitter.

## Colliders, Effectors, And Outflows

Assign optional collections to the domain's participant fields.

### Colliders

Mesh, box, and sphere colliders transfer boundary velocity into smoke. Mesh
`Convex Collision` treats a suitable closed convex mesh as a filled volume;
unsupported meshes fall back to the regular shell behavior. Increase Collider
Margin for fast smoke or thin geometry.

Colliders are walls for the pressure solve: smoke cannot be pushed into
them, so it stops at, spreads along and wraps around their surfaces, and the
collider's own motion pushes the air. Fumaris also sets the collider's velocity
inside it and removes any smoke that still gets in. Geometry thinner than
about one velocity cell (two density cells) is too thin to form a wall and
only applies its velocity, so very fast smoke can still pass through thin
sheets; give them some thickness or raise Resolution.

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

The 3D viewport draws every visible effector's reach: a sphere of its
Influence Radius (or Max Distance), the Min Distance sphere, the wind direction
or force arrows, and a vortex's cylinder, core, spin and updraft. Selected
effectors are drawn brighter. Hide the object to hide its display.

`Field Mask` limits any effector to where the simulated smoke meets a
condition. Choose Smoke, Temperature, Fuel, Burn or Speed, then a `Start` value
where the force begins and a `Full` value where it reaches full strength.
`Invert` acts at full strength below Start and fades toward Full. The mask
reads the live field on the GPU each step, so it follows the smoke rather than
the effector's shape. Examples: turbulence masked by Temperature breaks up the
flame but leaves cold smoke alone; turbulence masked by Smoke with Invert and a
small range stirs only thin wisps; drag masked by Speed slows only fast gas.
Temperature uses the simulation's 0 to 1 range, as Ignition Temperature does;
Speed is in meters per second.

### Outflows

Outflows remove smoke and combustion channels in a mesh surface or volume
region. Their mask and motion controls follow the mesh participant workflow.

## VDB Channels And Shading

Density is the standard smoke grid. Additional outputs are:

- `temperature`: Kelvin-like values in a fixed `0-5000` range, suitable for a
  Blackbody node or a custom remap. Gas below 500 K, which cannot glow, is not
  stored. Each grid (Density, Temperature, Fuel, Burn, Velocity) can be
  written at Full, Half or Quarter resolution (Output) for smaller files;
  Density, Fuel and Burn keep their amount (empty space counts as zero),
  Temperature and Velocity average the gas only. Velocity is simulated at half
  resolution and written at Half by default; Full appears with Full-Resolution
  Velocity. VDB Precision Half (default) stores 16-bit values.
- `burn`: raw timestep-dependent combustion activity for advanced control.
- `fuel`: remaining unburned fuel.
- `velocity`: world-space motion used for volume motion blur and compositing.

With fire, bakes also write a coarse `glow` grid (fire light blurred into the
smoke around it, for Scatter Glow), `glow_wide` (the same light spread four
times farther, for Glow Spread) and `glow_depth` (the least smoke between a
cell and the fire, for Glow Shadowing). `Glow Radius` (Output) sets how far the
light reaches; 0 follows the size of the fire.

`Look` chooses the fire shading, and each look keeps its own flame controls
(Brightness, Full Glow, Contrast, Blackbody Scale, Blue Core, Flame Tint and
the Scatter Glow settings), so switching looks does not change the other's
values. `Temperature` glows by each voxel's temperature and density.
`Pyro` follows Houdini's Pyro shading with the same brightness law, so equal
values give equal exposure; Blackbody Scale shifts its golden ramp (above 1
toward pale gold) and Blue Core works as in Temperature. `Colour` picks the
golden ramp or real blackbody colour (as Temperature, with Pyro's soft glow,
Thin Gas and dark soot): fire takes a golden colour, glows from the hotter of the gas and its
blurred surroundings (the baked `heat` grid), so cells glow softly and nearby
smoke picks up their light, and hot soot turns dark. `Thin Gas` is the smoke
density at which Pyro glows as brightly as Temperature; thinner gas glows up to
twice as bright, denser smoke shows as dark soot over the fire. It holds at any
scene size, and Full Glow changes need no re-bake. Pyro needs a bake from
Fumaris 2.0 or later (heat and soft density grids, written with the glow
grids).

When `Volume Material` is unset, Fumaris creates a material driven by the
domain's Appearance settings; changing them does not require a rebake.

- Smoke: `Density`, `Color` and `Anisotropy` (forward scattering, 0.3 by
  default, like real smoke).
- Fire glows by its temperature like soot on a camera exposed for the hottest
  gas: gas at `Full Glow` glows at full `Brightness`, hotter gas brighter,
  cooler gas falls off by `Contrast` (lower = wider, fuller flames). `Colour
  Spread` below 1 compresses the colour range (an orange body without
  white-hot or deep-red extremes); `Blackbody Scale` multiplies the temperature
  before the Blackbody colour; `Flame Tint` multiplies the flame colour.
- `Scatter Glow` adds the baked fire light in the surrounding smoke, so dark
  billows glow from inside near the flames. `Glow Spread` blends in the
  wider glow for a long, soft falloff; `Glow Shadowing` lets smoke between a
  spot and the fire block it, so dense outer billows stay dark and fire light
  shows in the crevices next to the flames. Cycles' unbiased volumes render
  the glow in square patches; Fumaris then shows a hint and a Use Biased
  button.

The live preview uses the same settings. The material reads only the density,
temperature and glow grids (and fuel when Blue Core is on); `burn` stays
available for custom materials and compositing.

When a custom `Volume Material` is assigned, enable the additional grids that
material needs explicitly. Fumaris does not export a separate derived `flame`
grid.

If a volume disappears only when motion blur is enabled, lower Blender's
volume velocity scale and inspect the velocity grid. Excessive motion bounds
can cause the renderer to reject or miss a volume frame.

## Performance Guidance

Preview is normally much faster than Bake because it reads back one
viewport-sized RGBA image (and its cache, compressed in the background). Bake must read complete grids from the GPU,
convert NanoVDB to OpenVDB, and write them to disk.

For a faster or more memory-efficient simulation:

1. Disable VDB channels not used by the final material.
2. Write Temperature, Velocity or other grids at Half or Quarter resolution when file size matters.
3. Lower Preview Resolution or Viewport Scale, or raise Step Size, for viewport work.
4. Increase resolution and sparse capacity in measured steps.
5. Use the lowest Sub-Step count that gives a converged result.
   For stepping at the smoke's edge, raise Air Margin before Sub-Steps.
6. Keep caches on a fast local drive.
7. Split point clouds with widely different radii and reduce excessive point
   counts when point emission dominates a frame.

## Known Limitations

- Hardware coverage is limited and AMD GPUs are currently unqualified.
- Geometry Nodes Volume emission is experimental.
- The preview cache lasts until the preview stops; export it to keep it.
  Cached frames are for looking back, not checkpoints: Resume always continues
  from the live frame.
- Volume preview shading is intentionally simpler than a final Blender volume
  material. Scene Occlusion uses viewport depth, so transparent objects occlude
  as if solid.
- Stopped bakes cannot resume solver state.
- Only one Fumaris preview or bake may run at a time.
- Colliders are improved Flow collision emitters, not sealed pressure
  boundaries.
- Results do not match Mantaflow parameter-for-parameter.
- Mac requires Apple Silicon, macOS 26+ and Blender 5.2+. Intel Macs are unsupported.

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

With Role set to **None**, click **Quick Setup…** in Physics → Fumaris, or choose it from the 3D Viewport's
Add menu. **Rising Smoke**, **Steady Fire**, **Explosion**, **Ground Fog** and
**Chimney Smoke** create a complete starting setup. Choose **Selected Objects** for unassigned mesh sources, or
**New Source** to create an icosphere at the 3D cursor. Existing participants are
left intact; the new setup has separate emitter, collider, effector and outflow
collections. The new simulation object is an axes empty, with no physical domain bounds.

Presets start from the defaults of a new domain and change only what makes the
effect. Rising Smoke emits continuously without fuel; Steady Fire emits smoke and
fuel that burns out within half a second, so flames stay a few source widths tall.
Explosion emits a burst of fuel for 8 frames, then vents warm soot that fades
out over 80 frames (the stem), with a 120-frame range; strong buoyancy and
Disturb lift it into a billowing mushroom cloud on that stem.
Ground Fog emits cold smoke that is heavier than air, and adds a collider floor
just under the source for it to spread over. Chimney Smoke blows warm smoke
straight up at 4 m/s.
Ranges start at the scene's Start Frame. These are editable starting points.
The simulation empty stores settings; smoke can extend freely around it.

### Preview Cache

With **Cache Preview** on (under Play; off by default, applies from the next
Play), the live preview keeps the frames it simulates, compressed in system
memory, at full detail (exactly what the preview drew). Without it the preview
loops over its range and keeps nothing. While a cached preview is paused:

- Scrub or play the timeline (Blender's own playback) to see the cached frames
  with the live camera, lighting and look. The status line shows the cached
  range and the frame shown.
- **Resume** returns to the live frame and continues the simulation from it.
  At the end of the range the preview pauses; Resume then starts over (a new
  cache).
- **Export Cache to VDB** (Presets & Compare) writes the cached frames as VDB files into a new
  cache slot (a version, see Presets & Compare), with the Output settings
  (grids, resolutions, Glow) at the preview's resolution. It imports, renders
  and compares like a bake. **Keep Frame** in Presets & Compare exports one
  cached frame.
- **Stop** releases the cache with the simulation.

Preferences > Preview Cache chooses how often frames are kept. Each kept frame
is copied from the GPU to system memory, which takes about 40 ms per GB on a
PCIe 4.0 x16 link and several times that on a narrower one (a card in an x4
slot). **Balanced** (the default) measures the link and spends about a fifth of
the frame time on the copy, spread evenly over the frames that follow a kept
one, so playback stays smooth: small scenes keep most frames, large ones every
few. **Every Frame** always keeps every frame, whatever the cost. RAM sets how much
memory the cache may use (0: a quarter of the RAM, 1 to 16 GB). When it is full, every other frame of the
older half is dropped, so the whole range stays reachable at a coarser step;
scrubbing to a dropped frame shows the nearest earlier one.

### Capacity warning and defaults

A capacity warning appears beside the simulation controls even when Diagnostics
is collapsed. At full capacity smoke may be cut off: increase **VRAM**
(Simulation Memory) or lower **Resolution**, then restart. A full-capacity warning remains
for that run because freeing blocks later does not recover missing smoke. A new
run or Delete clears it. Diagnostics sits below Advanced on the panel's darker background;
its exact appearance follows your Blender theme.

Current defaults are Dissipation **0.5**, emitter Fuel **1**, Temperature Per Burn
**1**, and preview Step Size **1** cell. Explicitly stored settings retain their
values; older unset properties inherit the new defaults. The Rising Smoke setup
explicitly uses zero fuel. All Quick Effects use dissipation 1 and emitter
Expansion 0.2; Rising Smoke and Steady Fire use Normal Velocity 0.25, Explosion
2. New emitters start at Expansion 0.2.


### Choosing the simulation GPU

Use **GPU** in the domain's **Simulate** controls, or **Simulation GPU** in
Fumaris preferences. Click the refresh icon to list Vulkan GPUs, then choose a
card. The choice applies to new Preview and Bake jobs and is shared across
scenes. Save Blender preferences to keep it after restarting Blender.

**Automatic** prefers a discrete GPU and preserves launch environment overrides.
An explicit choice uses the card's UUID, so a driver changing device order does
not redirect the simulation. An unavailable saved GPU stops the job with an
error; refresh and choose another card or Automatic. Changing the choice does
not move an already running or paused preview to another GPU; stop it first.
Diagnostics reports the GPU actually used by the worker.

Discovery lists identifiable Vulkan GPUs, excluding software CPU devices. It
does not qualify driver compatibility; an unsupported device or driver reports
its startup error when the simulation starts. A custom bridge must support
`--list-devices` and UUID selection. This feature does not expand the officially
tested hardware list.
