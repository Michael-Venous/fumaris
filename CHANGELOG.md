# Changelog

All notable customer-facing changes are recorded here.

## 2.0.0 beta 1 — Unreleased

Fumaris 2.0: a new solver core, physical fire, a live preview lit like Cycles,
two fire looks (Temperature and Pyro) and a reworked panel. A major version
because results change: **saved scenes keep their settings, but simulate and
look differently** (the sparse grid, pressure solve, rounding, combustion and
emitters changed; see Compatibility), and several features were removed or
replaced (see Removed). This is a beta: please report scenes that look wrong.

### Highlights

- **About 1.5x more simulation in the same memory, and faster bakes.** Blocks
  are stored without Flow's copied border, in 16-cell blocks that fit the smoke
  tightly (about 35% more smoke per GB, on macOS too). Smoke-only scenes store
  only the channels they use (Density Storage: Auto), up to about twice the
  smoke per GB. Bakes of the test scenes run about 30% faster than 1.3.1 while
  holding 36–59% more smoke; Apple silicon bakes up to 80% faster. Advection
  reads across block faces with fewer lookups: about 6% less GPU time per
  frame on Apple M4 and 8–10% on Intel Arc.
- **Physical fire.** One combustion model: burning releases Fuel Energy times
  the heat that takes cold air to Flame Temperature, per unit of fuel. Burn
  Time sets how long fire lasts and Cooling Time how fast heat fades after it;
  neither changes how hot fire gets. Fire glows by its temperature like real
  soot (Temperature look), in the render and the live preview alike.
- **Live preview lit like Cycles**: the scene's strongest light or HDRI sun
  with shadows, sky light, fire lighting the smoke, multiple scattering, and
  Blender's own colour management and blackbody colours.
- **Scatter Glow**: fire light spreads into the smoke around it, with Glow
  Spread (a long, soft falloff) and Glow Shadowing (dense outer billows stay
  dark while crevices next to the flames glow).
- **Simulation Memory** replaces Sparse Block Capacity: set GB (or Auto, 80%
  of free GPU memory); it is allocated up front, so GPU memory stays flat.

### Fire and combustion

- **Realistic combustion only** (Classic removed; such scenes open as
  Realistic). Combustion settings: Flame Temperature, Fuel Energy, Ignition
  Temperature, Burn Time, Cooling Time, Temperature Diffusion, Smoke Amount,
  Gas Expansion, Wisp Cooling, Flame Sheets, Oxygen Limit, Soot Radiation and
  Oxygen Field (Use, Mixing).
- **Gas Expansion**: burning gas pushes the smoke around it outward where it
  burns, the same at any resolution, frame rate or Sub-Steps.
- **Temperature Diffusion**: heat evens out between neighbouring voxels,
  smoothing speckle in fire (0.1 lowers cell-to-cell temperature noise about
  12%, 0.3 about 30%; about 7% more simulation time while on).
- **Oxygen Limit** and **Oxygen Field**: fire needs fresh air, so it burns in
  shells where fuel meets air, and with the field starves inside thick burnt
  gas. 0 burns evenly everywhere.
- **Soot Radiation**, **Wisp Cooling** and **Flame Sheets** break up evenly
  hot fireballs: sooty pockets radiate their heat away, thin gas cools faster,
  dense fuel burns in thin sheets with glowing seams.
- **Physical Buoyancy**: a checkbox beside Temperature Buoyancy that lifts hot
  gas as the gas law says (about 5.8 g at 2000 K).
- **Burning emitters**: an emitter can burn where it sits (a gas jet, a
  burning surface) without emitting fuel.
- Fire lights reliably from an igniter emitter (Temperature in Maximum mode at
  or above Ignition Temperature), and the emitter panel warns when an
  emitter's Temperature is too low to ignite its fuel.
- Cooling and burning are exact at any Sub-Steps: a short Cooling Time no
  longer reheats gas, and a short Burn Time no longer burns all fuel in one
  step.

### Motion and solver

- **Stronger, cleaner pressure solve**: two multigrid cycles, a checkerboard
  filter that removes the cell-sized odd-even pattern (and the straight-edged
  rectangles it caused in flames), and a sign error inherited from Flow fixed.
  15–25% less leftover compression; Projection Filter (Advanced) lets finer
  motion through.
- **Colliders are walls**, sitting on the real surface: smoke spreads along
  and wraps around colliders and reaches them, instead of thinning out or
  stopping a few cells short. Open Collider Wall (Advanced, 1 cell) keeps thin
  open planes sealed; Closed Collider Margin (Advanced) lets fire resting on a
  closed collider rise freely.
