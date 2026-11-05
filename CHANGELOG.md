# Changelog

All notable customer-facing changes are recorded here.

## 1.4.0 — Unreleased

- **Emitters push smoke physically.** Expansion (formerly Divergence) is now a
  rate per second that gives the same result at any Resolution and Sub-Steps.
  Normal Velocity on a closed mesh now really blows the air out (before, the
  pressure solve cancelled most of it), and closed meshes that grow or shrink
  push or pull the smoke around them. Quick Setups use smaller values to
  match. Emitters in saved scenes keep the old behaviour.
- **Fewer grid-shaped rectangles in fire at high Vorticity.** Vorticity now
  measures swirl over a wider stencil.
- **Small Sparse Blocks removed (Advanced).** Measured on four scenes at the
  same Simulation Memory, it was 20–30% slower for at most ~10% more room, and
  on explosions both slower (2.5x) and fuller. Scenes that used it now
  simulate with standard blocks.
- **Scatter Glow (Appearance).** Fire light now spreads into the smoke around
  it, so dark billows glow from inside near the flames, which Cycles does not
  do on its own. Bakes write a small blurred `glow` grid with the temperature;
  Glow Radius (Output) sets how far the light reaches (0 follows the size of
  the fire). Re-bake to use it in existing caches.
- **Live preview closer to the render.** Dense smoke is as opaque as in Cycles,
  flame colours match the material, Tone Mapping follows the scene's View
  Transform by default, and AgX now matches Blender's AgX (fire was about 2.8
  stops darker and redder in the preview than in the render).
- **Smoke, fire and heat no longer fade or stall at high Sub-Steps.** Small
  per-step changes were lost to half-precision rounding, so emitters stopped
  filling, decay was misapplied and sources ran cooler as Sub-Steps rose.
  Scenes now keep more smoke and heat, especially at 6+ Sub-Steps, and look
  much the same at any step count. Saved scenes will look denser and last
  longer than before.
- **Mesh emitters with high Normal Velocity emit from their real surface.** With
  several Sub-Steps, emission was swept back along the emitted velocity, so a
  fast Normal Velocity (for example the Explosion Quick Setup) emitted from
  shrunken copies inside the mesh, and its first frames changed with
  Sub-Steps. Such emitters now emit more smoke, the same at any Sub-Steps;
  saved scenes that use them will look denser.
- **Gas Expansion (Combustion).** Burning gas now pushes the smoke around it
  outward where it actually burns, with the same strength at any resolution,
  frame rate or Sub-Steps. Use it to drive fireballs and mushroom clouds from
  the fire itself. It replaces Expansion in the Realistic model (Expansion
  remains for the Classic model). The Explosion Quick Setup uses 2.
- **Temperature (Physical) flame look (Appearance).** Fire glows by its
  temperature like real soot: gas at Flame Temperature blazes yellow-orange,
  cooler gas falls off to deep red and dark crevices as on a camera exposed
  for the hottest gas, and dense hot gas reads as a glowing surface. Contrast
  sets the fall-off. New domains use it; saved scenes keep the Burn look.
- **Soot Radiation (Realistic combustion).** Sooty gas radiates its heat
  away, so dense pockets cool and darken while thin hot gas stays bright,
  instead of the whole fireball sitting at Flame Temperature (very dense
  soot radiates only from its surface). Off by default. Gas that is still
  burning stays at Flame Temperature.
- **Fire always glows by temperature.** The Burn (Classic) flame look is
  removed: the generated material and the live preview both use the
  temperature controls (Brightness, Full Glow, Contrast, Blackbody Scale), so
  the viewport preview now matches the render's fire.
- **Simpler domain panel.** Resolution and Memory share a row, Max Sub-Steps
  and Cells per Step share a row (Cells per Step 0 turns Adaptive off; the
  toggle is gone and saved scenes keep their setting), Vorticity Mask sits
  beside Vorticity, VDB precision beside compression, the cache slot is at the
  top of Output, Preview Settings are grouped (Quality, Exposure, Lighting,
  Light Direction), and spacing is consistent. Preserve Turbulence sits under the
  Sub-Steps row and is on for new domains (Sub-Steps are rounded up to an even
  count while it is on, as it works in pairs); Physical Heat moved to Advanced
  and is on for new domains (Realistic only);
  Vorticity sits just above Turbulence Detail.
