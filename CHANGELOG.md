# Changelog

All notable customer-facing changes are recorded here.

## Unreleased

- Added: All domains in Versions groups the scene's bakes, snapshots and playblasts by domain. Use the eye beside each VDB version to set up which bakes play together without switching domains.

- Changed: Full-Resolution Channels uses less GPU memory. The solver reuses the full-resolution density texture where possible and computes the smoke's motion paths directly on a stationary full-resolution grid (Cauliflower at Resolution 1100: 10.09 to 7.75 GiB on an RTX 4060 Ti). New full-resolution bakes can differ in the exact shape of individual folds. Existing bake files still play, and half-resolution motion is unchanged.
- Changed: Full-Resolution Channels moved from Advanced up to Simulate, under Resolution and VRAM, with its cost note and memory forecast: for billowing explosions it is the biggest single quality choice.
- Fixed: changing Per-Emitter Smoke Color during playback restarts the preview with the right VRAM budget, even if the simulation already kept all four channels.

- Fixed (Windows): a background bake in a deep project folder could fail at its end with "The filename or extension is too long" (WinError 206): the folder Fumaris imports the bake from now has a shorter name.
- Changed: moved the Per-Emitter Smoke Color explanation and simulation cost into its tooltip. The Color section still shows advice when all emitters use the same smoke color.
- Fixed: cache scans ignore directories named like numbered VDB or PLY frames. If a directory blocks a frame write, the bake reports that frame and keeps the output already written.

- Fixed: saving a copy of a Blender file beside the original could share a writable bake cache. The first cache edit in the copy gives it its own cache and keeps its existing versions.
- Fixed: Replace keeps the previous bake until the new one finishes. If the new bake fails or is stopped, any completed VDB frames are kept in a separate partial version.
- Fixed: an uncapped preview could stop with a frame-order error during its warmup frames.
- Fixed: the playback controls in Properties, the sidebar and the headers now agree. Play after Stop starts from the beginning, Release Cache frees the kept frames, and Play from a baked domain preserves the bake in Versions.
- Fixed: a background bake that finishes while another scene is open imports its volume when you return to the bake's scene.