- **Livelier default motion**: Flow's constant velocity fade (which stalled
  large plumes) is replaced by a small **Velocity Damping**.
- **Wind**: a steady wind that carries all smoke downwind at no cost; Gravity
  from Scene copies the scene's gravity.
- **Vorticity** is weakened in proportion below Resolution 512, so coarse
  grids rise and spread like fine ones, and it no longer keeps pumping energy
  into the gas after a fire dies. **Vorticity Mask** chooses where it acts.
- **Detail & Shaping** (Smoke Behavior): a stack of layers, applied in order.
  Disturb (swirls of a set size, with Detail octaves and Min Speed), Grain
  (blocky kicks that break up fast fronts; Size, Edges Only, Min Speed,
  Layers) and Shred add motion; Vorticity, Dissipation and Cooling layers
  scale those rates per cell; Soot Burn-off thins smoke where gas is hot.
  Stack several (a large and a fine Disturb), and give each up to six
  masks: a map range (From Min/Max to To Min/Max) of Temperature (K), Speed
  (m/s), Smoke, Fuel, Burn, Air Left or Noise, combined by Multiply,
  Minimum, Maximum, Add, Subtract or Replace. New layers start with useful
  masks (Disturb and Grain: smoke, or heat up to 2500 K); with none a layer
  acts everywhere. Speed masks read the gas as each layer finds it. Clear
  air skips the noise.
- **Preserve Turbulence** (Advanced, advection–reflection) keeps more swirling
  energy; off for new domains.
- **Smoke Detail** (Advanced): Sharp carries smoke with cubic interpolation
  for finer billows; **Edge Sharpness** sets how strongly edges are restored.
- **Full-Resolution Velocity** (Advanced): motion at the full Resolution for
  rounder, finer billows, at about twice the memory per block.
- **Adaptive Sub-Steps**: Cells per Step (0 = off) uses only as many steps as
  the motion needs, up to Max Sub-Steps.
- Smoke, fire and heat no longer fade or stall at high Sub-Steps (small
  changes were lost to half-precision rounding), and look much the same at
  any step count.

### Emitters

- **Physical Expansion and Normal Velocity.** Expansion (formerly Divergence)
  is a rate per second, the same at any Resolution and Sub-Steps, and exact
  wherever a box, sphere or Surface mesh sits on the grid. Normal Velocity on
  closed meshes, spheres and boxes really blows the air out, closed meshes
  that grow or shrink push or pull the smoke, and open meshes act as fans.
  Point and particle emitters expand per second and push outward too.
- **Influence Only**: an emitter can steer, heat or expand the smoke already
  there without adding any (also for points and particles).
- **Velocity attribute** for mesh and Geometry Nodes mesh emitters.
- Fast point and particle emitters leave joined trails instead of beads.
- Static emitters are stamped once per step (up to 8x less emitter time);
  fast-moving mesh emitters emit from their real surface at any Sub-Steps.
- Collection Mesh emitters with many copies of one mesh export up to 8x faster.
- Point attribute tooltips say that a per-point value replaces the emitter's
  setting (it does not scale it).

### Look and material

- **Temperature look**: Brightness, Full Glow, Contrast (flame width), Colour
  Spread, Sharpness, Blue Core and Flame Tint; Anisotropy for forward
  scattering. The Burn look is removed. Appearance always drives the generated
  material, which is leaner (density, temperature and glow grids; fuel only
  for Blue Core).
- **Scatter Glow**, Glow Spread and Glow Shadowing (bakes add small glow
  grids; Glow Radius in Output). Fumaris shows a hint and a Use Biased button
  when Cycles' unbiased volumes would render the glow in square patches.
- **Pyro look** (Appearance > Look): Houdini Pyro-style fire shading with its
  own flame controls (Brightness, Full Glow, Contrast, Blackbody Scale, Blue
  Core, Flame Tint, Scatter Glow; separate from the Temperature look's, using
  the same brightness law so equal values give equal exposure) and a Colour
  choice (Golden ramp or real Blackbody): golden fire,
  softly glowing cells (a blurred heat field), dark hot soot. Thin Gas sets
  the smoke density where both looks glow alike; thinner gas glows brighter,
  denser smoke darker. Works at any scene size, and Full Glow applies without
  a re-bake. Bakes write two small half-resolution grids for it (heat and a
  softened density). The live preview shows it too (without the blur).
- Blue Core is 0 for new domains (with Brightness raised it turned fuel-rich
  scenes lilac).

### Live preview

