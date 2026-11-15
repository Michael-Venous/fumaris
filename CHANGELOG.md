# Changelog

All notable customer-facing changes are recorded here.

## 2.0.0 beta 2 — Unreleased

A large update to the 2.0 beta: faster everywhere, more simulation per GB,
background baking and a reorganised panel. Still a beta: please report scenes
that look wrong. Frames differ slightly from beta 1 (smoke is now conserved
as it moves, and the result no longer depends on the VRAM setting).

### Highlights

Measured on an RTX 3090 Ti against beta 1, back to back:

- **Bakes about 2.8 times faster**: 200 frames of the comparison explosion
  to VDB in 21 s instead of 59 (less read back from the GPU, multithreaded
  VDB writing, Pyro and Scatter Glow grids built faster).
- **Faster simulation and preview**: Explosion at Resolution 1024 simulates
  in 28.7 ms a frame instead of 38.4 (40.6 instead of 56.0 with the live
  preview); the comparison explosion previews at 15.8 frames a second
  instead of 11.4. Smoke Detail Sharp costs about 1.7x Standard instead of
  2.4x.
- **More simulation per GB**: air around the smoke no longer stores smoke
  fields and the simulated space hugs the smoke: 9% more blocks in the same
  VRAM, 42% fewer smoke cells on the same shot.
- **Bake in Background, Bake All and Bake Selected**: several domains bake at
  once in their own Blender processes, on one or more GPUs, while you work.
- **Per-Emitter Color**: emitters with their own smoke color that mixes where
  plumes meet, with its cost shown before it can be turned on.
- **A reorganised domain panel** in setup order, a smoother first run and many
  fixes from a hands-on usability test (renaming a domain no longer stops its
  bake or preview).

### All changes