- Fixed: after an incomplete update (an old simulation program left behind while Fumaris was replaced), Bake and Bake in Background deleted the frames of the bake they were replacing and only then found that the simulation program was from another version: the old bake was gone and nothing replaced it. The add-on now checks the simulation program before it touches the bake, and the bake stays.
- Fixed: an OpenVDB source with small bits of smoke far apart (two specks 16,000 voxels apart) took more than 8 GB of RAM and 16 s before it was refused as too large for the volume budget; it is now refused at once.
- Added: a particle source of type Mesh warns when its instances are smaller than two cells of the domain. Blender scales each instance by the particle system's Size (0.05 by default), which easily makes them too small to reach the grid, so they emitted almost nothing without saying why.
- Fixed (Windows): the live preview could not run faster than about 64 frames a second: after each frame the simulation program waited a whole Windows timer tick (15.6 ms) however fast the GPU was. On an Intel Arc laptop a Rising Smoke preview at Resolution 256 and 50% image size now takes 5.5 ms a frame instead of 15.6 (2.8x the frame rate), 11.0 instead of 15.8 at full HD, and Resolution 512 at full HD 14.6 instead of 17.0.
- Changed (Linux): the live preview image reaches Blender through shared memory instead of the pipe: Rising Smoke at Resolution 512 previews at full HD in 6.1 ms a frame instead of 7.8 (the image took 3.0-3.3 ms to arrive, now 0.8-1.0 ms). Windows and macOS keep the pipe (no gain measured on Windows; slower on macOS). The image is the same.
- Changed (Windows): background bakes and the simulation program ask Windows not to slow them down as background processes (power throttling); they have no window of their own.
- Fixed: with Blender 5.2.0, the Trail and Spreading Fire sources (Add Source) emitted nothing: that Blender version left the new modifier's Curve, Surface and Ignition inputs empty. They are now set on the modifier (Blender 5.2.2 and 5.3 were not affected).
- Fixed (Windows): a background bake whose Blender was closed while it ran could leave its job folder behind when the next Blender started at the same moment it ended, and a bake could miss that its Blender had closed if Windows had already given that Blender's process number to another program.
- Fixed: Smoke Decay 1.0 removed all the smoke every step, so a source's smoke never left it. The rate compounds over each second, so the slider now ends at 0.99 (1% left after a second), and older files set to 1.0 simulate as 0.99.
- Fixed: bakes with fire glow are now guaranteed to write the same files run to run: one total used for the glow was added up in an order that depended on how the work was split between CPU threads.
- Changed: bakes with fire glow (the Explosion and other Pyro looks) write their VDB files faster, which speeds up bakes on computers with few CPU cores: an Explosion at Resolution 768 on 4 cores bakes in 7.8 s instead of 10.8 s (the glow and Pyro grids took 400 ms of each frame's writing, now 220 ms). The files are identical.
- Changed: OpenVDB sources that play a sequence load each new frame faster: only the parts of the file that hold something are read into the simulation and sent to the GPU, and the smoke grid is sent once instead of three times (a 512 Fumaris explosion used as a source: 311 -> 198 ms a new frame; a 256 one 74 -> 55 ms; the cauliflower scene's four VDB emitters 15 -> 11 ms a frame). Results are identical.
- Fixed: masked and deforming mesh sources could bake slightly differently depending on what was simulated before them in the same step (which of two equally close faces of the mesh a cell took depended on the face search built before); with the surface-search reuse of this version that showed up as a different bake. Each mesh's search now depends only on the mesh. Rising Smoke, Steady Fire, Explosion and unmasked static or moving sources bake identically; masked and deforming sources bake slightly differently from before.
- Changed: a mesh source with an Emission Mask that does not move or change is read once and then reused, like an unmasked one: it was evaluated and sent again every frame (an 82,000-triangle sphere masked by a vertex group: 58 ms of each frame, now under 0.1 ms). Masked smoke sources bake identically; a masked fire source can bake slightly differently from before (it now matches the exact surface search).
- Changed: Save Playblast frames like the viewport it records: looking through a camera it records the camera's frame (the render resolution's shape, what is inside the passepartout) at the render resolution, instead of the whole viewport; other views keep the viewport's own shape. It records the 3D viewport it is started from (the largest one when started from another editor). An animated camera is followed frame by frame.
- Added: while the timeline plays with baked volumes hidden (Preferences > Hide Volumes While Playing, on by default), the 3D Viewport and Timeline headers say "VDBs hidden while playing"; clicking it shows them and plays through the VDB sequence (it turns the preference off). The play head seemed to skip the VDBs with nothing saying why.
- Changed: the end of a bake no longer reads every VDB frame again to import it: a bake reads each frame's details as it is written, still cached, and a background bake hands them to the scene. On a hard disk this took 1.1-2.7 s per domain at 90 frames (Blender froze meanwhile; several domains ending together froze it for 6.7 s); now under a millisecond, for Bake and Bake All alike.
- Changed: a background bake's volume appears within 0.2 s of its end (up to 1 s before), and its progress updates no longer wait between frames when a virus scanner or the scene's own reading holds the status file (Windows).
- Fixed: with Background Bakes set higher than the GPUs ticked, a bake running alone under Auto VRAM took only its share of the card (a half or a third) as if the others were running. Only bakes that run at the same time on a GPU share it now.

- Added: View Changes' Measured shows the amount of smoke and of heat at the frame (each grid's values summed over its volume, with the change in percent), so a change that thins or thickens the smoke shows as a number, not only as more or fewer voxels. Bakes store the amounts in their VDB files' headers as they are written (no measurable cost); bakes made before have none.
- Fixed: a source that adds nothing to the smoke (Influence Only with Temperature 0 and no Velocity, for example one that only expands or pulls in) held the air inside it still while it moved with Motion Velocity Scale 0: a plume rising through such a moving sphere kept 19% of its speed there, a box 44%, a mesh 26%. The air now passes through as if it were not there. Mesh, box and sphere sources follow the same rule (a moving source with Motion Velocity still pushes the air). Scenes with such sources bake differently near them.
- Fixed: a preview restarted from its first frame (Play again, Loop, a change that restarts it) came out slightly different from a new preview of the same scene, more so at high Resolution after a long run (Explosion at 768: frame 6 differed by up to 29 of 255 levels): moving sources lost their motion on the first frame and the second frame allocated from the old run's state. Restarted runs now repeat a new one bit for bit. Bakes are unchanged.
- Added: the add-on and its simulation program check that they are from the same version. After an incomplete update (a simulation still running or a file locked while Fumaris was replaced), a preview or bake stops with "The Fumaris add-on and its simulation program are from different versions ... reinstall Fumaris from its ZIP file" instead of failing on a missing setting or simulating with settings it misread.
- Added: with Cache Preview, Stop keeps the cached frames: the simulation's GPU memory is released (an Explosion at Resolution 192 keeps 27 MB of its 1.8 GB), particles included, and scrubbing, timeline playback and Save Sequence keep working as while paused; frames outside the cache show nothing, warmup frames before frame 1 included. Changing the simulation's or its particles' settings releases the kept frames. The panel says "Stopped: cached frames kept" with the RAM they use. Play starts the preview again from the first frame; Release Cache (the Stop button now), a change to the simulation settings, starting a bake or another preview, or loading a file frees them. A stopped preview no longer blocks Bake, Quick Setup or Add Objects.
- Changed: mesh sources and colliders that do not move or change reuse their surface search between steps and frames: small scenes simulate faster (Rising Smoke at 512: 4.6 -> 3.2 ms a frame, Steady Fire 1.6 -> 1.3 ms of GPU). Results are identical.
- Changed: fires whose fuel comes from mesh sources simulate a little faster: the fuel the sources add is gathered only around them instead of over the whole fire (Steady Fire at Resolution 1024: 5.11 -> 4.94 ms of GPU a frame, a 6 m fuel bed at 512: 7.60 -> 7.23). Results are identical.
- Changed: the preview cache stores the smoke's detail only where the simulation keeps it: live frames with Cache Preview on are about 7% faster and cached frames show about 10% faster (Explosion at Resolution 768). Cached frames look the same.
- Changed: Per-Emitter Color costs much less: the Explosion at Resolution 1024 simulates 15% faster with it on (81 -> 69 ms a frame) and needs 2 GB less GPU memory, and it now stays within Simulation Memory (it took one more volume's worth than VRAM was set to). Where two colours meet, the blend is slightly softer; the smoke itself is unchanged.
- Changed: VDB files are written about 10% faster on one core (a 54 MB frame 40 -> 36 ms), which helps machines with few cores. The files are unchanged.

- Changed: colliders at rest hold the air at their own velocity only a quarter of a velocity cell out from their surface (new Advanced setting Collider Still Air, formerly Closed Collider Margin, now 0.25 and for every mesh and box collider) instead of their whole Collider Margin (0.3 m). Smoke now rests against walls (a plane facing rising smoke held it 0.19 m off at Resolution 512, now one voxel) and flows through gaps and pipes two velocity cells wide (at Resolution 256 a 0.5 m slot let none of a plume through, now 31%; 0.5 m pipes carried nothing, now smoke flows out of their top). Gaps of one cell or less stay sealed and nothing leaks through walls. Moving colliders keep their whole margin, as before. Set Collider Still Air to 0 for the old behaviour; scenes bake differently near resting colliders.
- Fixed: fast moving and rotating colliders lost or made smoke where they swept, and fast thin plates passed through it: a collider that moved farther than its Collider Margin in one step jumped over the smoke it should push. With Cells per Step above 0 (the default), a mesh or box collider now adds Sub-Steps for the frames it moves that fast, beyond Sub-Steps if needed (up to 8), so it moves at most its margin per step. At Resolution 256 / 512 a 4 m plate at 8 m/s keeps all of a cloud (49% / 6% before), a 0.8 m box at 16 m/s 99% / 96% (63% / 44%), a 0.1 m plate at 24 m/s leaves nothing behind (43% passed through at 512), a paddle at 3 turns a second keeps 83% / 89% (it made 110% / 49% more smoke). Those frames take as many times the GPU time; colliders slower than that change nothing. Scenes with fast colliders bake differently.
- Changed: the wall behind an open collider mesh (a plane) is Open Collider Wall thick (1 velocity cell) instead of Collider Margin: smoke from behind a plane rests 0.125 m from it at Resolution 512 (0.31 m before).
- Changed: gas may now leave a wall: next to a collider it could not move away from the surface either, which held a velocity cell of still air on every floor. Steady Fire at Resolution 192 has 22% more hot gas above its bed.
- Fixed: deforming colliders (shape keys, armatures, modifiers) did not push smoke; they swallowed what their surface moved into (a sphere inflating into a cloud kept 55% of it, now 87%).
- Changed: Detail & Shaping is now Modifiers, its own folded card below Smoke Behavior (its title counts them, "Modifiers (2)"): one stack for detail (Disturb, Grain, Shred), per-cell rates (Vorticity, Dissipation, Cooling, Soot Burn-off) and particles, added from a menu grouped by purpose, each with the same masks. Quick Setups fill it as before. Files keep their layers and bake identically.
- Added: Particles, as Spawn Particles modifiers (Spawn Tracers, Spawn Embers in the Modifiers menu): GPU particles that ride the smoke, simulated with it in every step, so they show in the live preview and a bake writes them with its VDBs (a point-cloud file per frame; each modifier gets an object that shows its particles as a point cloud that renders in Cycles and works with Geometry Nodes). The modifier's masks decide where in the smoke they spawn, denser where the masks give more (tracers in cool smoke: a Temperature mask; embers in burning gas: the default); they can also spawn from mesh surfaces or volumes, point clouds and Geometry Nodes points. Up to four per domain, each with its own drag, gravity, effectors (how hard the domain's effectors push them directly: embers feel a wind), lifetime and bounce/stick/kill on colliders; Max Particles and the preview's share are shared (Particle System, under the stack). Fast particles no longer jump through walls a velocity cell or thicker. Bakes are identical run to run and to the live preview. Baking over a version or deleting it removes its particle files, and a particle frame that cannot be written (a full disk, a file in the way) stops the bake and names the frame. Particles get at most half of VRAM and no more than the GPU can address (an Intel Arc 140V: about 22 million); the Particle System panel says when Max Particles is more than fits. A million particles add about 0.4 ms of GPU time per frame and 35 MB of cache per frame. Files with Particles from the beta's own Particles section open with them as a modifier.
- Changed: Sparks start only where the hot gas rises (new Lift Speed input, 0.5 m/s): embers are carried up by rising gas instead of being born in still air at a fuel bed and falling through the ground. Sparks already in a file keep their settings; Add Sparks again for the new behaviour.