- **Physical Heat (Realistic combustion).** Burning releases heat for the fuel
  it uses and needs air, so fire forms hot shells where fuel meets fresh air
  and cools as it mixes, instead of all burning gas sitting at Flame
  Temperature (the cause of flat, single-colour fire). New domains use it;
  saved scenes keep the previous heat. Use a short Burn Time and emitters
  below Flame Temperature (about 0.35, Maximum) so burning makes the heat.
- **Full Glow** sets the temperature where the Temperature look reaches full
  Brightness (default 1600 K); Contrast now defaults to 0.5.
- **Oxygen Limit (Realistic combustion).** Burning needs fresh air, so fire
  concentrates in shells where fuel mixes with air and temperatures vary,
  instead of the whole fireball sitting at Flame Temperature. Fireballs that
  burned everywhere at once become smaller and leaner. Off by default.
- **Explosion Quick Setup reworked.** Fuel is released in one short burst
  and ignited instead of pushed up as a jet (which shed stacked smoke rings);
  burning gas expands with Physical Heat, Sharp Advection is on, and it
  uses the Temperature flame look. The boiling cells need about 250 voxels
  across the fireball (Vorticity raises it; an experimental Vorticity
  Overshoot was dropped as noisy and energy-draining).
- **Linux: a failed bridge start now says what to fix:** a missing system
  library, a glibc that is too old, or no Vulkan driver.
- **Faster emitters that do not move:** a static mesh emitter with Normal or
  Initial Velocity (or a velocity attribute) was stamped up to 8 times per
  step as if it moved; now once (12 Sub-Steps test: emitter GPU time 1.8 s →
  0.2 s, bake 26% less GPU time).
- **Full-Resolution Velocity (Advanced).** Simulates motion at the full
  Resolution instead of half: rounder, finer billows and far fewer
  grid-aligned artifacts from Vorticity, at about twice the memory per block.
  Off by default; changing it restarts the simulation.
- **Appearance always drives the generated material,** also when that
  material is picked as the domain's Volume Material; new **Anisotropy**
  (default 0.3, forward scattering like real smoke).
- **New-domain defaults:** Sharp Advection on, Burn Time 0.5 s, Cooling Time
  5 s, Gas Expansion 0.5, Simulation Memory 4 GB, up to 4 Sub-Steps with
  Cells per Step 4 (Preserve Turbulence and Physical Heat on, as before).
  Saved scenes keep their settings. The Steady Fire Quick Setup ignites its
  fuel (Temperature 0.35, Maximum) so burning makes the heat, and an emitter
  that injects fuel at Flame Temperature under Physical Heat shows a hint.
- **Fixed:** a VDB bake could stop with "No density readback" when the last
  smoke vanished while warm air was still moving; those frames are now saved
  as empty.
- **Sharp Advection (Turbulence).** Carries motion with a stronger error
  correction that falls back to the plain step wherever it would overshoot,
  for crisper swirls and billow edges at the same cost per voxel. Stable at
  any Sub-Steps. Off by default.
- **Upres removed.** The experimental Upres option and its settings are gone; scenes saved with Upres enabled now simulate at their base resolution.

- **Velocity attribute for mesh emitters.** A mesh or Geometry Nodes mesh
  emitter can take its velocity from a vector attribute (Emitter > Velocity),
  such as one stored with Store Named Attribute. It replaces the velocity
  measured from the mesh's deformation, so emitters whose vertices change every
  frame can still carry motion.

- **Simulation Memory replaces Sparse Block Capacity.** Set the GPU memory
  for the simulation in GB; Fumaris turns it into space for smoke for the
  current settings. The full amount is allocated at the start, so GPU memory
  stays flat instead of spiking by 30–60% each time the smoke outgrew its
  space, and heavy bakes can use more of the GPU safely. Scenes saved with a
  Sparse Block Capacity keep it until Simulation Memory is changed.
- **GPU memory full warning.** When a simulation no longer fits in GPU memory
  and the driver moves it to system memory, the panel now says so instead of
  the simulation silently slowing to a crawl.