- **Air costs less**: blocks that hold only air around the smoke keep the
  velocity field and no longer the smoke fields (density, temperature, fuel,
  burn), which are about four fifths of a block's memory. The same VRAM holds
  7 to 9% more blocks (10 GB: 86,286 instead of 79,507 at 4 channels), and an
  Explosion at Resolution 1024 simulates 6% faster. A block gets its smoke
  storage back before anything can reach it: every cell that holds smoke,
  fuel, flame or air 0.1 K warmer than its surroundings reserves the blocks
  it could move into by the next frame. Baked files are identical with and
  without, byte for byte, in the eight scenes compared (60 to 120 frames
  each, Resolution 256 to 1024). Diagnostics shows the second pool ("With
  smoke storage"), and Voxels now counts the stored ones.
- **Tighter simulation space**: Air Margin is measured from the smoke itself
  instead of from the edge of the 16-cell block that holds it, so no whole
  block of empty air is kept where the smoke is far from a block's face.
  Explosion at Resolution 1024 simulates 21% fewer cells and runs 17% faster
  on the same frames (8 to 16% fewer blocks at 512); less VRAM in use
  likewise. Plumes rise 1 to 5% lower by frame 72 for having less air around
  them; raise Air Margin for more room.
- **Preview shadows cross clear air**: one plume now shadows another across
  unallocated space (the shadow's far part runs on a dense grid), as in
  Cycles; single plumes render as before, and the shadow passes are a
  little cheaper.
- **Mesh sources emit their amount whatever the grid**: each cell at a mesh
  source's edge takes the share of the emission band it covers, so the smoke
  a source puts out no longer changes with where its surface falls on the
  grid (Rising Smoke at Resolution 64 / 128 / 256 emitted 60 / 77 / 71 units;
  now 72 / 71 / 71), and a source thinner than a cell (a 2 cm shell, a thin
  sheet) emits instead of vanishing on coarse grids. Colliders are unchanged.
- **Large bakes read less back from the GPU**: the GPU now sends the bridge
  only the voxels that hold something (it sent every 8x8x8 leaf in full,
  nearly four times what the file needs), and no temperature below the 500 K
  the file keeps. With the faster file writer below, the 200-frame comparison
  explosion bakes in 26 s instead of 37; the files are byte-for-byte the same.
  On a card in a full-speed slot the GPU also gathers those voxels in its own
  memory and hands them over in one copy (24.5 s; a card on a narrow link
  keeps writing them directly, which is faster there).
- **Large bakes hold less system memory**: the bridge kept three copies of
  the buffers the GPU's results arrive in where two are in use (5.4 to 3.5 GB
  on the 200-frame comparison explosion; the bridge's peak 13.7 to 11.7 GB).
- **Scatter Glow costs half as much to bake**: the glow grids are summed
  from blocks of 4x4x4 voxels instead of voxel by voxel (199 to 89 ms a frame
  on the comparison explosion, which now bakes in 21.4 s). Their lattice
  cells are a whole number of such blocks, so `glow` and `glow_wide` can sit
  on a slightly different lattice than before (1 to 2% apart, the same total).
- **VDB files are written 2 to 4 times faster**: the bridge now encodes the
  voxel data of a frame on several threads and converts to half floats eight
  values at a time; OpenVDB still writes the file's structure. The files are
  byte-for-byte what OpenVDB wrote (checked on whole bakes), so nothing changes
  for Blender or other programs. The 200-frame comparison explosion bakes in
  33 s instead of 37 on a 24-core machine, and the gain grows as cores get
  fewer. Compression set to ZIP, Blosc or None uses OpenVDB's writer as before.
- **Bakes write faster**: the Pyro look's `heat` and `density_soft` grids
  are now built on the GPU (the same grids to half-float precision), and the
  VDB writers no longer hand their memory back to the system after every
  frame (which made each next frame page it in again). The 200-frame
  comparison explosion bakes in 43 s instead of 61 on a 24-core machine; the
  bridge keeps about 2 GB more RAM while a large bake runs.
- **Smoke no longer thins out as it moves.**
  The solver's transport gained a little smoke where gas expanded and lost some
  where it swirled (the Explosion preset ended with about 10% less than it
  made); each step now scales the moved smoke back to the amount it started
  with. Dissipation, Decay and combustion still apply as set. Bakes from beta
  1 keep slightly more smoke late in a shot. It costs nothing measurable and
  has no switch (scripts can set `conserve_smoke` to reproduce beta 1).
- **The live preview plays faster**: while it plays every frame as fast as
  the GPU goes (Frame Rate Uncapped), the next frame is sent before the
  current one is shown, so the solver's CPU work and the image hand-off no
  longer wait between GPU frames (Explosion 1024 on an RTX 4060 Ti: 18 to 22
  frames a second). The frames and images are the same.
- **Per-Emitter Color** (Look, under the smoke Color): each emitter's smoke
  carries its own Smoke Color (the swatch beside Smoke on the emitter); where
  plumes meet, the colors mix by amount, and smoke from combustion takes the
  color around it. The preview shows it, bakes gain a `color` grid, and the
  generated material multiplies its Color by that grid. It costs a second
  smoke field (about 1.4x the time per frame and a third fewer blocks for the
  same VRAM), so it is off by default and cannot be turned on unawares: until
  it is on, an emitter shows a color button that explains the cost and offers
  to turn it on, or to set the domain's Color instead; with it on and every
  emitter one color, the domain says that Color does the same for free. The
  density itself is unchanged by the option (byte-identical bakes).
- **Bake in Background** (the menu beside Bake), **Bake All** and **Bake
  Selected** (Fumaris tab of the sidebar): bake several domains at once, each
  in its own Blender process, while you keep editing or previewing. The
  Fumaris preferences (Baking) set on which GPUs; by default (Auto) one bake
  runs per ticked GPU. Progress, the GPU in use and Stop are on the domain's
  panel and in the sidebar. A bake starts about a second later than in the
  foreground (a second Blender loads the scene).
- New domains start with the cache directory you last set, in any scene
  (stored in the Fumaris preferences as Default Cache Directory).
- Preview **Color Management** (View and Look side by side, like Blender's):
  any View other than Scene takes its own Look (for example AgX with Punchy
  for more saturated fire). Khronos PBR Neutral and ACES views added.
- The GPU choice is in the Fumaris preferences only (it was also in the
  Simulate panel). The preferences are grouped: Simulation GPU, Cache, Live
  Preview Shortcuts, Runtime.
- A long list of separate mesh or sphere emitters shows a hint: one Collection
  Mesh or Collection Points emitter simulates several times faster.
- **Smoke Detail Sharp takes 30% less time**: its cubic smoke transport looked up
  every one of its 64 cells in the sparse grid; it now finds them through the
  block, as Standard does (Explosion at Resolution 1024: 52 to 37 ms a frame
  on an RTX 3090 Ti, about 1.7x Standard instead of 2.4x). The results are
  the same, byte for byte.
- **The domain panel is reorganised** in the order a shot is usually set up:
  Simulate, Look (Appearance and the live preview's settings, which were
  folded inside Simulate), Smoke Behavior, Participants, Output, Versions,
  Advanced. Play, Bake and their Stop share one row; Bake in Background and
  Delete Bake are in the menu beside Bake (Delete also beside the bake's name
  under Output). Simulation Speed, Simulation Scale and Move With Object moved
  from Advanced to Smoke Behavior. Combustion shows its seven main settings
  and folds the rest under More; Advanced folds the solver's internals (Trace
  Steps, block lifetime, allocation thresholds, Convex Collision). A folded
  group's title counts the settings changed from their defaults. Collider
  Face Flux left the panel (with moving colliders it changed nothing
  visible); the emitter's Channel Coupling is now Emission Blending.
- **The live preview's sun shadows match Cycles.** Sun-lit smoke previewed
  1.3-1.7x brighter than it rendered: the preview's shadows reached only 12
  voxels into the smoke. They now reach across the whole simulation, at the
  same cost (preview within about 7% of Cycles on a sun-lit plume). With
  Cycles' Volume bounces above 0 the preview was 2-3x too bright for dark
  smoke; the bounces now follow the smoke's colour.
- **The result no longer changes with VRAM (GB) or Auto**: the same scene
  simulates to the same frames whatever memory the simulation is given (as
  long as it fits). Bakes without Preview While Baking get about 4% more
  space from the same memory. Frames differ slightly from beta 1.
- Importing a bake no longer loads its last frame's grids just to read their
  names (about 30 MB of Blender memory kept per import). A domain deleted with
  Blender's X is freed when another domain is chosen, and the preview's
  view-transform tables are bounded.
- Sparks follow the domain's bake through another bake and a version switch
  (they went empty), keep their settings when Add Sparks is used again, move
  with the gas at any Simulation Speed and Scale and under a moved volume,
  and have a Brightness setting and tooltips. Add Sparks on a bake without
  velocity or temperature says so instead of adding sparks that cannot work.
- Sparks show up without playing from the first frame: Add Sparks, another
  bake and a version switch simulate them again up to the frame on screen
  (they stayed stale until the timeline was played from the start). The new
  object goes into the baked volume's collection, selected and active (it
  landed in the scene's root collection, unselected).
- A later bake without the velocity or temperature grid no longer leaves the
  sparks only falling, silently: Bake reports it, the panel shows a warning
  beside the Bake row and the grid is turned on again in Output.
- Sparks blur with Cycles' motion blur (they carry a `velocity` attribute).
  Brightness starts at 0.1: hot sparks orange, cool ones red; at the old 1 all
  of them rendered pinkish white under AgX. Sparks already in a file keep
  their value.
- Make Lighter Copy imports and shows the lighter version (the domain switched
  to it without importing it, so the fire left the viewport until Import).
- Importing a bake with velocity no longer logs "Could not find grid with name
  velocity"; the volume's Velocity Grid is set, so Cycles' motion blur follows
  the baked velocity.
- Renaming a domain no longer stops its live preview or its bake (it stopped
  with "The active Fumaris domain was deleted", and Play stayed unavailable
  until the file was reopened). A domain deleted with Blender's X stops its
  preview at once, and its background bake no longer reports finishing for a
  domain that is gone.
- Play and Bake in an emitter's panel run that emitter's own domain (they ran
  the domain chosen in the sidebar), and a selected domain is the one the
  sidebar's Play and Bake run. Selecting an emitter keeps the sidebar's
  choice, so Add Objects still adds to it.
- A refused action (Bake or Delete on a protected version, Play without
  emitters) is reported without becoming the domain's lasting error in
  Simulate. Clearer messages: Bake Selected with no domain selected, Play
  without an emitter collection, Play on a domain waiting for a background
  bake. Protect refuses an empty version, and Delete on one says there is
  nothing to delete.
- **New domains start on Auto VRAM** (Quick Setup, New Domain, or Role set
  to Domain): they reserve 80% of the GPU memory free at the start. Since the
  result no longer depends on the amount, this only gives the smoke more
  room; turn Auto off to leave memory to other programs. Domains in existing
  files keep their setting.
- Temperatures show their unit: Flame Temperature, Ignition Temperature and
  the Look's Full Glow, Start and Full Temperature read "2000 K" (in Celsius
  if the scene's units are set to it).
- **Restart is about three times faster**: the live preview's Restart
  starts over in the running simulation instead of launching a new one.
  The comparison explosion is back at frame 5 0.3 s after the click instead
  of 0.9 s (RTX 4060 Ti), and Blender no longer freezes for 0.4 s on the
  click. A different GPU, or a change that needs a new session (VRAM, frame
  range, sources), still starts a new one.
- Warnings for an End frame before Start and for VRAM (GB) set above the
  GPU's memory (it ran, slowly, in system memory).
- Make Lighter Copy also halves the Pyro look's `heat` and `density_soft`
  grids (large frames 8.4 times smaller instead of 7.2) and is unavailable on
  a version that is already a lighter copy.
- The "first-run shader compilation" notice shows once per Blender session,
  not on every Play, and a stop's message no longer ends with the list of
  GPUs.
- A narrow Properties editor cuts off less: the Simulate box's status lines
  wrap; Gravity and Wind stack their fields; the emitter's shape settings,
  Detail & Shaping and its masks have shorter or stacked labels; Physical
  Buoyancy has its own labelled checkbox. Under Output the version is named
  `Version` with its tools `Lighter` and `Sparks` named where they fit; empty
  Directory and Prefix fields show their defaults; Density and Temperature
  show ticked and greyed when the generated material needs them. Advanced and
  Diagnostics have headers like the other sections. The live preview's
  `Show` calls the Look `Smoke & Fire`, its colour settings are headed
  `Color Management`, and `Colour` labels are `Color`.

## 2.0.0 beta 1 — 2026-11-12

The first Fumaris 2.0 release: a new solver core, physical fire and a live
preview that looks like the render. **Scenes from 1.x are not converted** (see
Compatibility). This is a beta: please report scenes that look wrong.

### Highlights

- **A new solver core.** Smoke keeps its motion and rolls instead of fading,
  the pressure solve is stronger (no more rectangles in flames), colliders act
  as real walls, and results hold up across Resolution and Sub-Steps.
- **The same look with fewer sub-steps.** New domains run 1 Sub-Step (1.3.1
  used 2): fast smoke follows the flow within each step (Trace Steps). About
  35–40% less GPU time per voxel than 1.3.1 at default settings (Explosion and
  Rising Smoke, Resolution 1024, RTX 4060 Ti).
- **More simulation per GB.** VRAM (GB) replaces Sparse Block Capacity and is
  reserved up front, so memory stays flat: about 1.4x as much simulation per
  GB, up to about 2x for smoke-only scenes.
- **A live preview lit like Cycles**: the scene's sun or HDRI with shadows, sky
  light, fire lighting the smoke, multiple scattering and Blender's own colour
  management.
- **Physical fire**: one combustion model (Flame Temperature, Fuel Energy, Burn
  Time, Cooling Time, Gas Expansion) with Soot Radiation, and an optional
  oxygen model (Oxygen Limit, and an Oxygen Field in which fire starves inside
  thick burnt gas).
- **Detail & Shaping layers**: stack Disturb, Grain, Shred, Vorticity,
  Dissipation, Cooling and Soot Burn-off, each limited by up to six masks
  (temperature, speed, smoke, fuel, burn, air left, noise).
- **Two fire looks**: Temperature (blackbody) and Pyro (Houdini-style), with
  Scatter Glow, so fire light spreads into the smoke around it.
- **Preview cache and Versions**: scrub and replay what the preview simulated,
  save it as a VDB version or a snapshot, and compare versions with the
  settings that made them.

### Simulation

- Vorticity scales with Resolution and has a Vorticity Mask; Adaptive
  Sub-Steps (Cells per Step); Wind and Gravity from Scene.
- Advanced: Preserve Turbulence (at least 2 Sub-Steps), Smoke Detail Sharp,
  Full-Resolution Velocity, Projection Filter.

### Emitters and sources

- Expansion (formerly Divergence) is a rate per second; Normal Velocity really
  blows air out of closed meshes; Influence Only; Burning emitters (also points
  and particles); a per-channel operation for each emitter.
- **Add Source**: Candle, Burst, Trail and Spreading Fire Geometry Nodes sources.
- Thin collider planes stay sealed (Open Collider Wall); moving colliders are
  smooth between steps; effectors are drawn in the viewport.
- OpenVDB sources read temperature in kelvin.

### Live preview

- Show views: Speed, Expansion, Fuel and Active Blocks.
- Smoother: panning while playing re-renders between frames, occlusion
  follows fast orbits, and Preview While Baking.
- Preview Frame Rate, and Show Fumaris Preview Here or in All Viewports.
- The preview works in orthographic views (it showed nothing).
- A looping preview repeats exactly: Disturb and Grain restarted their noise
  only when Blender did, so each loop came out different.

### Baking and files

- Versions list: bakes, preview saves and snapshots, pinned first; Bake asks
  whether to replace a pinned bake or start a new one.
- Smaller VDBs: Half precision by default, per-grid resolution, optional
  Density and Glow; Make Lighter Copy; Add Sparks (embers from the bake).
- Faster VDB writing with bounded RAM (VDB Writers).
- Escape stops a bake, a playing preview or a save.

### Workflow

- Quick Setup: Rising Smoke, Steady Fire, Explosion, Ground Fog and Chimney
  Smoke.
- User presets for domains and sources; a participant list with Enabled and
  Solo; clearer status and diagnostics; a reorganised panel.
- A Fumaris tab in the 3D Viewport sidebar lists the domain's objects: click
  one to show its settings. Bake from the Timeline and viewport headers, and
  Restart a running preview from its first frame in one click.
- A Biased/Unbiased switch for Cycles volumes in Appearance.
- The generated material is organised into node groups; Sync Material per
  version.

### Compatibility

- Blender 5.2 and 5.3 (tested with 5.2.3 and 5.3.0).
- 1.x scenes open, but removed settings are dropped and the rest take the new
  defaults: set the domain and sources up again (Quick Setup is a good start).
  Expansion values from 1.x push about ten times harder.
- Defaults changed: Sub-Steps 2 → 1, Dissipation 0.5 → 0.2, Smoke Amount
  3 → 1, Flame Brightness 5 → 2.

### Removed

- Bake on Preview and Scrub Recording (the preview cache replaces them).
- Upres (simulating at a higher Resolution is faster).
- Classic combustion and the Burn look; Small Sparse Blocks; the Adaptive
  toggle (Cells per Step 0); Turbulence Detail (now layers); Ray Steps (now
  Step Size).

### Known issues

- Results change slightly with VRAM; a preview and its bake agree.
- The Pyro look's heat blur shows in renders only.
- Sun-lit smoke previews about 1.3x brighter than Cycles renders it.
- The first replay of a large cached preview stutters once.

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
