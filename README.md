# Fumaris Blender Integration

Current release: **0.2.0 Beta**.

This repository contains the GPL-licensed Blender integration layer for
Fumaris, an interactive GPU smoke and fire simulator.

## Source Boundary

The files tracked by this public repository integrate with Blender's Python API
and are licensed under GPL-3.0-or-later. Customer packages combine this source
with a separately built simulation bridge, NVIDIA Flow runtime, and other
native dependencies for the selected operating system.

This is the add-on source root used by Blender extensions. The repository alone
is not a complete runnable product; customers receive a matching Linux or
Windows package from the authorized storefront.

## License

The Python integration is free software under GPL-3.0-or-later. You may use,
modify, and redistribute it under that license. See `LICENSE`.

Customer packages also contain a separately licensed native runtime and
third-party libraries. See `LICENSE_OVERVIEW.md`,
`COMMERCIAL_RUNTIME_LICENSE.txt`, and `THIRD_PARTY_NOTICES.md`.

Third-party names and marks belong to their respective owners. Fumaris is
not affiliated with or endorsed by Blender Foundation or NVIDIA.

## Documentation

- `DOCUMENTATION.md`: installation, workflow, settings, shading, performance,
  troubleshooting, and beta limitations
- `CHANGELOG.md`: customer-visible changes by version

## Support

Use the support channel listed on the product page and include the Fumaris
version, Blender version, operating system, GPU, driver version, console log,
and a minimal reproduction file.