- **Faster, smaller VDB bakes.** Density channels are packed on the GPU and
  only cells holding smoke are copied back, so a VDB bake no longer reserves
  GPU memory for exported density channels and spends far less time copying:
  the same memory now holds up to a third more smoke. VDB files are written
  by more threads, and a VDB bake uses less RAM: a large bake that took
  about 52 GB now takes about 34 GB.
- **Faster bakes and more room.** Mesh emitters and colliders, vorticity and
  bake frame handling were streamlined: bakes of the test scenes at 12 GB
  run about 30% faster than 1.3.1 while holding 36–59% more smoke in the same
  Simulation Memory (13–17% more without VDB export). Apple silicon bakes
  run up to 80% faster.
- **VDB Precision** (export panel). Half (default) stores 16-bit values,
  halving file size and write time; Full keeps 32-bit values.
- **Colliders are walls.** Smoke is no longer pushed into colliders by the
  pressure solve, so it spreads along and wraps around them instead of
  thinning out against them.
- **More lively default motion.** Flow removed a constant 1 m/s of velocity
  per second, which stalled large plumes early. Motion now only loses a small
  fraction per second, set by the new **Velocity Damping**; plumes rise
  higher and keep their turbulence longer. Scenes tuned for the old look can
  raise Velocity Damping.
- **Steady flame brightness with Adaptive Sub-Steps.** Frames that ran fewer
  steps no longer flash brighter in the preview and in baked VDBs.
- **Adaptive Sub-Steps.** A toggle beside Sub-Steps uses only as many steps
  as the motion needs each frame, up to Sub-Steps; `Cells per Step` sets how
  far the fastest flow may move per step.
- **Vorticity Mask.** Choose whether Vorticity swirls fast motion (as before),
  dense smoke, flames or everything.
- **Realistic combustion** (new domains; earlier scenes keep Classic). Fuel
  burns away gradually at a set **Flame Temperature**, so fires glow at real
  flame colours and fade into embers and smoke instead of turning white-hot
  and stopping at once. The Combustion panel uses Flame Temperature, Burn
  Time, Cooling Time, Smoke Amount, Expansion and Ignition Temperature in
  place of Flow's eight per-burn coefficients.
- **Sharper fire.** Appearance gains `Sharpness` (default 2: crisp flame
  tongues instead of an all-over glow, also in the live preview; set 1 for the
  previous look) and `Blue Core (Render Only)` (blue where fuel is just
  igniting).
- **Preserve Turbulence** (Behavior > Turbulence Detail). Advection–reflection
  keeps much more of the flow's swirling energy for livelier, longer-lasting
  turbulence, at 6–26% more time.
- **Disturb and Shred** (Behavior > Turbulence Detail). Disturb adds small
  random swirls inside smoke and hot gas; Shred tears flames into tongues.
- **Consistent expansion across Sub-Steps.** Emitter Divergence no longer
  weakens as Sub-Steps rise, so raising Sub-Steps keeps the plume's push and
  amount of smoke closer to the default.
- **Steady VRAM when adjusting a paused preview.** Changing an Appearance
  setting while paused could briefly allocate up to several GB more GPU memory.
- **More accurate pressure solve.** A sign error in the solver's multigrid
  step (inherited from NVIDIA Flow) is fixed, removing slightly more of the
  compression the solver is meant to remove.
- **Pre-1.0 beta data is no longer recognised.** Caches, preview objects and
  imported volumes made by the beta under its old name are left alone instead
  of being managed by Fumaris. Delete them by hand if a scene still has any.
- **Auto Simulation Memory.** The Auto button next to Memory reserves 80% of
  the GPU memory free when the simulation starts (at least 0.5 GB); the panel
  shows the amount it used. Off by default; saved scenes keep their GB.
- **See what Simulation Memory buys.** Below Resolution and Memory the panel
  shows the voxel size and about how much space the smoke can fill.
- **Live preview counted in Simulation Memory.** The self-shadow and flame
  light volumes and preview images are now part of the budget, so a preview
  no longer uses more GPU memory than set.

## 1.3.1 — 2026-10-26