- Added: turning on Full-Resolution Channels after a preview or bake shows what that run's busiest frame would need with it ("Last run's peak at full res:" / "~12 of 9.3 GB: smoke cut off"), in red when it would fill or overflow VRAM. Auto VRAM keeps the same amount with it on, which holds 3-4 times fewer blocks, so a scene that fit could be cut off seconds in with no warning before the run. The red line also shows under VRAM while Advanced is folded.
- Added: View Changes measures a bake against the version the domain shows: smoke and heat voxels, extent and top (how high it rose) at the current frame, the frame's file size, and the bakes' time, time per frame, peak VRAM, RAM and sparse blocks, with the change in percent. All read from the files' headers and the stored bake statistics: they appear at once.
- Added: background bakes show in the 3D Viewport and Timeline headers ("Baking 2 · 45%") with an X that stops them all, so they are visible while you work elsewhere.
- Fixed (macOS): a bake's peak RAM read 0. It is now measured as on Linux and Windows.

- Changed: the Steady Fire quick setup is a fuel fire: flames rise from a fuel bed in licking tongues, yellow at the base and orange at the tips, and turn into dark smoke a few source widths up. It was a column of smooth, washed-out peach balloons on a glowing ball. New Source now makes a flat fuel bed (2 m across, 0.25 m high) standing on the 3D cursor, on a ground plane, instead of a ball centred on it (the ground's collider sits a bed height lower and is hidden in renders, so the fire also burns at low Resolution and in a 50% preview); the fire burns richer fuel, stops glowing sooner (Cooling Time 0.6), makes half the soot, adds Shred and a Vorticity layer in the flames, and its Pyro look is darker (Brightness 0.08, Contrast 1) so it is not overexposed in AgX. It also uses about half the sparse blocks; the live preview is as fast or a little faster. Existing scenes are unchanged.

- Fixed: when the smoke filled the space VRAM gives it, it was cut off and only the panel's capacity warning said so: nothing in the log or in a bake's report. The warning now names the frame it ran out at ("Capacity reached at frame 31"), the bake's report says the smoke is cut off from that frame on, and the system console logs it once per run with the block counts.
- Fixed: a Resolution far too fine for a source (the default 2 m source at Resolution 16.7 million and above) simulated nothing, with no message (lower values filled the capacity with a patch of the source). The panel and the console now say the source is too large for this Resolution and how many blocks its surface alone needs. Resolution keeps no upper limit for physical-scale scenes.
- Fixed: smoke-only scenes with Full-Resolution Channels used 4-16% more GPU memory than VRAM was set to (Ground Fog at 6 GB: 6.8 GB). The memory each block needs is now counted right: the same VRAM holds 5-13% fewer blocks of smoke-only scenes, and stays within it. Fire and half-resolution motion are unchanged.
- Fixed: the live preview under world light (an HDRI or a sky) drew rising smoke too dark, about 0.8x Cycles, darkest down the middle of a column. Smoke now takes the world's light from every direction, as in Cycles (0.98x). Thin, pale smoke with many volume bounces no longer previews brighter than Cycles (up to 1.5x before, 0.93-1.06x now across dark and pale, thin and dense smoke, 0-12 bounces).
- Changed: fire bakes no longer write the `density_soft` grid, which nothing has read since 2.0.0 beta 2: fire VDBs are 7-17% smaller (Explosion and Steady Fire at Resolution 256). Bakes that have it still load and render.

- Changed: the Burst source (Add Source) throws two dozen clumps of fuel and heat out of a ball at their own speeds, sizes, delays and fuel, as the Explosion quick setup does: the burst grows uneven lobes, a few pockets burn on, and it rolls up into soot. It was one shell of points pushed straight out, so it always made a round ball. The clump source has a new Downward Spread input (0 for the Explosion: unchanged).

- Changed: Versions sits right under Simulate (it was below Output), with Save Playblast at its top.
- Changed: Save Playblast asks for a name first (playblasts of the same frames looked alike in the list). The playblast being recorded is selected from the start, and an Image Editor showing a playblast switches to the new one when it finishes, and to whichever one you select (it kept showing the previous movie).
- Added: Play on a playblast opens it in your video player, independent of Blender's timeline (playing the live preview no longer scrubs it); Image Editor shows it along the timeline as before. Export MP4… saves a copy where you choose and Open Folder shows where it is kept: playblasts are files in the domain's Versions folder, never inside the .blend.
- Changed: VDB versions are listed as "VDB · main" (a bare "main" read as the domain).

- Fixed: dense smoke and fireballs (the Explosion quick setup most of all) rendered fine concentric rings in Cycles, one per voxel layer, where their surface crossed the voxel grid. It was Cycles' linear reading of the volume, not the simulation: Fumaris's materials now read it with cubic interpolation (Look > Smooth Voxels, on by default; about 1.5 times the render time; turn it off for faster test renders). Existing bakes get it the next time their volume is imported or a Look setting changes.

- Fixed: a VRAM amount larger than the GPU's free memory put everything above it in system memory from the first frame, even for smoke that fit on the card: the first frame took seconds and every frame ran 5-10 times slower. Fumaris now starts with what fits on the GPU and goes to system memory only when the smoke needs more (40 GB set on a 16 GB card: first frame 0.3 s instead of 6.6 s, frames as fast as at 4 GB).
- Fixed: "GPU memory full" could stay hidden while the simulation ran from system memory: its count was reset each time the preview looped. It now shows for as long as any of the simulation is in system memory.
- Fixed: opening another file during Bake All (or Bake in Background) stopped the bakes with a console message only, and reopening the first file did not show the frames they had kept. The new file now says which bakes stopped, and reopening the first file loads their frames.
- Fixed: a very high Resolution (about 65536 and up with the default emitter) sat at frame 1 forever, the simulation process filling gigabytes of RAM: a mesh emitter listed every block of its padded surface before the GPU's capacity applied. The list now stops well above what any GPU holds, and the panel says the capacity is full (lower Resolution or raise VRAM). The warning under a VRAM amount above the GPU's size now fits the panel.
- Fixed: a domain copied from another file, or two files with same-named domains, could share one cache folder; each domain's cache is now its own.
- Fixed: undo during a live preview stopped it with "StructRNA of type Object has been removed"; the preview now carries on with the restored objects.
- Fixed: two or more background bakes on one GPU with Auto VRAM each claimed nearly the whole card and spilled into system memory; they now share its free memory.
- Fixed: the Auto VRAM line and the capacity warnings were cut off ("…") at Blender's default Properties width. They now use two short lines ("Auto VRAM: 9.0 GB" / "10.0 GB free, 1.0 GB left"; "Capacity reached" / "Smoke may clip: increase VRAM").
- Fixed: the narrow VRAM bar read "VRAM 0.0/40.0 GB" while the GPU was full. It now leads with the VRAM reserved and the share smoke uses of it ("VRAM 40.0 GB, 0% used").
- Fixed: the bake summary could end a line with a stray " |", and showed "N/A" for figures that were not measured. It now breaks between items and leaves unmeasured ones out; "1 frames" reads "1 frame".
- Fixed: a bake that was queued and then dropped showed "Stopped: 0 frames | N/A"; it now says "Stopped before the first frame".
- Fixed: sizes in GB were counted two ways (the preview cache and version sizes in thousands, Preferences, VRAM and RAM in 1024s), so a 16 GB cache limit showed as 17.2 GB. All now count GB the same way.
- Fixed: imported baked volumes were all named "Fumaris Volume", "Fumaris Volume.001", … and their names could swap between domains when one was baked again. Each is now named after its domain ("Smoke Domain Volume"), and baking again replaces that domain's own volume.
- Changed: Preferences list the GPUs as soon as they are opened, without pressing Refresh, and Automatic names the GPU it uses ("Automatic (NVIDIA GeForce RTX 3090 Ti)").
- Changed: an integrated GPU beside a discrete one is marked "(integrated, not recommended)" in the Simulation GPU list and is no longer offered for background bakes (a GPU already ticked stays). Automatic already chose the discrete GPU; a computer with only an integrated GPU can still use it.
- Changed: with no domain in the scene, the sidebar's buttons are named: New Domain and Quick Setup.

## 2.0.0 beta 3 — 2026-11-19

A fix release for the 2.0 beta, from a tester's reports. Windows users
should update: every simulation on Windows used much more GPU memory than
set. Windows results can differ slightly from beta 2: Windows now simulates
the same fields as Linux and macOS.

- Fixed (Windows): simulations used 1.6 to 2.2 times the VRAM that was set unless Per-Emitter Color was on, so Auto VRAM, and any amount near the card's size, ran out of GPU memory on the first frame ("GPU memory full", the simulation crawling). The setting that tells the solver which smoke fields a scene uses never reached its libraries on Windows, so they always stored all of them. Windows now stores only the fields a scene uses, as Linux and macOS do: the same VRAM holds 1.6 to 2.2 times the smoke, simulations run faster, and the Oxygen Field works on Windows (it was silently off).
- Fixed: a bake whose last frames took more than 30 seconds to write (high resolution, a hard disk; mostly Bake All and Bake in Background) never finished and lost its last frames: the panel stayed at "Frame 120 of 120, 0 s left" until Stop Now. Bakes now wait for the disk and show "Writing the last frames to disk…"; Stop in that phase ends the bake at once and keeps the frames already written; a simulation process that ends before the last frames are written is reported as an error instead of leaving the bake waiting.
- Fixed: Per-Emitter Color's smoke colours were wrong in the live preview (green and blue mixed up, changing with the shadows) and grey in renders (the imported volume's material read the colour grid as one number). Both now show the colours picked; the baked colour grid was already right, so existing bakes render in colour once the volume is imported again.
- Fixed (Windows): a background bake (Bake in Background, Bake All, Bake Selected) that finished could be reported as "ended unexpectedly (exit code 0)": its last status was lost when the main Blender was reading the status file at that moment.
- Fixed: Play on a domain without emitters showed a Python traceback; it now says what to add. Error messages no longer end with unrelated status lines (such as the Auto VRAM reading), and on macOS no longer carry MoltenVK's start-up banner, which also pushed the error out of the panel's log.
- Changed: the preview cache's lines say that its limit is system RAM ("Preview cache: frames 84–109 · 8.4 of 8.6 GB RAM"), and a full cache is no longer shown as a red error: the oldest frames are dropped and the simulation is not affected. The frames lost and what Save Sequence keeps are still flagged.
- Changed: Per-Emitter Color is named **Per-Emitter Smoke Color**, and says that it colours the smoke, not the flame (the flame's colour is set under Flame, for the whole domain). Its note when every emitter has the same colour says the option changes nothing yet. Files and scripts are unaffected (`color_mixing`).
- Changed: the Auto VRAM line shows the free GPU memory Auto was read from and what it left free ("Auto VRAM: 9.0 GB (10.0 GB free, 1.0 GB left free)"), and the VRAM bar's total stays the Auto amount. When the GPU memory is full under Auto, the panel advises closing other GPU programs, or turning Auto off and setting a lower VRAM, then pressing Play again (lowering VRAM did nothing under Auto). The narrow VRAM bar says VRAM.
- Changed: Bake in Background, Bake All and Bake Selected say in their tooltips that they run without a live preview, whatever Preview While Baking is set to.
- Changed: the Explosion quick setup drops its large Disturb (hot-smoke billows): it rolled the fireball into many small, separate soot balls; the lobes now stay a few big, soft billows with a fine crust of cells and fire in their seams, at the same size and timing and a slightly faster preview.
- Changed: Full-Resolution Channels' description and panel line give its real cost: about 3-4 times the memory per block and 1.5-4 times the time per frame (it said about twice the memory); at the same memory, half-resolution motion at about 1.5 times the Resolution looks as detailed. Detail & Shaping explains the smallest useful Disturb and Grain Size: about two motion cells, 128 / Resolution (64 / Resolution with Full-Resolution Channels).
- Changed: the Explosion quick setup follows footage of real fuel explosions (lessons of a large desert explosion shot): its width is set at once by a low, wide blast that rises later; fire turns to soot gradually, lean regions first, with flames lasting in pockets and at the base (shorter Burn Time; Explosion Clumps gain Fuel Low/High, Long Burn and Upward Spread inputs); the young fireball is hot from its first frame instead of starting as a dark puff; clumps write smoke and fuel as Maximum, so overlaps no longer stack into stepped layers; hot gas renders up to 10x denser for crisp, dark lobes with glowing seams; and a fine Disturb on fast gas only crisps the young fireball without churning the late smoke. Air Margin and Cooling Time now follow the explosion's size, so a large explosion no longer grows flat, block-shaped faces.
- Changed: the Steady Fire and Explosion quick setups render darker fuel soot (Smoke Color 0.28, Hot Soot 0.25, Thin Gas 0.08); Grain's speed mask follows the source size.

## 2.0.0 beta 2 — 2026-11-18

A large update to the 2.0 beta: faster everywhere, more simulation per GB,
background baking and a reorganised panel. Still a beta: please report scenes
that look wrong. Frames differ slightly from beta 1 (smoke is now conserved
as it moves, and the result no longer depends on the VRAM setting).

### Highlights

Measured on an RTX 3090 Ti against beta 1, back to back:

- Changed: Auto VRAM uses the GPU memory free at the start less a reserve of 6% of the GPU (12% for a bake, at least 1 GB) instead of 80% of it: about 22 GB on a free 24 GB card instead of 18.8 GB, still clear of running out of memory.
- Fixed: the simulation used more GPU memory than Simulation Memory: the block pool's estimate missed some of its textures and tables (the desert explosion set to 19 GB held 20.3 GB while previewing; now 19.5 GB, the rest being its emitters' data). The same setting now holds about 4% fewer blocks; results are unchanged.
- Fixed: bakes wrote the Pyro and glow grids full of zeros wherever smoke had no fire: Ground Fog caches are 42% smaller, Rising Smoke 29%, Steady Fire 21%, renders identical.
- Changed: the Pyro look always bakes Glow (its fire is the baked glow: with Glow off it rendered no fire at all); the Glow toggle shows as on for Pyro.
- Changed: Pyro look defaults from the explosions that looked best: Glow Heat 1500-2000 K, Full Glow 1700 K, Blackbody fire colour (the Ramp stays one click away). The Fire and Explosion quick setups use the Pyro look with dense, defined soot (Smoke Density 4.5, Sharp detail, Wisp Cooling 3, no Dissipation); Fire gains Disturb billows in hot gas and Grain where it moves fast, and its source emits almost no smoke so it glows instead of showing a dark ball.
- Fixed: the Explosion's fire clumps no longer fly into the ground and emit fire below the ground plane.
- Changed: the Explosion quick setup is a grand ground explosion: it balloons out wide and low on a ground collider instead of shooting up into a mushroom on a smoke stem (low buoyancy, slower Simulation Speed, fuel burnt in the blast), in the Pyro look with blackbody fire and lit soot. Explosion Clumps have a Lift input (how far up they are thrown).
- Fixed: renders of baked Pyro caches spent minutes of CPU time before sampling ("Computing Volume Mesh"): the Thin Gas reference grid covered the whole smoke box in empty space. Re-bake to benefit.
- Fixed: Pyro and Scatter Glow renders in Cycles took about 2.5 times longer than needed (3.7 times with unbiased volume sampling) and came out about 8% too dark: the coarse glow grids made Cycles march through large blocks of empty space around the smoke (where it ran out of steps), and shadow rays evaluated the whole fire shading. Bakes now write the fire's heat and glow as one grid (`pyro`) on the smoke's lattice, and shadow rays read only the smoke's density. Re-bake to benefit fully (renders get slightly brighter, as they should); older caches still render unchanged, about 1.2 times faster.
- Fixed: OpenVDB sequence emitters kept their GPU memory after their last frame, so bakes with many of them used several GB more VRAM than Simulation Memory.
- Fixed: very large bakes (over about 3.4 GB of smoke data read back a frame) lost the upper part of the smoke on alternate frames, or stopped. They now bake complete (a desert explosion of 900 M voxels a frame: all 100 frames).
- New: the generated material's Appearance values are inputs of one "Fumaris Look Controls" node at the left of the graph, so a material detached from Fumaris can be tuned by hand; the graph is laid out in three rows (Smoke, Fire, Scatter Glow). Existing materials are rebuilt at the next sync and keep their Colour Ramp. New **Hot Smoke Density** (From/To, off by default) thins or thickens smoke by its heat, and **Hot Soot** (how dark hot soot turns in the Pyro look; 1 = the look before) is an Appearance setting that survives a rebuild.
- Fixed: Stop no longer freezes Blender while the preview shuts down (2.1 s to 8 ms on a large scene).
- Fixed: the preview cache stays within the free GPU and host memory: large previews no longer spill into system memory or stutter while caching, and the first scrub through a large cached preview takes 2 s instead of 26. A preview resumed after showing cached frames continues exactly as it would have.
- Faster live preview drawing: 1.1 ms a frame instead of 12 at 1920×1080.
- **Bakes about 2.8 times faster**: 200 frames of the comparison explosion
  to VDB in 21 s instead of 59 (less read back from the GPU, multithreaded
  VDB writing, Pyro and Scatter Glow grids built faster).