- Lit like Cycles (see Highlights), with exposure and every Tone Mapping mode
  following Blender's colour management exactly (Standard replaces Off), and
  dithered so dark smoke does not band.
- Dense smoke as opaque as in Cycles and flame colours matching the material;
  Scatter Glow, Glow Spread and Glow Shadowing drawn in the preview (Glow
  Spread mixes two blur widths as the material does, so it changes smoothly).
- **Show** views: Speed, Expansion, Fuel and Active Blocks. Field views ignore
  the look's Brightness; Speed runs from dark (still) through red (10-20 m/s)
  to orange (fast) and never clips to white; Active Blocks outlines are
  thinner; Lighting settings hide while a field view is shown.
- **Preview Step Size** replaces Ray Steps; the preview is faster (no velocity
  reads for ordinary looks, rays stop once nothing behind can show).
- Dragging a mesh emitter while previewing costs much less (fast drags: the
  emitter pass ~2.4x cheaper, frames back within 24 fps): the live preview
  stamps a moving source at half its emission band instead of every 2 voxels.
  Bakes and recordings keep the finer stamping.
- **Frame Rate** (Preview Settings): Scene (default, real speed), Uncapped (as
  fast as the GPU simulates) or a Custom frame rate.
- Stays in the window it was started from; View > Show Fumaris Preview Here
  and All Viewports choose where it appears.
- The self-shadow and flame light volumes are counted in Simulation Memory.
- Smooth playback: panning while the preview plays re-renders between
  simulation frames (Rising Smoke: ~23 -> ~130 images/s, 24 fps kept), and
  playback no longer slows as Simulation Memory grows (Flow recreated its
  scratch textures every preview frame). Scene occlusion depth now follows the
  view while orbiting and playing.
- Scene lighting with a world: the sky-light shading inside the smoke no
  longer shows block-shaped patches that shifted as the smoke grew.
- Lighting: Custom replaces the scene's lights and world with its one light
  and its own grey Sky (shadowed by the smoke, for soft ambient depth), the
  same in every scene; it used to add the world's light.

### Baking and files

- **Faster Scatter Glow grids**: the glow is computed only around gas that
  glows, in parallel, and smoke far from the fire is skipped (a mushroom bake:
  glow time 8.4 s to 1.9 s, worst frame 234 to 95 ms, same grids).
- **Faster, smaller VDB bakes**: only cells holding smoke are read back,
  VDB files are written by more threads (FUMARIS_VDB_WORKERS limits them),
  bakes use far less RAM, and **VDB Writers** (Output) trades speed for even
  less: Balanced (6, the default: as fast as Fastest with ~22% less RAM), Low Memory (4 writers, ~40% less RAM for ~5% more time)
  or Ultra Low Memory (1 writer, about half the RAM, several times slower).
- **Smaller files**: VDB Precision (Half by default), temperature below 500 K
  not stored, a Full / Half / Quarter resolution menu for each grid (Density,
  Temperature, Fuel, Burn, Velocity; velocity Half by default, Full only with
  Full-Resolution Velocity), Density can be left out, optional Glow.
- **Takes**: every bake stores its settings in its cache slot; Restore
  Settings from Slot puts them back. **Make Lighter Copy** writes a half-
  resolution copy about 7x smaller.
- **Sparks** (Output > Add Sparks): sparks and embers flying through the baked
  velocity, glowing by temperature.
- A GPU-memory-full warning when the driver starts moving the simulation to
  system memory. The bar under the controls reads "VRAM in use", its total is
  the VRAM the simulation reserved (matching the VRAM field), and it notes
  when a changed VRAM waits for a restart.

### Workflow

- **Quick Setup**: Rising Smoke, Steady Fire, Explosion (optional Debris
  Trails), Ground Fog and Chimney Smoke, starting from the new defaults, at
  2 GB of VRAM with every frame under 25 ms on an RTX 4060 Ti (24 fps
  playback). The Explosion is grander (a longer, stronger burst and more soot:
  a large mushroom cloud) and Chimney Smoke breaks up into turbulent smoke.
- **Emitter operations**: new emitters Add what they emit (Smoke, Temperature,
  Fuel, Burn); Velocity gets the operation menu too (Blend by default). The
  coupling tooltips say what each rate means per operation.
- **Add Source**: Candle, Burst, Trail and Spreading Fire Geometry Nodes
  sources.
- **User presets** for domains and sources, shared between files; applying
  one restores the whole saved state.
- **Participant list** with Enabled and Solo, saying why an object is skipped;
  effectors are drawn in the viewport.
