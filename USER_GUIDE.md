# Fumaris Beta User Guide

## Core Workflow

1. Add an object that bounds the simulation and set `Flow Object` to `Domain`.
2. Create an emitter collection and assign it in the domain's `Participants`
   section.
3. Add emitter objects to that collection. Set each object's `Flow Object` to
   `Emitter`, choose its source geometry, and set emitted channels.
4. Select the domain and press `Play` for interactive feedback.
5. Adjust emitters, effectors, and domain behavior while previewing.
6. Stop preview, choose an output directory and cache slot, then press `Bake`
   to create an OpenVDB sequence.

The first simulation on a new GPU or driver may spend several minutes compiling
shaders. Blender remains responsive and shows an initialization notice.

## Preview

Preview runs one persistent Flow session and advances the timeline only after
each simulated frame is ready. It loops over the domain's simulation frame
range and is independent of Blender's timeline start and end.

Preview dots are a sampled representation of density, not the final volume.
Use Preview Resolution and dot controls to balance responsiveness and detail.
Preview does not write a durable cache.

## Baking And Cache Slots

Bake simulates frames sequentially and writes OpenVDB files. Enabled output
channels increase readback, conversion, disk use, and bake time. Density is
always available; enable only the extra grids needed by the material.

Cache slots keep separate bakes in the same output root. Give important
versions descriptive slot names. `Delete` removes the selected slot's generated
volume and cache files.

Stopping a bake imports all valid frames already written. A stopped Flow state
cannot be resumed in this beta, so another Bake starts a fresh simulation.

## Emitters

Supported sources include:

- Mesh, sphere, and box emitters
- Blender particle systems
- Geometry Nodes points and meshes
- OpenVDB files
- Experimental evaluated Geometry Nodes volumes

For Geometry Nodes points, map named attributes for position, radius, velocity,
smoke, temperature, fuel, burn, and divergence. Missing optional attributes use
the emitter's object-level values. Empty point outputs are valid.

Collections are traversed recursively. Objects whose `Flow Object` role does
not match the assigned collection role are skipped.

## Colliders, Effectors, And Outflows

Assign optional collider, effector, and outflow collections on the domain.
Collider velocity influence transfers collider motion into smoke. Effectors
support force settings, noise, and distance falloff. Outflows remove channels
inside their region.

## Resolution And Memory

Resolution controls the domain grid. Sparse block capacity limits how much of
that grid may become active. High resolution, combustion channels, exact point
spheres, velocity output, and large preview-dot limits all increase memory use.

Leave `Boundary-Safe Advection` and `Allocate Neighbor Blocks` enabled for fast
smoke. Sub-Steps are complete solver steps from 1 to 20: use 1 for moderate
motion, start at 2 for energetic pyro, and increase it for extreme velocities,
combustion, or high-resolution detail. Higher values increase simulation time
roughly linearly.
`Small Sparse Blocks` is faster and more memory efficient, while disabling it
can improve continuity in demanding simulations at a substantial cost.

If a simulation fails or the system becomes unstable:

1. Reduce resolution.
2. Reduce sparse block capacity.
3. Disable unused VDB channels.
4. Reduce Preview Resolution and Preview Max Dots.
5. Confirm the GPU driver is current.

## Reporting A Problem

Include the Fumaris version, Blender version, operating system, GPU, driver,
console output, exact steps, and the smallest `.blend` that reproduces the
issue. State whether it occurs in Preview, Bake, or both.

Geometry Nodes Volume emission is experimental. Mention that source type
explicitly in reports.