- **Simulation GPU selector.** Choose a supported GPU beside the simulation
  controls or in Fumaris preferences. Refresh the list after changing hardware,
  and save Blender preferences to retain the choice. Selection uses the device
  UUID, so a changed device order does not silently select a different card.
  A missing saved GPU reports an error instead of falling back to another GPU.
  Stop the current job before changing its GPU; Automatic retains the existing
  device-selection behavior.

- **Use Timeline.** A clock button beside Start and End makes the simulation
  follow the scene's frame range, with a `Warmup` count of frames simulated
  before it starts, so the range no longer has to be matched to the scene by
  hand. New domains and Quick Setup start with it on (Quick Setup effects with
  a fixed length, such as Explosion, keep their own range); domains in scenes
  saved earlier keep their Start and End. Turning it off restores them.
- **Bakes are harder to lose.** Delete now asks before removing a bake, and
  Bake asks before replacing one that is already in the cache slot.
- **New Slot** (the + beside Cache Slot) switches to the next unused version
  of the slot name, `main` to `main_001` to `main_002`, so the next bake is
  kept alongside the earlier ones, like Save Incremental.
- **Protect** (the lock beside Cache Slot) locks a slot: Bake and Delete are
  refused until it is unlocked. The lock is stored in the slot's folder.
- Switching Cache Slot now shows that slot's bake and hides the other slots'
  volumes instead of leaving them all visible on top of each other, and the
  status line follows the slot.
- Preview **Flame Light.** Flames now light the smoke around them in the live
  preview: a warm glow that is strongest beside the fire, fades with distance
  and is dimmed by smoke in the way. On by default at 1; 0 turns it off. It
  added about half a millisecond or less per frame on an RTX 4060 Ti in
  testing. Preview-only.
- Preview **Light Scattering** gently fills shadowed billows using the existing
  light field. Default 0.5; set it to zero for direct lighting only. Preview-only.
- Smoke opacity and fire brightness vary less with Ray Steps. Smoke-only previews
  skip flame shading, and opaque smoke skips hidden samples to limit GPU cost.

## 1.3.0 — 2026-10-11

New controls, and fewer switches. The solver now always takes its best path
instead of offering it as an option, so **scenes from earlier versions
simulate differently**: edges are cleaner, large plumes roll up, and more
sparse blocks are in use. Check the capacity readout after updating.

- **Air Margin.** Simulates a set distance of clear air on every side of the
  smoke and reaches further in the direction the smoke is moving. It removes
  the horizontal steps that fast smoke could leave at the edge of the
  simulated region and gives large plumes the surrounding air they need to
  roll up. 0.5 m by default; it sits below Sparse Block Capacity.
- **Large plumes roll up.** The pressure solve now reaches across large
  plumes, so mushroom caps and big rolling shapes form at high resolution and
  low Sub-Steps.
- **Move With Object.** A domain can now simulate in its own object's space:
  parent the domain to a vehicle, or animate it, and the simulation travels
  and turns with it. Emitters and colliders that ride along stay put in the
  simulation grid, which keeps fast vehicles smooth with far fewer Sub-Steps.
  The air stays still in the scene, so smoke trails behind as it would
  outdoors; `Carry Air` carries it along instead, as inside a closed cabin.
  Bakes are written in scene space as before. Not available with Upres.
- **Fast emitters and colliders stay stable.** Very fast emitters and
  colliders with few Sub-Steps no longer make the motion explode: vorticity
  can still sharpen swirls but cannot feed on itself. Fast emitters leave a
  continuous trail instead of separate stamps, and colliders at rest (a long
  road, a wall) no longer reserve simulation space along their whole length.
- **Removed settings.** Adaptive Boundary Padding is gone; Air Margin replaces
  it. Allocate Neighbor Blocks, Boundary-Safe Advection and Speed Minimum
  Smoke are gone too: the solver always behaves as it did with them at their
  recommended values. A scene that had changed one of them now uses the
  recommended behavior.
- **Small Sparse Blocks is off by default.** Standard blocks measured about
  20% faster for the same GPU memory. The setting stays under Advanced; a
  domain that never changed it now uses standard blocks. Sparse Block Capacity
  means the same amount of space either way.