- Transport status (Live / Recorded frames) and **Scrub Recording**.
- **Preview While Baking** (next to Bake on Preview): watch the live preview
  while a bake runs; off keeps bakes fastest.
- Reorganised domain panel: paired rows, Advanced grouped by solver, moving
  domain, colliders and memory (Simulation Speed and Trace Steps first, then
  block allocation). Appearance shows only the chosen Look's controls, side by
  side, and hides them all when Flame is off. Emitters keep Influence Only and
  Burning under Advanced.
- Linux: a failed bridge start says what to fix (missing library, old glibc,
  no Vulkan driver).

### New-domain defaults

Burn Time 2 s, Cooling Time 2 s, Gas Expansion 0.3, Smoke Per Burn 1 (3 buried
the fire in dark smoke within a second), Temperature Diffusion 0.1, Soot
Radiation 1, Edge Sharpness 0.5, Dissipation 0.2, Smoke Decay 0.1,
1 Sub-Step with Trace Steps 16 (Cells per Step 8 applies once Max Sub-Steps is
raised; Trace brings 1 step much closer to a converged result), Simulation
Memory 4 GB; even burning
(Oxygen Limit 0, Oxygen Field, Wisp Cooling and Flame Sheets off); look: Full
Glow 2000 K, Brightness 2, Scatter Glow 2, Glow Shadowing 0.5, Blue Core 0,
Smoke Density 1, smoke colour 0.45, Anisotropy 0.3.

### Removed

Classic combustion and the Burn look, Upres, Small Sparse Blocks, Sparse
Block Capacity (scenes that set it keep it until Simulation Memory is
changed), the Adaptive toggle (Cells per Step 0), Shadow Min Light and Light
Scattering, and recognition of pre-1.0 beta data.

### Fixes

- Fast moving colliders at high resolution no longer blow up the simulation
  (a car at Resolution 1400 reached 12,000 m/s at frame 89): the pressure
  solve's coarse levels keep every sealed pocket solvable.
- Moving and dragged mesh emitters and mesh colliders cost much less: motion
  stamps share one closest-face pass and faces too far away are skipped (a
  source dragged 3 m per frame: emitter pass 19 -> 7 ms in a bake, 8 -> 3 ms
  live; a scene with two mesh colliders: 535 -> 302 ms over 60 frames).
  Results are unchanged.
- Preview Scatter Glow widens smoothly as the fire grows instead of jumping
  to twice its spread.

- OpenVDB sources read temperature grids in Kelvin: re-emitting a Fumaris bake
  made its gas millions of degrees hot (infinite in Half precision files).

- Simulation Speed now applies to emitter motion measured by the add-on:
  moving or deforming mesh emitters, collection meshes and points, velocity
  attributes and closed meshes that grow. At Speed 4 they pushed the air four
  times too hard (a growing source rose far too high); Speed 4 with 4
  Sub-Steps now tracks Speed 1 with 1 Sub-Step.

- A VDB bake could stop with "No density readback" when the last smoke
  vanished while warm air still moved.
- Frames with fewer Adaptive Sub-Steps no longer flash brighter.
- Changing Appearance while paused no longer briefly allocates several GB.
- Keyed emitter Temperature or Fuel starting at 0 no longer loses those
  channels with Density Storage on Auto.

### Compatibility

Saved scenes keep their settings; emitters and point emitters keep their
old Expansion behaviour and channel operations. Because the solver core
changed, saved scenes simulate differently:

- The pressure solve now converges with colliders: plumes next to the ground
  rise less (an expanding emitter on a ground plane about half as high at
  Resolution 80) and spread wider, and look the same at any resolution.
- Heat and smoke no longer drift with the Sub-Steps count (unbiased rounding).
- Fire scenes follow the new heat law and may look hotter or cooler.
- Existing bakes need a new bake for Glow Spread, Glow Shadowing and the Pyro
  look; generated materials rebuild when opened.
- New domains start with 1 Sub-Step, and new emitters Add what they emit;
  saved domains and emitters keep their values.
- Emitters saved before physical Expansion keep the older per-cell Expansion,
  which pushes harder at higher Resolution (about twice at twice the
  Resolution); their panel says so, and **Use Physical Expansion** converts
  them, keeping their push at the current Resolution.
- Turbulence Detail, Control Ramps and Soot Burn-off became Detail & Shaping
  layers (Min Speed and Edges Only as masks): saved scenes, presets and takes
  convert on load and simulate the same to within rounding; a Disturb or
  Grain speed ramp now reads the speed after the layers above it.

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
