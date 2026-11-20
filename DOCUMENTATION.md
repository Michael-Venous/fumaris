# Fumaris 2.0 Documentation

Fumaris is an interactive GPU smoke and fire simulator for Blender. Use its
live volume preview to develop motion quickly, then bake standard OpenVDB
sequences for Blender's native volume shading and rendering workflow.

Fumaris 2.0 lights the live preview like Cycles (the scene's lights and world,
fire light, Scatter Glow, Blender's own colour management), adds one physical
combustion model for every domain, and smaller VDB options (temperature floor,
Half Res grids). Preview shading controls do not change baked VDB materials.
See [CHANGELOG.md](CHANGELOG.md) for release details and the Updating section
below before replacing an installation.

## Requirements And Compatibility

- Blender 5.2 or newer (officially supported on all platforms; tested with
  5.2.3 and 5.3.0)
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
simulation participants. The domain's panel runs top to bottom in the order you
usually need it: `Simulate` (Play, Bake, frame range, Resolution and memory),
`Look` (smoke and fire appearance and the live preview's settings), `Smoke
Behavior` (with Combustion), `Modifiers` (detail, rates and particles, folded
until you need them), `Participants`, `Output`, `Versions` and `Advanced`. Unlike Mantaflow, the domain object's dimensions do
not form a hard simulation box; Flow allocates a sparse grid around active
smoke.

1. Select any convenient object and set `Role` to `Domain`.
2. Create an emitter collection and assign it under `Participants`.
3. Put source objects in that collection. Set each one's `Role` to
   `Emitter` and choose its source shape.
4. Select the domain and press `Play` to see an interactive volume preview.
   `Pause` freezes simulation but keeps camera and Look rerenders active;
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
image over the 3D viewport. It writes no NanoVDB or OpenVDB output (Save
Sequence writes its cache on request, see Preview Cache). Moving
the viewport or changing display settings rerenders the current Flow frame
without advancing the simulation. The former diagnostic points preview has
been removed; point-based emitters remain supported. A hidden domain can keep
simulating while its viewport overlay is suppressed.

`Show` (Look > Live Preview) switches the live preview from `Smoke & Fire` (as
the Look renders it) to Speed:
how fast the air moves, drawn as fire (still air dark, about 10 m/s dim red,
20 m/s red, 50 m/s and faster orange). Use it to see where emitters, effectors and expansion push.

The live preview shows in one 3D viewport: the one picked with View > Show
Fumaris Preview Here (until Blender closes), otherwise the largest in the
window the preview was started from. `All Viewports` (Look > Live Preview) shows
it in every 3D viewport that shows the domain, in any window or on any
monitor; each extra viewport renders the volume again every frame.

`Scene Occlusion` (Look > Live Preview, on by default) hides preview smoke behind
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
- `Color Management` (View and Look) uses Blender's own colour management,
  like the scene's Color Management panel. View `Scene` follows the scene's view transform, look and
  display, so the preview matches the render. Any other View (`Standard`,
  `Khronos PBR Neutral`, `AgX`, `Filmic`, `ACES`) takes the Look beside it, for
  example `AgX` with `Punchy` for more saturated fire. They change only the
  preview: set the scene's own View and Look to render the same way. The 8-bit
  preview image is dithered so dark smoke does not band.
- `Exposure` follows the scene's exposure; the offset here adds to it (+1 doubles
  the light). It affects only the preview, not baked grids or materials.
- The domain's `Look` section controls smoke density and color plus fire
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
  cost; off lights every voxel fully. Changing the Look's Density rebuilds the
  shadow field so light and camera attenuation stay consistent.
- `Fire Lighting` makes flames light the smoke around them. The glow takes the
  flame's color, is strongest beside the fire, falls off with distance and is
  dimmed by smoke in between; darker Smoke Color reflects less of it. 1 is the
  default, 0 turns it off, and it follows Flame Brightness. It is an
  approximation for the preview only: light spreads equally in all directions
  from each flame and does not cast sharp shadows, and it does not light
  objects in the scene. Baked volumes and the generated material are not
  changed; Cycles and EEVEE compute their own fire lighting.
- `Scatter Glow` (Look) also shows in the preview: smoke around flames
  glows with their light as in renders.

The live volume preview uses a simplified Flow shader. `Scene Occlusion` uses
viewport depth to hide smoke behind solid objects; transparent objects also
occlude. Its Filmic option is an ACES-style
approximation; AgX (Preview) uses a polynomial approximation of the original
AgX transform. Neither is Blender's full OCIO view transform. The preview does not
follow scene color-management settings; final OpenVDB shading and rendering
inside Blender are unaffected.

Press `Pause` to inspect the frozen live grid from other angles without
advancing it. Press `Stop` to end preview and release its simulation's GPU
memory. Without Cache Preview it also clears its viewport image. With Cache
Preview the cached frames stay after Stop (see Preview Cache). Native Blender
playback is separate from Fumaris preview and should not be started at the same
time.

## Baking

`Bake` resets Flow and simulates every frame sequentially. It writes an
OpenVDB sequence and imports the completed frames as a Blender Volume object.
The status row reports elapsed time and peak RAM and VRAM observed during the
bake.

- The first bake needs no choice. Once a bake exists, Bake asks: **Replace**
  the selected pinned bake in Versions (or, with none selected, the one the
  domain shows), or **New Bake**, which keeps the others. Rebaking replaces, so
  nothing piles up unless you choose New Bake.
- A bake is pinned in Versions. Pin a preview save there to make it a bake
  that Bake can replace.
- Play works after a bake: the bake stays in Versions (its eye shows it again)
  and the preview never writes over it.
- `Delete Bake` (in the menu beside Bake, and the bin icon below the bake's
  name under Output) asks before removing the bake the domain shows and
  removes only its files. It is unavailable while a job is active. Scripts that
  call the operators directly are not asked, but a protected bake still
  refuses them.

Under `Output`:

- Choose a local writable cache directory with enough free space. Before the
  project is saved, default/project-relative output uses Blender's user data
  directory (system temporary storage if unavailable). An explicit absolute
  output directory is respected. After saving, project-relative output is
  resolved beside the `.blend`; earlier caches stay at their original location.
- `Version` names the bake the domain shows. Below it: `Lighter` (make a
  lighter copy), `Sparks`, the lock (`Protect`) and the bin (`Delete`). While
  it is locked, Bake and Delete refuse it and the Simulate box says so. The lock is a small file in the bake's
  folder, so it also holds if another scene points at the same folder.
- Assign a material to apply it automatically to the imported volume.
- Use `Selectable Volume` to control whether the imported object can be
  selected in the viewport.
- Enable only the VDB channels needed by your material. Each extra grid adds
  GPU readback, conversion time and disk space; velocity also adds GPU memory.
  Without a material of your own, Density is always written (the generated
  material reads it), shown ticked and greyed; Temperature is written when
  ticked (on by default): without it the generated material shows smoke
  only, and the Output panel says so in a scene with heat. Empty
  `Directory` and `Prefix` fields show what they fall back to.
- `VDB Precision` sets how values are stored in the files. **Half** (the
  default) halves file size and write time and is ample for rendering;
  **Full** keeps 32-bit floats for downstream processing that needs them.

`Stop` during a bake imports every valid frame already written. Flow's complete
solver state cannot currently be serialized, so a stopped bake cannot resume;
the next Bake starts from the beginning.

After the last frame is simulated, the bake still writes the frames waiting
for the disk; the status shows `Writing the last frames to disk…`. At high
resolution on a hard disk or network drive this can take minutes: the bake
waits for it and ends by itself. `Stop` in that phase ends the bake at once:
the frames already written are kept and imported, the ones still being
written are lost.

### Bake in Background

`Bake in Background` (in the menu beside Bake) bakes the domain in a
separate Blender process, so you can keep working: edit the scene, play a live
preview or bake another domain. `Bake All` and `Bake Selected`, in the Fumaris
tab of the 3D Viewport's sidebar, do the same for every domain in the scene or
for the selected domains. Background bakes run hidden, without a live preview,
whatever `Preview While Baking` is set to; the volume appears when the bake is
done.

- A background bake uses the scene as it was when you clicked, unsaved changes
  included. Edits you make afterwards do not reach it.
- It asks the same question as Bake (Replace or New Bake) and writes the same
  files to the same place.
- **Background Bakes** in the Fumaris preferences (Baking) sets how many
  domains bake at once, from 1 to 8; the default, 0 (Auto), bakes one per
  ticked GPU (one if none is ticked). The others wait their turn.
- Under **On GPUs**, tick the GPUs they may use (the refresh icon beside
  Simulation GPU lists them). The bakes take the ticked GPUs in turn: with two
  bakes and two GPUs ticked, each GPU runs one. With none ticked they use the
  Simulation GPU.
- With more than one GPU in use, each bake's progress says which GPU it runs
  on.
- When more bakes run than GPUs are ticked, several share a GPU. With Auto,
  the bakes that start together on one GPU split what Auto would take
  between them; a fixed `VRAM (GB)` is used as set. A bake that runs alone on
  its GPU takes all of it, whatever Background Bakes is set to.
- While a domain bakes, its panel and its row in the sidebar show the frame,
  the speed and the time left, with a `Stop`. The 3D Viewport and Timeline
  headers show the background bakes in one short line ("Baking 2 · 45%", the
  queued ones counted at 0%) with an `X` that stops them all. Stop keeps the frames already
  written, like Stop during a Bake. A second click (`Stop Now`) ends the bake at
  once and loses the frame it was writing.
- After the last frame the status shows `Writing the last frames to disk…`
  while the last files are written (minutes on a slow disk at high
  resolution); the bake then ends by itself. Stop in that phase ends it at
  once, keeping the frames already written.
- Bake, Delete and Play are unavailable for that domain until its bake ends.
  Other domains are not affected.
- When it ends, the bake is imported and shown like any bake. If you are in
  Edit Mode (or any mode other than Object Mode) or a render is running, the
  volume appears when you are back in Object Mode or the render is done.
- If a background bake fails, Blender reports the error and the domain's status
  line shows it. The bake's log is kept; the report names the file.
- Opening another file or quitting Blender stops the background bakes after
  the frame they are on. The frames written so far are kept.

A background bake costs about two seconds more than Bake (its Blender starts
and loads the scene); its frames are as fast or faster, since it draws no live
preview. Several domains with Bake All finish sooner than one Bake after the
other. On a hard disk or a 4-core processor, writing the VDB files sets the pace
of both. Each background bake is a second Blender with the scene loaded, so it needs
its own system memory. Other add-ons are not loaded in it: motion that another
add-on computes on each frame is missing, so bake such scenes with `Bake`.
Python drivers run in it only when Auto Run Python Scripts is on in Blender's
preferences (Save & Load). The copy of the scene it bakes from is kept in
Blender's Temporary Files folder (Preferences > File Paths) until the bake ends.

### Sparks

`Sparks` (below `Version` under `Output`) adds an object that makes sparks
and embers from the bake the domain shows. They start in gas hotter than
`Spawn Temperature`, are carried by the baked velocity, fall, cool and
disappear. The object goes into the baked volume's collection, selected, and
already shows the sparks of the current frame; play the timeline to see them
move. (A Geometry Nodes simulation: scrubbing backwards past what has been
played starts it over there, as in Blender.)

- The bake needs the `Velocity` and `Temperature` grids. Without them Add
  Sparks turns them on and asks you to bake again. If a later bake lacks them,
  the panel says so beside the Bake row (and Bake reports it) and turns them
  on again for the next bake.
- Sparks start in hot gas that rises at least `Lift Speed` (0.5 m/s), as
  embers are lifted by rising gas.
- The settings are on the sparks object's Geometry Nodes modifier: `Sparks per
  Frame`, `Lift Speed`, `Lifetime`, `Drag` (how closely sparks follow the gas), `Gravity`,
  `Cooling Time`, `Radius`, `Brightness` and more, each with a tooltip.
- `Spawn Temperature`, `Gravity` and `Simulation Speed` are taken from the
  domain when the sparks are added. Change them on the modifier if you later
  change the domain's Flame Temperature, Simulation Scale or Simulation Speed.
- The sparks follow the domain: after another bake, or after choosing another
  version, they are simulated again up to the frame on screen (the frames a
  living spark can have started in; a short pause after the bake). A version
  that is not in the scene has no sparks until it is imported (see Versions).
- Sparks render in Cycles and EEVEE with the `Fumaris Spark` material, coloured
  by their temperature. `Brightness` 0.1 keeps the hottest sparks orange under
  AgX; above about 0.3 they render white. Cycles' motion blur smears them along
  their movement (they carry a `velocity` attribute).

## Versions

The `Versions` section (below Output) lists what the domain has
saved, in one list:

- **VDB versions** (volume icon): bakes, and live previews kept with Save
  Sequence or Save Frame (see Preview Cache). Each is a VDB sequence with the
  settings that made it, at the resolution it was simulated; it imports and
  renders in EEVEE and Cycles like any volume. The row shows its frames.
- **Snapshots** (picture icon): viewport images from Save Snapshot, each with
  the settings of the moment it was taken.

Pinned entries (the pin on a row) stay at the top, above a line; bakes are
pinned. Any entry can be pinned or unpinned; Bake offers to replace a pinned
VDB version. The button at the right of a row is a quick look: the eye shows or
hides a VDB version in the viewport (imported the first time), the picture opens
a snapshot in an Image Editor.

Click a row to select it; its buttons appear below the list:

- VDB version: **Import** brings it into the scene as a volume object to render
  and keep (**Remove** takes it out again; its files stay), **View Changes**,
  and a menu with **Rename**, **Save Settings as Preset** and **Delete**.
- Snapshot: **Preview** opens it, **View Changes**, and the same menu.
- **Sync Material** (VDB versions): on, the version uses Fumaris's material,
  kept in step with Look (shared with the other synced versions; your
  Volume Material if one is set). Off, it gets its own copy, which Look
  no longer changes: a look you liked stays put. Bakes start synced; saves from
  the live preview start with their own copy.
- **View Changes** lists every setting that differs from the current settings,
  with **Load These Settings** below. For a bake other than the version the
  domain shows, it also measures the two against each other (**Measured**):
  smoke and heat voxels, the amount of smoke and of heat (each grid's values
  summed over its volume), the smoke's extent and top (how high it rose) at the
  current frame (or the last frame both have), the frame's file size, and the
  bakes' time, time per frame, peak VRAM, peak RAM and peak sparse blocks, with
  the change in percent. They come from the VDB files' headers and the bake's
  stored statistics, so they appear at once; older bakes (2.0 beta 3 and
  before) have no peak blocks and no amounts. Loading replaces the current settings of
  the domain and its sources (sources are matched by name) after a warning; Undo
  brings them back.

Right-click a row for the same actions. Rename changes the name shown, not the
files. Delete asks first and cannot be undone; a protected bake refuses it.
Versions can be deleted during a live preview (not while baking), except the
one being saved.

The domain's **Presets** menu sits at the top of its panel, as for emitters,
colliders, effectors and outflows: apply a preset (`X` deletes it) or `+` to
save the current settings as one, available in every file.

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
driver moves data to system memory and the simulation runs very slowly. Lower
VRAM or close other GPU programs, then press Play (or Bake) again; with Auto
on, close other GPU programs or turn Auto off and set a lower VRAM.

VRAM is GPU memory only. The preview cache (Cache Preview) keeps its frames in
system RAM, with its own limit (Preferences > Preview Cache RAM); its figures
in the panel say RAM.

`Auto` next to Memory uses the GPU memory that is free when the
simulation starts, less a reserve of 6% of the GPU's memory (12% for a
bake; at least 1 GB) for Blender, the desktop and Fumaris's own buffers
outside the smoke's space (emitters, the VDB export, the preview cache's
copies); at least 0.5 GB. On a free 24 GB card that is about 22 GB for
the live preview and 20.5 GB for a bake. It is read from the driver; the GB value then
only applies if no reading is available. Close other GPU work before starting
for the most space. Drivers without a free-memory reading (some macOS
versions) use half the GPU memory. Auto is on for new domains (Quick Setup,
New Domain, or Role set to Domain); the result does not depend on the amount,
only how much room the smoke has. Turn it off to reserve a set amount, for
example to leave memory to other programs (Quick Setup's domains then use
2 GB, which holds every preset with room to spare). A set amount larger than the GPU's free memory
starts with only what fits on the GPU (as much as Auto would take) and grows past it only when
the smoke needs the room; that part goes to system memory and runs slowly ("GPU memory full").
The field flags an amount above the GPU's size. The panel shows the amount Auto resolved to at the last
start, with the free memory it was read from and what it left free, for
example `Auto VRAM: 9.0 GB` over `10.0 GB free, 1.0 GB left`; the VRAM bar's
total is that amount. Auto reads the free memory once, when the simulation
starts: a program started afterwards (a Cycles render, a game) is not
accounted for until you Stop and Play again. Diagnostics shows the voxel size (32 scene units divided by Resolution),
the voxel count and about how much space the budget lets smoke fill. The live
preview's shadow and flame light volumes are part of the same budget.

Not every field is stored at the Resolution you set, and none where the scene
does not use it. Smoke and temperature are; motion (velocity and pressure) is
simulated at half of it unless `Full-Resolution Channels` is on; a fire's fuel
and burn are kept at half of it too, on the motion's grid: flames keep their
detail, size and edges, and 1.6 times the blocks fit in the same memory for
7 to 9% more frame time. At low Resolution (around 200) an explosion burns a
few percent cooler than with full-resolution fuel, more with Oxygen Limit or
the Oxygen Field; the difference shrinks as Resolution rises.
Scenes without fire store no fuel or burn, and scenes without heat no
temperature. This is automatic, also with `Full-Resolution Channels`. Fuel
and burn go back to full resolution while `Full-Resolution Channels`
(Advanced), the Fuel or Burn output or `Per-Emitter Smoke Color` is on.

Resolution has no artificial maximum: values above the slider's suggested
range can be typed directly, but extreme values produce no extra detail when
Simulation Memory or Auto Cell Size is limiting.

Start with the defaults. For energetic or high-resolution effects, raise
Sub-Steps until motion and combustion stop changing materially. Sub-Steps are
complete solver steps and may be set from 1 to 40; cost rises roughly linearly.
`Cells per Step` (beside Sub-Steps, default 8; 0 turns it off) makes Sub-Steps
a maximum (shown as `Max Sub-Steps`): each frame uses only as many steps as the
previous frame's fastest flow needs to cross at most that many voxels per step.
Lower is more accurate and slower. It has nothing to do at 1 Sub-Step, where it
is greyed out. With Sub-Steps at 6 the test scenes used 3–5 on average and baked
23–34% faster, with the fast phases close to always using 6; the result is not
identical to a fixed count.

### Air Margin

Only the space near smoke is simulated. Smoke that reaches the edge of that
space before the surrounding air exists shows horizontal steps or block-shaped
terraces, and a plume with too little air around it rises as a column instead
of rolling into a mushroom cap. Both are most visible at high resolution and
low Sub-Steps, where blocks are small and the smoke crosses several per frame.

`Air Margin` (below Simulation Memory) is the distance of clear air kept
on every side of the smoke, measured from the smoke itself. It also reaches
further ahead of moving smoke, by how far the smoke will travel before the
next allocation. The default of 0.5 m suits most plumes; large explosions can
use 1 m or more. At 0 the smoke still keeps a few cells of air around it, the
least the solver needs.

Air Margin adds sparse blocks (typically 1.5x to 2.5x over a margin of 0), so
watch the capacity readout; when capacity runs out, smoke and emitters keep
their blocks and the margin shrinks. The pressure solve always reaches across
the whole plume, so no separate setting is needed for large shapes.

A block of air costs about a fifth of a block of smoke: it stores the
velocity field only, and gets its smoke fields (density, temperature, fuel,
burn) just before anything can move into it. Diagnostics therefore shows two
counts when Air Margin is above 0, `Sparse blocks` (all of them) and `With
smoke storage`, each against its own capacity; the capacity warning follows
whichever is fuller. `Voxels` counts the stored ones.

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

- `Speed` (Simulation Speed) scales time: 0.5 plays the motion at half speed,
  and it can be animated for slow motion. `Scale` (Simulation Scale) is the
  physical size the scene stands for: 2 makes the simulation behave twice as
  large (slower-looking, heavier plumes), with the scene's dimensions,
  voxel size and speeds in Blender units unchanged.
- `Gravity` is the domain's own gravity (the button beside it copies the
  Scene's). It acts through buoyancy only: hot gas rises against it, and cold
  smoke ignores it unless Smoke Buoyancy is negative.
- `Wind` carries all air along at a steady speed, so plumes lean and trail
  while sources stay put. It costs nothing; use Wind effectors for gusts.
- `Move With Object` and `Carry Air`: see Fast Emitters, Vehicles And Move
  With Object.
- `Temperature Buoyancy` lifts hot gas. The checkbox beside it, `Physical
  Buoyancy`, replaces it with the gas-law lift for Flame Temperature (gas at
  2000 K is 6.8 times lighter than 293 K air: about 5.8 g).
- `Smoke Buoyancy` controls density-driven lift or sinking.
- `Vorticity` restores small rotational motion lost through advection.
  `Vorticity Mask` chooses where: `Fast Motion` (default), `Smoke` (in
  proportion to density, so thin edges stay calm), `Flames` (hot gas) or
  `Everywhere`.
- Artistic forces, per-cell rates and particles are `Modifiers` (their own
  folded card below Smoke Behavior, see Modifiers). A `Disturb` modifier
  pushes smoke and hot gas with small random swirls of its `Size` (scene
  units; at least 128 / Resolution), for breakup the base motion lacks: a
  Strength of about 50 is subtle and 300 strongly mixed on a fireball a few
  metres across. `Shred` stretches the motion across sharp temperature
  edges, tearing flames into tongues; values around 1–5 are typical.
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

### Modifiers

`Modifiers` (its own card below Smoke Behavior, folded; its title counts
them, "Modifiers (2)") is one stack of everything you add on top of the base
simulation: detail, per-cell rates and particles. A domain needs none; Quick
Setups fill it with what suits them (the Explosion: a fine Disturb and Grain).
Add with `+`, a menu grouped by purpose:

- Detail: `Disturb`, `Grain`, `Shred`.
- Rates: `Vorticity`, `Dissipation`, `Cooling`, `Soot Burn-off`.
- Particles: `Spawn Tracers`, `Spawn Embers` (a `Spawn Particles` modifier).

Reorder with the arrows (motion modifiers apply in that order); the checkbox
turns one off; `-` removes it. Below the list are the selected modifier's
settings and its masks. At most 8 motion modifiers (Disturb, Grain, Shred,
Vorticity), 4 smoke and heat ones (Dissipation, Cooling, Soot Burn-off) and
4 Spawn Particles.

#### Detail

- `Disturb`: swirling pushes of a set `Size` (scene units), with `Detail`
  adding smaller swirls, each half the size. Strength
  is an acceleration: about 50 is subtle, 300 strong on a fireball a few
  metres across. Stack a large and a fine Disturb for detail at two scales.
- `Grain`: small random kicks, one direction per block of `Size`, renewed
  every frame: the gritty breakup of fast fronts. `Layers` adds blocks three
  times larger (up to 3).
- `Shred`: stretches the flow across steep temperature changes, tearing flame
  edges into strands.

Disturb and Grain push the motion, which is simulated at half the Resolution
(unless `Full-Resolution Channels` is on): one motion cell is 64 / Resolution
scene units (0.125 at Resolution 512, 0.0625 at 1024), or 32 / Resolution
with Full-Resolution Channels. A `Size` under about two motion cells, 128 /
Resolution (0.25 at 512, 0.125 at 1024; 64 / Resolution with Full-Resolution
Channels), does little: the pushes are smaller than the motion grid can hold.
The same goes for a Disturb's `Detail` swirls, each half the size of the one
before: those below that size add no detail. To get finer detail,
raise Resolution rather than lowering Size.

#### Rates

These scale a rate per cell, by their masks:

- `Vorticity`, `Dissipation` and `Cooling` multiply the domain's Vorticity,
  Dissipation and cooling, for example stronger swirls in hot gas or cold
  smoke that fades faster.
- `Soot Burn-off` thins smoke at its rate per second where the masks allow
  (by default where gas is hot and air is left), as soot oxidises.

#### Masks

Masks decide where a modifier acts. Each modifier holds up to six; each maps
a field from `From Min`–`From Max` to `To Min`–`To Max` and combines with the
masks above it by Multiply, Minimum, Maximum, Add, Subtract or Replace (the
first starts from 1). Fields: Temperature (K), Speed (m/s), Smoke, Fuel,
Burn, Air Left and Noise. New Disturb and Grain modifiers start with masks
that keep them to smoke or heat (up to 2500 K), so clear air skips the work;
a modifier without masks acts everywhere (a rate modifier without masks
changes nothing). For a Spawn Particles modifier the masks are where it
spawns (see Particles).

#### Particles

A `Spawn Particles` modifier adds GPU particles that ride the smoke. Unlike
Sparks they are simulated with the smoke, in every simulation step and from
the live velocity: they show in the live preview, and a bake writes them
beside its VDBs (one point-cloud file per frame,
`particles/<prefix>particles_0001.ply`). After the bake each Spawn Particles
modifier gets an object, "<domain> Tracers" or "<domain> Embers", that shows
its particles of the frame on screen as a point cloud that renders in Cycles
and EEVEE. A domain can hold four, each its own kind of particle, for example
tracers in the cool smoke and embers from the fire.

- `Tracers` / `Embers` set good values for each look: Tracers are light,
  follow the smoke exactly and glitter white (an ink plume); Embers are heavy,
  thrown out of the fire by gravity and drag, cooling and glowing by
  temperature. Every value can be changed after.
- `Spawn From`: `Smoke` spawns `Rate` particles per simulated second in the
  smoke, spread by the modifier's masks (its `Spawn Masks`): where the masks
  give more, more particles start, and none where they give 0. A new Tracers
  modifier spawns in visible smoke (Smoke 0.05–0.1, fading in), a new Embers
  modifier in burning gas (0.6–0.7 of Flame Temperature); add a Temperature
  mask from 600 to 900 K with To 1 to 0 to keep tracers to the cool smoke, or
  a Noise mask for clumps. Without masks they spread evenly over all smoke.
  `Objects` spawns from a collection too: meshes from their `Surface` or
  `Volume` at `Rate` per second (with `Normal Speed` and a share of their own
  motion, `Inherit`), or `Points`: one particle per point of a point cloud or
  a Geometry Nodes output each frame (a point `velocity` attribute sets its
  start speed, `size` its size). Object particles ignore the masks unless
  `Mask Objects` (under More) is on: then each is kept with the masks' value
  where it starts (0 to 1).
- Motion: `Drag` is how strongly the gas carries them per second (0 flies
  free, 10000 follows the smoke exactly); `Gravity` is the share of the
  domain's gravity (negative: they rise); `Effectors` is how strongly the
  domain's effectors push the particles themselves, as they push the smoke
  (Tracers 0: they follow the smoke, which the effectors push; Embers 1: a
  wind blows them too); `Lifetime` in simulated seconds (0: forever) with
  `Random`; `Colliders`: Pass, Bounce (with `Bounce` and `Friction`), Stick
  or Kill on the domain's Fumaris colliders, tested every half velocity cell
  along a particle's step, so fast particles do not jump through walls.
- Look: `Size` (radius of the points) and `Random`; tracers' `Color` and
  `Brightness` (the strength of their glitter flashes), embers' `Brightness`
  (their glow); `Material` (under More) to use your own. `Show in Preview`
  draws them over the live preview.
- `Particle System` (under the stack, shared by every Spawn Particles
  modifier of the domain): `Max Particles`, how many live at most of all of
  them together; each takes 92 bytes of GPU memory, taken from the domain's
  simulation memory (1 million: 88 MB), and when it is full new particles are
  dropped until others die. `Seed` gives other random particles. `Inside
  Domain` removes those that leave a mesh domain's box. `Show in Preview` and
  `Amount`: the share of them the live preview draws (one pixel each).
- The point cloud carries `velocity` (Cycles motion blur), `age` (seconds),
  `temperature` (K), `smoke` (density at the particle), `size`, `random`
  (0..1, fixed per particle), `id`, `group` (the modifier's place among the
  bake's Spawn Particles, 0 the first), and `Col` with Colour Mixing. Its
  `Fumaris Particles` Geometry Nodes modifier reads the files and keeps its
  modifier's group; add your own nodes after it to instance objects on the
  points, make trails from `velocity`, or fade them by `age`.
- A bake is the same every time, and the same as the live preview of the same
  frames. Re-baking or choosing another version points the objects at that
  bake's particles.
- Files with Particles from an earlier 2.0 beta (their own panel section)
  open with them as a Spawn Particles modifier: the same particles.
- Not yet: particles that emit smoke, particles that push the smoke, particles
  without visible smoke (a velocity-only mode), and particles in a cached
  preview frame (they show the live simulation only). A collider thinner than
  a velocity cell (twice the voxel size) is not a wall for the smoke or the
  particles: both pass through it.

### Combustion

Every domain uses one physical combustion model (the Classic model was removed
in 2.0). Flame Temperature, Fuel Energy, Ignition, Burn and Cooling Time, Smoke
Amount and Gas Expansion are always shown; the rest folds under `More`, whose
title counts the settings changed from their defaults.

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
  burns in shells where fuel meets air. The default, 1, keeps a fireball's
  temperatures varied and lets it smoulder on in pockets instead of sitting at
  Flame Temperature and burning out at once. `Oxygen Field` tracks the air's oxygen
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

- `Smoke Detail` chooses how smoke and heat are carried between cells:
  Standard (linear) or Sharp (cubic: billows about twice as fine, at about
  1.25x the simulation time). `Edges` (Edge Sharpness) sets how strongly each
  step restores detail lost to interpolation; about 0.5 avoids stair-stepped
  edges in close-ups.
- `Projection Filter` (1 = Flow's) is how strongly the pressure solve smooths
  out cell-to-cell motion; lower lets fire flicker and puff (about 0.25) at
  the risk of grid-scale noise.
- `Preserve Turbulence` (see Smoke Behavior) and `Auto Cell Size` (see
  Resolution And Simulation Memory).
- `Full-Resolution Channels` simulates everything at the full Resolution:
  motion (velocity and pressure) and a fire's fuel and burn, like smoke and
  temperature. Rounder, finer billows and fewer grid-aligned artifacts from
  Vorticity at the same Resolution, but each block takes about 3-4 times the
  memory (so 3-4 times the VRAM for the same smoke; a set VRAM amount holds a
  third to a quarter of the blocks) and each frame 1.5-4 times as long; the
  live preview runs at about half the frame rate or less. At the same memory,
  half-resolution motion at about 1.5 times the Resolution looks as detailed,
  with sharper smoke. It also changes the timing: buoyant plumes rise up to
  about 20% faster and stay narrower (most at low Resolution), so a tuned
  scene may need less buoyancy. A fire also gets the heat of
  full-resolution fuel at low Resolution (explosions and the Oxygen options
  burn a little cooler without it). Only the fields the scene uses are
  stored either way: smoke alone stays smoke alone. A line under it says
  what runs at which resolution. After a preview or bake without it, turning
  it on also shows what that run's busiest frame would need with it ("Last
  run's peak at full res: ~2.4 of 9.0 GB VRAM"), in red when that is near or
  above `VRAM (GB)`: the smoke would be cut off. Auto VRAM does not grow for
  it (it takes what is free either way), so this line is the warning; the red
  line also shows under VRAM while Advanced is folded. Raise VRAM or lower
  Resolution until it fits. Changing it restarts the simulation. (It
  replaces Full-Resolution Velocity; scenes saved with that on open with
  this on.)
- `Open Collider Wall` is the thickness, in velocity cells, of the wall
  behind open collider meshes such as planes, so smoke cannot leak through
  them (0 = Collider Margin alone). `Collider Still Air` (called Closed
  Collider Margin before 2.0.0 beta 4, when it was 0 and applied to closed
  colliders only) holds the air at a collider's velocity only that many
  velocity cells out from the surface of a collider at rest, at most its
  Collider Margin; 0 = the full margin. Moving colliders always hold their
  margin. See Colliders.
- `Solver Internals` (folded; its title counts the settings changed from
  their defaults): `Trace Steps`, `Block Minimum Lifetime`, the two allocation
  thresholds and `Convex Collision`. The defaults suit almost every scene.

The smoke's total is kept through transport in every simulation: the solver's
advection gains a little smoke where gas expands and loses some where it
swirls (an explosion lost about 10% by its end); each step scales the moved
smoke back to the amount it started with, within 5%. Dissipation, Decay and
combustion still add and remove smoke as set. (Beta 1 had a `Conserve Smoke`
switch for this; scripts can still set `conserve_smoke`.)

### Per-Emitter Smoke Color

`Per-Emitter Smoke Color` (Look, under the smoke Color; called Per-Emitter
Color before 2.0 beta 3) lets each emitter's smoke carry its own `Smoke
Color`, set with the swatch beside Smoke on the emitter, under Emitted
Channels (times the domain's Color). It colors the smoke only, not the flame:
the flame's color is set under Flame in the Look (Ramp or Blackbody, Flame
Tint), for the whole domain. Where plumes meet, the colors mix by amount; smoke
that combustion forms takes the color of the emitted smoke around it. The live
preview tints the smoke, bakes gain a `color` grid, and the generated material
multiplies its Color by that grid (a volume without the grid is unchanged).

The color travels in a second smoke field: about 1.4x the simulation time and
a third fewer blocks for the same VRAM, so it is off by default. While it is
off, an emitter shows a color button instead of the swatch: it offers to turn
the option on for the emitter's domain or to show the domain's Color, which
colors all of its smoke at no cost. With the option on and every emitter the
same Smoke Color, the domain's Look says so: the option changes nothing until
the emitters have different colors. Mesh, box, sphere, Collection Points,
Collection Mesh, particle and OpenVDB sources all carry a color (one per
emitter, or per particle system).

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
`Emission Blending`. It decides how the emitter writes that channel:

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
slow or push the fluid. A file with a density grid emits only where that grid
has density (any amount, including the dense cores of other tools' caches,
which often exceed 1); a file without one emits throughout its bounding box.
Volume emitters start in `Blend`, which pulls smoke inside the emitting
region toward the file's density; choose `Maximum` or `Add` for Smoke when the
source should only contribute.

`Expansion` on a volume emitter is shaped by its density grid: each voxel
expands at density × Expansion per second, like a pressure (divergence) source
in other solvers, so a burst's dense core pushes hardest. Without a density
grid the whole region expands at Expansion. A burst file of other tools'
densities (peaks of 10 and more) needs far smaller values than a mesh: about
10–40 for a fireball's punch.

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
`Convex Collision` (Advanced > Solver Internals) treats a suitable closed convex mesh as a filled volume;
unsupported meshes fall back to the regular shell behavior.

Colliders are walls for the pressure solve: smoke cannot be pushed into
them, so it stops at, spreads along and wraps around their surfaces, and the
collider's own motion pushes the air. Fumaris also sets the collider's velocity
inside it and removes any smoke that still gets in. Geometry thinner than
about one velocity cell (two density cells) is too thin to form a wall and
only applies its velocity, so very fast smoke can still pass through thin
sheets; give them some thickness or raise Resolution. A velocity cell is
64 / Resolution scene units (0.25 m at 256, 0.125 m at 512).

After each step a collider also holds the air near its surface at its own
velocity. A collider at rest holds only a thin layer: `Collider Still Air`
(Advanced; 0.25 velocity cells by default), at most its `Collider Margin` (on
the collider, 0.3 m). So smoke rests on walls instead of a margin away from
them and flows through gaps and pipes. A moving (or deforming) collider holds
its whole Collider Margin, so it pushes the smoke ahead of it instead of
running over it; smoke within the margin rides along with it. With Collider
Still Air at 0 colliders at rest hold the whole margin too, as before 2.0.0
beta 4: smoke then stops that far from them and gaps narrower than twice the
margin are shut.

- **Closed meshes, boxes and spheres** are solids: smoke stays outside them.
- **Open meshes** (a plane, a sheet with holes) have no inside. They are a wall
  `Open Collider Wall` velocity cells thick (Advanced, default 1) behind their
  normal, so smoke that comes from behind a plane rests about that far from
  it (0.125 m at Resolution 512; with Collider Margin 0.3 m before 2.0.0 beta 4,
  0.31 m), and within a voxel of it in front (0.19 m before).
- **Gaps and pipes.** A gap between colliders (or a pipe's inside) must be
  about two velocity cells wide for smoke to flow through it. One cell or less
  is sealed: no smoke passes. Measured at Resolution 256 (0.25 m cells) under
  a rising plume: a 0.125 m or 0.25 m slot passed nothing, a 0.5 m slot let
  31% of the plume through, a 1 m slot 51%; 0.5 m wide round and square pipes
  2 m tall carried smoke out of their top, 0.25 m ones none. Nothing leaked
  through their walls.
- **Moving and rotating colliders** push the smoke. Smoke within Collider
  Margin of a moving collider rides along with it and its path through smoke
  stays clear for a while (a 0.8 m box crossing a cloud at 4 m/s carried 17%
  of it out at Resolution 256). A collider pushes with the air it holds out to
  its Collider Margin, so with Cells per Step above 0 (the default) a mesh or
  box collider whose surface would move farther than its margin (at least a
  velocity cell) in one step adds Sub-Steps for that frame, beyond Sub-Steps
  if needed, up to 8. Before 2.0.0 beta 4 such colliders jumped over the smoke
  they should push and lost or made smoke where they swept. Measured at
  Resolution 256 / 512: a 4 x 4 m plate 0.1 m thick at 8 m/s keeps all of a
  cloud (49% / 6% before), a 0.8 m box at 16 m/s 99% / 96% (63% / 44%), a
  plate at 24 m/s leaves nothing behind (43% passed through it at 512), a
  paddle spinning 3 times a second keeps 83% / 89% (it made 110% / 49% more).
  Frames in which a collider moves that fast take as many times the GPU time.
  With Cells per Step 0 Sub-Steps is used as set: raise it for fast
  colliders. With Move With Object colliders add no steps.
- **Deforming colliders** (shape keys, armatures, modifiers) push the smoke
  with their surface as it moves (`Velocity Influence`), and are not treated
  as convex solids while they deform. A sphere inflating into smoke kept 87% of
  it (55% before 2.0.0 beta 4, when it swallowed the smoke).

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
  Channels. VDB Precision Half (default) stores 16-bit values.
- `color` (with Per-Emitter Smoke Color): the smoke's color per voxel, as the emitters'
  Smoke Colors mixed by amount; white where no emitted smoke is. The
  generated material multiplies its Colour by it; in your own material, an
  Attribute node named `color` does the same.
- `burn`: raw timestep-dependent combustion activity for advanced control.
- `fuel`: remaining unburned fuel.
- `velocity`: world-space motion used for volume motion blur and compositing.

With fire, bakes also write the grids the generated material's fire and
Scatter Glow read, on one half-resolution lattice over the smoke: `pyro`, a
vector grid packing three values per voxel: x the blurred heat above ambient (Kelvin, the Pyro
look), y the fire light blurred into the smoke around it (Scatter Glow) and z
the least smoke between the voxel and the fire (Glow Shadowing); `glow_wide`
holds the same light spread four times farther (Glow Spread). In your own
material, an Attribute node named `pyro` with a Separate XYZ node gives the
three. The light is computed on a coarse lattice and sampled onto the smoke's
voxels, so renders do not march through empty space around the smoke. Bakes
before 2.0.0 beta 2 wrote `heat`, `glow`, `glow_wide`, `glow_depth` and
`density_ref` instead; the generated material still renders them. Bakes
up to 2.0.0 beta 3 also wrote `density_soft` (the smoke, slightly blurred),
which nothing reads. `Glow
Radius` (Output) sets how far the
light reaches; 0 follows the size of the fire. `Glow Source` sets what lights
it: `Fire Brightness` weights each voxel by its fire's brightness (Wien's law,
so the hottest core outshines everything), `Warm Gas` lights from all smoke
warmer than `Glow Heat From`, fully at `Glow Heat To` and above (Houdini
pyro's scatter source), so a cooling cap still glows from within. `Auto`
(default) uses Warm Gas for the Pyro look and Fire Brightness otherwise.
Changing it needs a re-bake; the live preview follows it straight away.

`Look` chooses the fire shading, and each look keeps its own flame controls
(Brightness, Full Glow, Contrast, Blackbody Scale, Blue Core, Flame Tint and
the Scatter Glow settings), so switching looks does not change the other's
values. `Temperature` glows by each voxel's temperature and density.
`Pyro` follows Houdini's Pyro shading with the same brightness law, so equal
values give equal exposure; Blackbody Scale shifts its colour range (above 1
toward the ramp's hot end) and Blue Core works as in Temperature. `Color` picks
**Ramp** or real **Blackbody** colour (as Temperature, with Pyro's soft glow,
Thin Gas and dark soot). Ramp is a colour ramp you edit in the panel (as
Houdini's Pyro fire colour ramp; it starts deep red to pale gold): the gas
temperature picks its colour from left (0.45 x Full Glow) to right (Full Glow),
for the fire and the light it casts on the smoke (Scatter Glow); brightness
still follows the temperature. The ramp belongs to the domain's Fumaris material
(**Edit Ramp** creates it before a bake) and the live preview follows it. Both
looks offer Ramp. **Thin Gas** (Pyro) sets which smoke counts as thin
enough to glow; with **Auto** (default) it is Thin Gas Ratio (0.12) x each
frame's typical smoke density (its density-weighted mean, baked per frame as
the `pyro` grid's `fumaris_density_reference` metadata, keyed on the volume
object at import as its `fumaris_density_reference` property, and measured
live in the preview), so the look holds at any
Smoke Amount, scale or time; lower the ratio for darker, more cellular smoke.
Off, Thin Gas is a fixed density. Fire glows from the hotter of the gas and its
blurred surroundings (the baked heat, `pyro`'s x), so cells glow softly and nearby
smoke picks up their light, and hot soot turns dark. Pyro glows by the fire's
light: the baked, blurred heat (and part of each voxel's own heat), so gas
near fire glows too and billows read round and soft. Gas glows by volume, so a
billow is brightest through its middle, and smoke denser than `Thin Gas` fades
to dark soot (none of it glows above 1.5 x Thin Gas): the dense tops of billows
read as soot and the fire shows in the thinner gas between them. This is the
cauliflower look of Houdini and Axiom explosions; it needs enough Resolution
(cells about ten voxels across or more) and a Grain layer to form the cells.
Raise Thin Gas for dense, sooty sims (a smoke median of about 5 wants about
12), lower it for more soot. `Hot Soot` sets how dark that hot soot turns (0
keeps hot smoke the Smoke Color, 1, the default, is full dark soot). For a
fireball close to Houdini's pyro shading:
Color Blackbody with Blackbody Scale about 1.3, Full Glow about 1100 K,
Contrast about 0.3, Scatter Glow 0 (it lights thin gas around the cloud, which
softens the edges). It holds at any scene size, and Full Glow changes need no
re-bake. Pyro needs a bake from
Fumaris 2.0 or later (the heat and soft density grids, written with the glow
grids).

When `Volume Material` is unset, Fumaris creates a material driven by the
domain's Look settings; changing them does not require a rebake. Every value
it uses is an input of one `Fumaris Look Controls` node at the left of the
node graph, so a copy of the material detached from Fumaris can be tuned
there by hand; the graph is laid out in three rows (Smoke, Fire, Scatter
Glow).

- Smoke: `Density`, `Color` and `Anisotropy` (forward scattering, 0.3 by
  default, like real smoke). `Hot Smoke Density` makes hot gas this many times
  denser than cool smoke, rising from `Hot Smoke From` to `Hot Smoke To`
  (1, the default, leaves it unchanged).
- Fire glows by its temperature like soot on a camera exposed for the hottest
  gas: gas at `Full Glow` glows at full `Brightness`, hotter gas brighter,
  cooler gas falls off by `Contrast` (lower = wider, fuller flames). `Color
  Spread` below 1 compresses the colour range (an orange body without
  white-hot or deep-red extremes); `Blackbody Scale` multiplies the temperature
  before the Blackbody colour; `Flame Tint` multiplies the flame colour.
- `Scatter Glow` adds the baked fire light in the surrounding smoke, so dark
  billows glow from inside near the flames. `Glow Spread` blends in the
  wider glow for a long, soft falloff; `Glow Shadowing` lets smoke between a
  spot and the fire block it, so dense outer billows stay dark and fire light
  shows in the crevices next to the flames. If Cycles' unbiased volumes
  render the glow in square patches, try Render Properties > Light Paths >
  Volumes > Biased (often, not always, faster too).
- `Smooth Voxels` (Cycles; on by default) reads the volume with cubic instead
  of linear interpolation. With linear, dense smoke and fireballs show fine
  concentric rings, one per voxel layer, where their surface crosses the
  voxel grid. It costs about 1.5 times the render time: turn it off for quick
  test renders.

The live preview uses the same settings. The material reads only the density,
temperature and glow grids (and fuel when Blue Core is on); `burn` stays
available for custom materials and compositing. Density is always written for
it; Temperature only when ticked (on by default): without it the material
shows smoke only, which suits smoke that carries heat just to rise.

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
6. Keep caches on a fast local drive. A large bake writes about 1 GB a
   second; on a filesystem that compresses what it stores (Btrfs with
   `compress=`, the default on some Linux distributions) the kernel's
   compression takes a share of that time for files about a quarter smaller.
   `chattr +m` on the cache folder turns it off for new files there.
7. Split point clouds with widely different radii and reduce excessive point
   counts when point emission dominates a frame.

## Known Limitations

- Hardware coverage is limited and AMD GPUs are currently unqualified.
- Geometry Nodes Volume emission is experimental.
- The preview cache lasts until Play starts the preview again, Release Cache,
  a settings change or another job; Save Sequence keeps it on disk. Cached
  frames are for looking back, not checkpoints: Resume always continues from
  the live frame, and Play after Stop starts from the first frame.
- Volume preview shading is intentionally simpler than a final Blender volume
  material. Scene Occlusion uses viewport depth, so transparent objects occlude
  as if solid.
- Stopped bakes cannot resume solver state.
- Blender keeps every shown frame of an imported volume in RAM, up to
  Preferences > System > Memory Cache Limit (4 GB by default), even after the
  volume is deleted. Lower that limit if RAM is short; it is Blender's cache,
  not Fumaris's.
- One live preview or `Bake` runs at a time. `Bake in Background` bakes more
  domains at the same time.
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
Shift–Alt–X stops it, and Shift–Alt–R starts the preview again from its first
frame (or starts it, when it is not running). The shortcuts can be used with one hand. The current
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
**New Source** to create an icosphere at the 3D cursor (for Steady Fire, a fuel bed
standing on it). Existing participants are
left intact; the new setup has separate emitter, collider, effector and outflow
collections. The new simulation object is an axes empty, with no physical domain bounds.

Presets start from the defaults of a new domain and change only what makes the
effect. Rising Smoke emits continuously without fuel. Steady Fire is a fuel fire:
its New Source is a fuel bed (a cylinder 2 m across and 0.25 m high, its base on
the 3D cursor, filled with fuel by Volume emission) on a ground plane. The
ground's collider (hidden in renders) sits a bed height lower: right under
the bed it starved the fire at low Resolution (a velocity cell of gas next to a
wall cannot move toward it, and at Resolution 192 that cell is taller than the
bed). Air comes in mostly from the sides and the flame base wrinkles
into tongues. Its fuel
burns out within half a second and burnt gas stops glowing within 0.6 s
(Cooling Time), so the flames end in tips that turn into dark smoke a few
source widths up; a Shred layer and a Vorticity layer (up to 5x in flames)
break the puffs into licking tongues. Its Pyro look has Brightness 0.08 and
Contrast 1 for a yellow base and orange tongues in daylight. Steady Fire
setups made before 2.0.0 beta 4 used a 2 m icosphere centred on the cursor.
Steady Fire and Explosion use the Pyro look with dark fuel soot (Smoke Color
0.28, Hot Soot 0.25) and fire in glowing seams between the cells (Thin Gas 0.08).
Explosion is a ground explosion with a 120-frame range: a 14-frame blast of fuel
and Expansion over a ground collider sets its width at once as a low lens, which
then rises slowly into a row of cauliflower lobes rather than a mushroom on a
stem. Most of its fuel comes from **Clumps**, a hidden Geometry Nodes point
source beside the source: four dozen balls of fuel fly out at random speeds,
mostly sideways, and burn for random times, so the fireball grows uneven lobes
instead of one smooth ball. Their fuel varies from clump to clump (Fuel Low and
Fuel High, 0.15-4x Fuel) and a share of them keeps feeding fuel after the blast
(Long Burn, Long Burn Frames), so lean regions turn to soot first while fire
survives in pockets and at the base, as in footage of real fuel explosions (its
inputs, such as Clumps, Clump Size, Speed, Upward Spread and Seed, are on its
Geometry Nodes modifier). The clumps write smoke and fuel as Maximum, so
overlapping clumps do not stack into stepped layers. Hot gas renders up to 10
times denser (Hot Smoke Density), for crisp dark lobes with glowing seams. It
has no large Disturb, so the clumps' lobes stay a few big, soft billows rather
than many small soot balls; a fine Disturb and Grain work only where the gas
moves at about blast speed, crisping the young fireball with a crust of cells
while the late smoke stays calm. Disturb sizes, the Speed masks, Air Margin and Cooling Time follow the size of the
sources, so a larger explosion keeps the same look at the same number of
voxels across it.
Ground Fog emits cold smoke that is heavier than air, and adds a collider floor
just under the source for it to spread over. Chimney Smoke blows warm smoke
straight up at 4 m/s.
Ranges start at the scene's Start Frame. These are editable starting points.
The simulation empty stores settings; smoke can extend freely around it.

### Preview Cache

With **Cache Preview** on (under Play; off by default, applies from the next
Play), the live preview keeps the frames it simulates, compressed in system
memory, at full detail (exactly what the preview drew). Without it the preview
keeps nothing. **Cache Every** beside it chooses how often a frame is kept:
Every Frame (the default), every 2nd, 3rd or 4th, or Auto. Fewer frames use
less RAM and preview time; scrubbing and replay hold each kept frame until the
next one, so they look choppier. Auto keeps as many frames as copying them from
the GPU allows in about a fifth of the frame time (the copy takes about 40 ms
per GB on a PCIe 4.0 x16 link, several times that on a narrower one).

**Looping.** With **Loop** on (the loop button beside the frame range, on by
default) the preview starts over at the end of its range:

- When the cache holds the whole range and nothing the simulation uses has
  changed, the preview **replays the cache** instead of simulating: smooth
  playback, no GPU simulation. The status line says Replaying the cache.
- If you change a setting, an emitter, collider or effector, or their
  animation, the run is marked as mixing settings (its frames turn orange on
  the Timeline). A simulated pass keeps caching, so a paused preview still shows
  the mix, and at the next loop the range simulates again with the current
  settings. A change during a replay simulates again from the first frame at
  once, so it shows straight away. The cache is never cleared for this: the new
  pass replaces the stale frames as it reaches them, so you can still scrub
  them until then.
- When the oldest frames have rolled out (the RAM limit below), the range
  simulates again and keeps rolling: each new frame replaces the oldest one,
  so the last frames you watched stay until newer ones push them out.

Look and viewport changes (lighting, colour, camera) never count: cached frames
are always drawn with the live look. With Loop off the preview pauses on the
last frame, and Resume continues the same way.

**On the Timeline**, a thin strip along the bottom shows which frames are
cached while the preview runs: blue for cached frames, orange for cached frames
from earlier settings that the next pass replaces. Only the RAM limit (below)
ever removes cached frames.

- While the preview is paused, scrub or play the timeline (Blender's own
  playback) to see the cached frames with the live camera, lighting and look.
  The status line shows the cached range and the frame shown.
- **Resume** returns to the live frame and continues (a replay continues from
  where it was).
- With Cache Preview on, three buttons sit below the cache options (Save
  Sequence and Save Frame need a cached preview running; with Cache Preview
  off only Save Snapshot is shown):
  **Save Sequence** writes the cached frames as VDB files into a new version
  (see Versions), with the settings that made them and the Output
  settings (grids, resolutions, Glow) at the preview's resolution; it imports,
  renders and compares like a bake. **Save Frame** saves only the frame the
  preview shows (the live one, or the cached frame at or before it). **Save
  Snapshot** saves what the 3D Viewport shows, live preview included, as an
  image in Versions, with the current settings. Save Sequence and Save Frame
  work while paused (greyed while playing); Resume waits until the files are
  written, and the cursor counts the frames as they are saved. The buttons
  show only while this domain previews.
- **Escape** stops a bake (keeping the frames written, like Stop), a playing
  live preview (like Stop) and a save in progress (its files are removed). A
  paused preview leaves Escape to Blender (it stops timeline playback).
- **Stop** releases the simulation's GPU memory but keeps the cached frames:
  the panel says "Stopped: cached frames kept" with the RAM they use, and
  scrubbing, timeline playback and Save Sequence work as while paused (frames
  outside the cache show nothing). **Play** then starts the preview again from
  the first frame, and **Release Cache** (the Stop button now) frees the
  frames. A change to the domain's or its sources' simulation settings,
  starting a bake or another preview, and loading a file release them too.

Preferences > Preview Cache RAM sets the most memory the cache may use (0: a
quarter of the RAM, from 1 to 16 GB; set more to keep more frames, for example
20 on a 32 GB machine). When it is full the oldest frames are dropped, so the
cache holds the most recent part of the preview; the status line shows the
range it still holds and its size against the limit, for example `Preview
cache: frames 84–109 · 8.4 of 8.6 GB RAM`. A full cache is not an error: the
simulation keeps running, and the panel says `Preview cache RAM is full`, which
frames were dropped and what Save Sequence still keeps. To keep more frames,
raise Preview Cache RAM, set Cache Every to 2 or more, lower Resolution, or
Bake (to disk, any length). Whatever RAM is set to,
the cache also drops its oldest frames when the system runs short of free
memory, leaving 2 GB or an eighth of the RAM (whichever is more) for Blender and
other programs; the status line then says System RAM is low.

**Save Playblast** (at the top of Versions, and in the menu beside Bake) asks
for a name, then simulates the whole range as fast as the GPU allows (not in real
time) and saves what the 3D viewport shows, with the smoke and without
overlays, as a movie and its PNG frames in Versions, beside the snapshots, with
the settings it was made with. It records the viewport it is started from (from
another editor, the largest one), framed as you see it: a perspective or
orthographic view at the viewport's own size and shape; looking through a
camera, the camera's frame (what is inside the passepartout, as a render shows
it) at the render resolution and its percentage, up to 2048 pixels on the
longer side. The new playblast is selected while it records.
**Play** (the triangle on its row) opens the movie in your video player,
independent of Blender's timeline (full screen, any monitor); **Image Editor**
shows it in an Image Editor along the timeline, and an Image Editor showing a
playblast switches to the one you select or have just recorded. View Changes
compares its settings with the current ones; the More menu renames, saves a
copy (**Export MP4…**) and opens its folder; it can be pinned or deleted like a
snapshot. Playblasts are files on disk in the domain's Versions folder, never
inside the .blend. A stopped playblast keeps the frames made so far (without a
movie). Objects hide the smoke behind them when Scene Occlusion is on. It keeps
no preview cache, so it suits shots longer than the cache's RAM; Esc stops it.

Each domain in the sidebar's Domains list has an eye: off hides the domain, its
live preview and the volumes imported from its bakes in the viewport (renders are
unchanged), so several effects can be set up one after another and switched
between; on shows them again.

Preferences > Hide Volumes While Playing (on by default) hides imported Fumaris
volumes in the viewport while the timeline plays, since Blender loads every VDB
frame as it plays, and shows them again when playback stops. While they are
hidden, the 3D Viewport and Timeline headers say `VDBs hidden while playing`:
click it to play through the VDB sequence (it turns the preference off; turn it
back on in the Fumaris preferences). Volumes you hid yourself stay hidden;
renders are not affected.

### Capacity warning and defaults

A capacity warning appears beside the simulation controls even when Diagnostics
is collapsed. At full capacity smoke may be cut off: increase **VRAM**
(Simulation Memory) or lower **Resolution**, then restart. The warning names the
frame the capacity ran out at, a bake's report in the status bar says so too
(the frames are written, but the smoke is cut off from that frame on), and the
system console logs it once per run. A full-capacity warning remains
for that run because freeing blocks later does not recover missing smoke. A new
run or Delete clears it.

Resolution has no upper limit (physical-scale scenes use very large values), but a
source can be too large for it: its surface alone may need more sparse blocks than
VRAM holds. The smoke is then cut off at the first frame, and with a source
thousands of times too large (the default 2 m source at Resolution 16.7 million
and above) nothing is emitted at all. The panel then says "A source is too large
for this Resolution": lower Resolution, since more VRAM cannot hold it. Diagnostics sits below Advanced on the panel's darker background;
its exact appearance follows your Blender theme.

Current defaults are Dissipation **0.2**, Smoke Decay **0.1**, emitter Fuel
**1** and preview Step Size **1** cell. Explicitly stored settings retain their
values; older unset properties inherit the new defaults. Quick Setup keeps the
domain's fade defaults and sets Resolution 512 and 2 GB; the Rising Smoke
setup uses zero fuel. New emitters start at Expansion 0.2; Rising Smoke and
Steady Fire use Normal Velocity 0.25, Explosion 1.


### Choosing the simulation GPU

Use **Simulation GPU** in the Fumaris preferences (Edit > Preferences >
Add-ons > Fumaris). The Vulkan GPUs are listed when the preferences are first
shown (the refresh icon lists them again), then choose a card. The choice applies to new Preview and Bake jobs, is shared across scenes
and is kept with Blender's preferences.

**Automatic** prefers a discrete GPU and preserves launch environment overrides;
the list names the GPU it resolves to, for example `Automatic (NVIDIA GeForce RTX 3090 Ti)`.
An integrated GPU beside a discrete one is marked `(integrated, not recommended)`
and is not offered under On GPUs for background bakes (a GPU already ticked stays).
An explicit choice uses the card's UUID, so a driver changing device order does
not redirect the simulation. An unavailable saved GPU stops the job with an
error; refresh and choose another card or Automatic. Changing the choice does
not move an already running or paused preview to another GPU; stop it first.
Diagnostics reports the GPU actually used by the worker.

Background bakes have their own choice: **On GPUs** under Baking in the same
preferences (see Bake in Background). A ticked GPU that is no longer available
stops the bakes given to it with the same error.

Discovery lists identifiable Vulkan GPUs, excluding software CPU devices. It
does not qualify driver compatibility; an unsupported device or driver reports
its startup error when the simulation starts. A custom bridge must support
`--list-devices` and UUID selection. This feature does not expand the officially
tested hardware list.