- **A shorter Advanced section.** It now holds Simulation Speed, Small Sparse
  Blocks, Auto Cell Size, Convex Collision, Block Minimum Lifetime and the two
  allocation thresholds. Air Margin and Move With Object are beside
  Resolution.
- **Dissipation** accepts typed values above 1 for dense smoke that must clear
  quickly.
- **Source operations.** Smoke, Temperature, Fuel and Burn each choose Blend
  (the existing behavior), Add, Maximum or Replace under Channel Coupling.
- **Field masks for effectors.** Any force can be limited by the simulated
  Smoke, Temperature, Fuel, Burn or Speed, with an Invert option.
- **Scene Occlusion.** The live preview hides smoke behind solid objects using
  the viewport depth. On by default; switch it off in Preview Settings.
- **OpenVDB sources.** Map grid names from other tools, read Fuel and Burn
  grids, play numbered file sequences with a frame offset, and optionally emit
  the file's velocity grid. Grids with their own voxel size, including the
  velocity grid of a Fumaris bake, are now placed correctly instead of being
  read at the smoke grid's coordinates, and grid velocities keep their real
  speed instead of being shrunk by the voxel size.

Fixes

- Valid empty frames in OpenVDB source sequences now pause emission instead
  of aborting the bake; emission resumes when a populated frame arrives.
- Changing Move With Object during preview restarts the simulation instead
  of shifting existing smoke. Changing it during a bake reports an error.
- Scene Occlusion retains depth changes made during its redraw delay, so a
  paused preview updates after an object moves.
- Baked VDBs are now positioned exactly where the simulation is. Earlier
  versions wrote every grid half a voxel toward -X, -Y and -Z, so a bake sat
  slightly off its emitters, colliders and the live preview. Re-baking shifts
  the result by that half voxel; existing caches are not changed.
- Delete, Bake and cache migration now touch only numbered Fumaris frame files.
  Other VDBs sharing the output prefix in the Output Directory are left alone, and
  folders containing `[`, `]`, `*` or `?` work.
- A bake that runs out of disk space now stops with an error instead of publishing
  truncated, unreadable VDB frames.
- Stopping a bake no longer leaves the interrupted frame behind as a `.tmp` file.
- A Collection Mesh source no longer fails when one member has no faces.
- Importing a bake on Windows no longer fails when the cache is on another drive.
- A duplicated simulation no longer takes over the original's cache.
- When Sparse Block Capacity is full, a newly active emitter now gets blocks
  ahead of empty blocks that were only waiting out Block Minimum Lifetime.
- Baked velocity grids no longer contain stray values when Small Sparse
  Blocks is on (the default before 1.3.0). Parts of the velocity field were being
  overwritten with unrelated data from neighboring blocks, differently on
  each run and graphics card, which could streak motion blur and distorted
  bakes fed back in as VDB sources. Re-bake to get correct velocity.

Performance

- Faster per-frame export for particle, Collection Mesh, deforming, masked and
  Geometry Nodes mesh emitters; faster VDB conversion and VDB-source loading.
- Static meshes are uploaded once when a simulation starts on frame 0, and mesh
  geometry is no longer copied inside the solver bridge each frame.
- Moving convex colliders skip their repeated topology check.

Requires Blender 5.2+. Stop simulations and restart Blender after updating.

## 1.2.1 — 2026-10-01

- Fix baking when an emitter has no particles or active smoke on an initial frame.
  Empty frames now produce valid VDB files instead of a density-readback error.
- Fix disconnected smoke trails from small, fast-moving mesh emitters by allocating
  their swept path and preserving motion interpolation across solver substeps.
- Velocity settings and defaults remain unchanged. Existing scenes and caches are preserved.
- Requires Blender 5.2+. Stop simulations and restart Blender after updating.

## 1.2.0 — Apple Silicon package addition

- Added a native ARM64 Mac package using Metal through MoltenVK.
- Requires macOS 26+ and Blender 5.2+; tested on Mac mini M4, 16 GB.
- Includes the existing 1.2.0 simulation features and workflow. Intel Macs are not supported.
- Windows/Linux 1.2.0 downloads remain available; no mandatory update for those platforms.

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