- **Faster simulation and preview**: Explosion at Resolution 1024 simulates
  in 28.7 ms a frame instead of 38.4 (40.6 instead of 56.0 with the live
  preview); the comparison explosion previews at 15.8 frames a second
  instead of 11.4. Smoke Detail Sharp costs about 1.25x Standard instead of
  2.4x.
- **More simulation per GB**: air around the smoke no longer stores smoke
  fields and the simulated space hugs the smoke: 9% more blocks in the same
  VRAM, 42% fewer smoke cells on the same shot. A fire fits 1.6 times the
  blocks: its fuel and burn are stored at half resolution.
- **Bake in Background, Bake All and Bake Selected**: several domains bake at
  once in their own Blender processes, on one or more GPUs, while you work.
- **Per-Emitter Color**: emitters with their own smoke color that mixes where
  plumes meet, with its cost shown before it can be turned on.
- **A reorganised domain panel** in setup order, a smoother first run and many
  fixes from a hands-on usability test (renaming a domain no longer stops its
  bake or preview).

### All changes

- **Save Playblast** (Versions, and the Bake menu ▾): simulates the range as
  fast as it can and saves the viewport with the live smoke (overlays off, smoke
  hidden behind objects) as an MP4 plus its PNG frames, kept in Versions beside
  the snapshots with the settings it was made with (Preview plays it; View
  Changes, rename, pin, delete). Nothing is kept in the preview cache, so long
  or high-resolution shots work on any RAM. Esc stops it.
