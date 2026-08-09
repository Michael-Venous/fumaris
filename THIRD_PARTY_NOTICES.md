# Third-Party Notices

Fumaris includes or dynamically uses third-party software. Each component
remains subject to its own license; no Fumaris license overrides those
terms.

The packaged runtime may include:

| Component | License |
| --- | --- |
| NVIDIA Flow / PhysX distribution | BSD-3-Clause |
| Slang shader compiler | Apache-2.0 WITH LLVM-exception |
| glslang used by Slang | BSD, MIT, Apache-2.0, and GPL-3.0 with Bison exception |
| miniz used by Slang | MIT |
| SPIR-V Headers used by Slang | Modified MIT |
| SPIR-V Tools used by Slang | Apache-2.0 |
| ankerl::unordered_dense used by Slang | MIT |
| OpenVDB | MPL-2.0 |
| oneTBB | Apache-2.0 |
| Imath | BSD-3-Clause |
| Boost | BSL-1.0 |
| Blosc | BSD-3-Clause |
| GLFW | Zlib |
| ICU | ICU |
| log4cplus | Apache-2.0 |
| LZ4 | BSD-2-Clause |
| Snappy | BSD-3-Clause |
| zlib / zlib-ng compatibility library | Zlib |
| Zstandard | BSD-3-Clause or GPL-2.0-only |
| bzip2 | bzip2-1.0.6 |
| liblzma from XZ Utils | 0BSD |
| GCC runtime libraries | GPL with GCC Runtime Library Exception |

Release engineering must include complete applicable license texts for every
binary shipped by a particular platform package before public sale.

Project home pages:

- https://github.com/NVIDIA-Omniverse/PhysX
- https://github.com/shader-slang/slang
- https://github.com/KhronosGroup/glslang
- https://github.com/richgel999/miniz
- https://github.com/KhronosGroup/SPIRV-Headers
- https://github.com/KhronosGroup/SPIRV-Tools
- https://github.com/martinus/unordered_dense
- https://github.com/AcademySoftwareFoundation/openvdb
- https://github.com/uxlfoundation/oneTBB
- https://github.com/AcademySoftwareFoundation/Imath
- https://www.boost.org/
- https://github.com/Blosc/c-blosc
- https://www.glfw.org/
- https://icu.unicode.org/
- https://github.com/log4cplus/log4cplus
- https://github.com/lz4/lz4
- https://github.com/google/snappy
- https://github.com/madler/zlib
- https://github.com/facebook/zstd

Applicable license texts are included in the `licenses` directory. The exact
set of native libraries differs by platform package.