- **Eye toggle on each domain** (sidebar Domains list): hides the domain, its
  live preview and every volume imported from its bakes in the viewport, and
  shows them again; its bakes stay linked and editable. Renders are not affected.
- **Clearer when the preview cache runs out of RAM**: a red box names the frames
  that were dropped, says what Save Sequence will keep and how to keep more;
  Save Sequence warns when it can only save part of the range.
- **Pyro looks right at any scale (Thin Gas Auto)**: Thin Gas now follows each
  frame's own smoke (0.12 x its typical density, in the render and the live
  preview), so the glowing cells look alike whatever the Smoke Amount, scene
  size or time in the effect. Before, a fixed Thin Gas of 3 made thin scenes
  glow everywhere (blown-out white). Turn Auto off for a fixed Thin Gas. Pyro
  Brightness defaults to 1.5, close to the Temperature look's exposure.
- **Colour Ramp for fire**: the Golden colour option is now **Ramp**, a colour
  ramp you edit right in the panel (like Houdini's Pyro fire colour ramp; it
  starts as the old golden ramp). It colours the fire and the light it casts on
  the smoke (Scatter Glow), in the render and the live preview alike.
- **Sidebar**: the Objects list now comes before Add Objects.
- **Better-looking fire out of the box**: Oxygen Limit now defaults to 1, so
  fire burns where it mixes with air, temperatures vary and fireballs smoulder
  on in glowing pockets. The Explosion quick setup adds Clumps (two dozen fuel
  balls thrown out at random) for an uneven, lumpy fireball instead of a smooth
  ball, Disturb only in hot smoke for round billows, and a fine Grain layer.
- **Smooth timeline playback over a bake**: imported Fumaris volumes are hidden
  in the viewport while the timeline plays and shown again when it stops
  (Preferences > Hide Volumes While Playing, on by default).
- **Glow Source (Output)**: Scatter Glow can be lit by all warm gas
  (**Warm Gas**, as Houdini's pyro scatter: gas ramps in from Glow Heat From
  to To and is fully lit above) instead of only by fire brightness. Auto,
  the default, uses Warm Gas for the Pyro look, so the cooler cap of a
  rising fireball glows in the gaps between its billows instead of turning
  to grey smoke. Needs a re-bake. The live preview follows it, and its Pyro
  look no longer speckles at low Thin Gas (the soot masks are averaged along
  each ray step, as Cycles averages its samples).
- **Pyro look shows the cells**: Pyro now glows by the fire's light (the baked
  heat, blurred, plus part of each voxel's own heat) through the volume of
  thin gas, and fades the fire out of smoke denser than Thin Gas, voxel by
  voxel. Dense billow tops read as soot and round, popcorn-like cells glow
  between them, as in Houdini and Axiom explosions; before, dense hot smoke
  glowed as evenly as the gaps and fine billows rendered as smooth blobs. The
  preview matches. The fixed Thin Gas (Auto off) defaults to 3 (was 1). Scenes using Pyro
  render differently: cooling smoke glows less than before (no more glowing
  cracks late in an explosion's smoke); raise Thin Gas for dense, sooty sims.
  Pyro's Scatter Glow (fire light in the smoke) follows the same mask, so each
  billow gets a glowing skin instead of a haze over dense smoke.
- **Scatter Glow follows Contrast**: it falls off with the flame's Contrast,
  so a Full Glow below 1500 K no longer blows the smoke out to white (it was
  567 times too strong at 1100 K). Renders at the default Contrast and Full
  Glow are unchanged.
- **OpenVDB emitters**: Expansion now works on volume emitters (it was shown
  but did nothing), shaped by the file's density so a burst's core pushes
  hardest. Volume emitters also emit their dense cores: voxels with density
  above 1, common in Houdini and other tools' caches, emitted nothing before,
  while empty parts of the file's box slowed the flow and pulled heat toward
  zero. Scenes with such files simulate differently (now as their files
  describe).
- **Fire costs a third less memory**: a fire's fuel and burn are stored at
  half resolution, on the motion's grid; smoke and temperature stay at the
  Resolution you set. The same VRAM holds 1.6 times the blocks (15 GB on the
  comparison explosion: 195,128 instead of 122,880; VRAM in use at its peak
  1.87 instead of 2.95 GB) for 7 to 9% more frame time (Explosion at
  Resolution 1024: 34.8 ms a frame instead of 32.6, with Smoke Detail Sharp
  43.8 instead of 40.2).
  Flames keep their detail, size and edges: over 100 frames of a steady
  fire at Resolution 1024 the hot gas, its edges and its fine detail stay
  within 3% of full-resolution fuel. Heat stays within 2% for fires and 3 to
  4.5% for explosions at Resolution 768; at low Resolution explosions run a
  little cooler (Resolution 192: about 5%, 11% with Oxygen Limit) and so
  does a fire with the Oxygen Field (18% at 192, 2% at 384). Automatic;
  fuel and burn go back to full resolution while Full-Resolution Channels,
  the Fuel or Burn output or Per-Emitter Color is on. Frames of fire scenes
  differ slightly from before.
- **Full-Resolution Channels replaces Full-Resolution Velocity** (Advanced):
  one switch simulates everything at the full Resolution, motion and a
  fire's fuel and burn included, and still stores only the fields the scene
  uses. Smoke with full-resolution motion no longer stores temperature, fuel
  and burn it never uses: Rising Smoke fits 18% more blocks in the same
  memory, Fog 37% (same smoke, same rise). Scenes saved with Full-Resolution
  Velocity on open with Full-Resolution Channels on.
- **Temperature export is optional with the generated material**: it was
  always written when no custom material was set. Untick it (Output) for
  smoke that carries heat only to rise: the material then shows smoke only,
  and a hint says so when the scene has heat. On by default as before.
- **Smoke Detail Sharp no longer slower without fire**: with smoke alone, or
  smoke and heat, Sharp's first pass ran 22% slower than with fire (2.54
  against 2.09 ms at Rising Smoke 1024); it now takes the same. Such scenes
  bake slightly different frames with Sharp.
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
- **Smoke Detail Sharp takes about half the time**: its cubic smoke
  transport read 64 cells per voxel, each looked up in the sparse grid. It now
  takes seven filtered samples (the cubic correction along each axis), found
  through the block as Standard's are, and shares its overshoot check with
  the correction step (Explosion at Resolution 1024: 52 to 28 ms a frame on
  an RTX 3090 Ti, about 1.25x Standard instead of 2.4x; in a live preview
  Sharp now costs roughly 3 to 14% of the frame rate). Sharp's result changes
  slightly: it keeps about 95% of the fine detail it had, still far above
  Standard. Standard is unchanged, byte for byte.
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
- Deleting a preview's only emitter no longer stops the preview: nothing
  emits until Undo brings the emitter back, into the running preview.
- An output folder that can't be written is reported by name ("Can't write
  to ... Choose another Output Directory") instead of a raw "[Errno 2]" for
  a cache subfolder.
- The sidebar's object list is headed "Objects": a long domain name cut the
  header off (the domain is named in the controls above it).
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
  to Domain), which uses the GPU memory free at the start (see Highlights).
  Since the result no longer depends on the amount, this only gives the
  smoke more room; turn Auto off to leave memory to other programs. Domains in existing
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
  Directory and Prefix fields show their defaults; Density shows ticked and
  greyed when the generated material needs it. Advanced and
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
