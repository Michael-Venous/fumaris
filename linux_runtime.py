"""Select GCC runtime libraries for the isolated Linux bridge process.

Keep Flow/Slang private, but allow newer host Vulkan drivers to use their
native C++ ABI. Do not load these libraries into Blender itself.

The second half of this module explains bridge start-up failures (missing
libraries, too-old glibc/libstdc++, no Vulkan driver) without executing any
inspected file; see runtime_diagnostics() and explain_launch_failure().
"""

import functools
import json
import os
import platform
import re
import struct
import subprocess
import sys


def _ldconfig_libraries():
    """Map each x86-64 soname in the system loader cache to its path.

    ldconfig's cache reflects the system's multiarch layout. Never search
    Blender/Conda's PATH or LD_LIBRARY_PATH for the native runtime.
    """
    for command in ("/sbin/ldconfig", "/usr/sbin/ldconfig", "/usr/bin/ldconfig"):
        if not os.path.isfile(command):
            continue
        try:
            clean = os.environ.copy()
            clean.pop("LD_LIBRARY_PATH", None)
            clean.pop("LD_PRELOAD", None)
            result = subprocess.run([command, "-p"], env=clean, capture_output=True,
                                    text=True, timeout=5, check=True)
        except (OSError, subprocess.SubprocessError):
            continue
        found = {}
        for line in result.stdout.splitlines():
            fields = line.strip().split()
            if fields and "x86-64" in line and "=>" in line:
                path = line.split("=>", 1)[1].strip()
                if os.path.isabs(path):
                    found.setdefault(fields[0], path)
        return found
    return {}


def _system_gcc_libraries():
    cache = _ldconfig_libraries()
    found = tuple(os.path.realpath(cache[name]) for name in ("libstdc++.so.6", "libgcc_s.so.1")
                  if name in cache and os.path.isfile(cache[name]))
    return found if len(found) == 2 else ()


def _stamp(path):
    stat = os.stat(path)
    return (path, stat.st_mtime_ns, stat.st_size)


@functools.lru_cache(maxsize=8)
def _native_runtime_works(executable_stamp, library_stamps, environment):
    try:
        result = subprocess.run([executable_stamp[0], "--check-runtime"],
                                env=dict(environment), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def prefer_system_gcc(executable, env):
    """Return a worker-only environment; retain bundled fallback on older hosts.

    Preload only the two GCC runtimes, rather than putting all system libraries
    ahead of app-private dependencies. A bridge probe checks native compatibility
    before selection. Cache invalidates on binary/library/environment changes.
    """
    libraries = _system_gcc_libraries()
    if not libraries or not os.path.isfile(executable):
        return env
    candidate = env.copy()
    candidate["LD_PRELOAD"] = " ".join((*libraries, env.get("LD_PRELOAD", ""))).strip()
    try:
        stamps = tuple(_stamp(path) for path in libraries)
        # Include private libraries: a development update may raise their ABI floor.
        private = os.path.join(os.path.dirname(executable), "libs")
        stamps += tuple(_stamp(os.path.join(private, name))
                        for name in sorted(os.listdir(private)) if ".so" in name)
        if _native_runtime_works(_stamp(executable), stamps, tuple(sorted(candidate.items()))):
            return candidate
    except OSError:
        pass
    return env


# ---------------------------------------------------------------------------
# Loader diagnostics
#
# ELF files are parsed as data only; nothing inspected here is executed. Every
# public entry point is fail-open: it returns a neutral result instead of
# raising, and is only used to annotate messages, never to refuse a launch.
# ---------------------------------------------------------------------------

_PT_INTERP = 3
_SHT_DYNAMIC = 6
_SHT_GNU_VERDEF = 0x6FFFFFFD
_SHT_GNU_VERNEED = 0x6FFFFFFE
_DT_NEEDED, _DT_SONAME, _DT_RPATH, _DT_RUNPATH = 1, 14, 15, 29
_EM_X86_64 = 62
_EM_NAMES = {3: "x86", 40: "ARM", 62: "x86-64", 183: "AArch64", 243: "RISC-V"}
_MAX_SECTION = 32 << 20
_MAX_OBJECTS = 256
_REQUIRED_BUNDLED = ("libnvflow.so", "libnvflowext.so")
_GLIBC_LIBRARIES = frozenset((
    "libc.so.6", "libm.so.6", "libpthread.so.0", "libdl.so.2", "librt.so.1",
    "libresolv.so.2", "libutil.so.1", "ld-linux-x86-64.so.2",
))
_DEFAULT_DIRECTORIES = (
    "/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu", "/lib64",
    "/usr/lib64", "/lib", "/usr/lib",
)
_PACKAGE_HINTS = {
    "libvulkan.so.1": "the Vulkan loader (Debian/Ubuntu: libvulkan1, Fedora: vulkan-loader, Arch: vulkan-icd-loader)",
    "libstdc++.so.6": "the GCC C++ runtime (Debian/Ubuntu: libstdc++6, Fedora: libstdc++, Arch: gcc-libs)",
    "libgcc_s.so.1": "the GCC runtime (Debian/Ubuntu: libgcc-s1, Fedora: libgcc, Arch: gcc-libs)",
    "libX11.so.6": "Xlib (Debian/Ubuntu: libx11-6, Fedora: libX11, Arch: libx11)",
}
_SOFTWARE_VULKAN = ("lvp", "lavapipe", "swiftshader")


class ElfInfo:
    __slots__ = ("machine", "elf_class", "interpreter", "soname", "needed",
                 "rpath", "runpath", "requires", "provides")

    def __init__(self, machine, elf_class):
        self.machine = machine
        self.elf_class = elf_class
        self.interpreter = ""
        self.soname = ""
        self.needed = ()
        self.rpath = ()
        self.runpath = ()
        self.requires = {}          # needed file name -> frozenset of versions
        self.provides = frozenset()  # version definitions (GLIBCXX_3.4.30, ...)


def read_elf(path, header_only=False):
    """Parse an ELF file's dynamic-linking data; None if it is not valid ELF."""
    try:
        with open(path, "rb") as stream:
            return _parse_elf(stream, os.fstat(stream.fileno()).st_size, header_only)
    except (OSError, ValueError, struct.error, UnicodeDecodeError):
        return None


def _parse_elf(stream, size, header_only):
    ident = stream.read(16)
    if len(ident) < 16 or ident[:4] != b"\x7fELF" or ident[4] not in (1, 2) or ident[5] not in (1, 2):
        return None
    wide = ident[4] == 2
    order = "<" if ident[5] == 1 else ">"
    fields = struct.unpack(order + ("HHIQQQIHHHHHH" if wide else "HHIIIIIHHHHHH"),
                           stream.read(48 if wide else 36))
    machine, phoff, shoff = fields[1], fields[4], fields[5]
    phentsize, phnum, shentsize, shnum = fields[8], fields[9], fields[10], fields[11]
    info = ElfInfo(machine, 64 if wide else 32)
    if header_only:
        return info

    def read(offset, length):
        if offset < 0 or length < 0 or length > _MAX_SECTION or offset + length > size:
            raise ValueError("ELF range outside file")
        stream.seek(offset)
        return stream.read(length)

    program = order + ("IIQQQQQQ" if wide else "IIIIIIII")
    if phoff and phentsize >= struct.calcsize(program):
        for index in range(min(phnum, 512)):
            entry = struct.unpack(program, read(phoff + index * phentsize, struct.calcsize(program)))
            if entry[0] == _PT_INTERP:
                offset, length = (entry[2], entry[5]) if wide else (entry[1], entry[4])
                info.interpreter = read(offset, min(length, 4096)).split(b"\0", 1)[0].decode()

    section = order + ("IIQQQQIIQQ" if wide else "IIIIIIIIII")
    if not shoff or shentsize < struct.calcsize(section):
        return info
    # (name, type, flags, addr, offset, size, link, info, align, entsize)
    sections = [struct.unpack(section, read(shoff + i * shentsize, struct.calcsize(section)))
                for i in range(min(shnum, 4096))]
    tables = {}

    def strings(index):
        if index not in tables:
            if not 0 <= index < len(sections):
                raise ValueError("ELF string table index")
            tables[index] = read(sections[index][4], sections[index][5])
        return tables[index]

    def text(table, offset):
        end = table.find(b"\0", offset)
        if offset >= len(table) or end < 0:
            raise ValueError("ELF string offset")
        return table[offset:end].decode()

    for entry in sections:
        kind, offset, length, link, count = entry[1], entry[4], entry[5], entry[6], entry[7]
        if kind == _SHT_DYNAMIC:
            table, data = strings(link), read(offset, length)
            step = 16 if wide else 8
            needed = []
            for position in range(0, len(data) - step + 1, step):
                tag, value = struct.unpack(order + ("qQ" if wide else "iI"), data[position:position + step])
                if tag == 0:
                    break
                if tag == _DT_NEEDED:
                    needed.append(text(table, value))
                elif tag == _DT_SONAME:
                    info.soname = text(table, value)
                elif tag == _DT_RPATH:
                    info.rpath = tuple(p for p in text(table, value).split(":") if p)
                elif tag == _DT_RUNPATH:
                    info.runpath = tuple(p for p in text(table, value).split(":") if p)
            info.needed = tuple(needed)
        elif kind == _SHT_GNU_VERNEED:
            table, data = strings(link), read(offset, length)
            requires, position = {}, 0
            for _ in range(min(count or 4096, 4096)):
                _, aux_count, name, aux, following = struct.unpack(order + "HHIII", data[position:position + 16])
                versions, cursor = set(), position + aux
                for _ in range(min(aux_count, 4096)):
                    _, _, _, version, aux_next = struct.unpack(order + "IHHII", data[cursor:cursor + 16])
                    versions.add(text(table, version))
                    if not aux_next:
                        break
                    cursor += aux_next
                library = text(table, name)
                requires[library] = requires.get(library, frozenset()) | versions
                if not following:
                    break
                position += following
            info.requires = requires
        elif kind == _SHT_GNU_VERDEF:
            table, data = strings(link), read(offset, length)
            provides, position = set(), 0
            for _ in range(min(count or 65536, 65536)):
                _, flags, _, aux_count, _, aux, following = struct.unpack(
                    order + "HHHHIII", data[position:position + 20])
                if aux_count and not flags & 1:  # VER_FLG_BASE names the file itself
                    name = struct.unpack(order + "I", data[position + aux:position + aux + 4])[0]
                    provides.add(text(table, name))
                if not following:
                    break
                position += following
            info.provides = frozenset(provides)
    return info


def _version_key(name):
    match = re.search(r"_(\d+(?:\.\d+)*)$", name)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def _dotted(version):
    return ".".join(map(str, version))


def _glibc_version():
    try:
        value = os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, ValueError, OSError):
        value = ""
    match = re.match(r"glibc (\d+(?:\.\d+)*)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


@functools.lru_cache(maxsize=1024)
def _cached_elf(stamp):
    return read_elf(stamp[0])


def _elf(path):
    try:
        return _cached_elf(_stamp(path))
    except (OSError, TypeError):
        return None


def _usable(path):
    header = read_elf(path, header_only=True)
    return header is None or header.machine == _EM_X86_64


class _Resolver:
    """Approximate the ld.so(8) search order for one bridge environment."""

    def __init__(self, env, system):
        self.system = system
        self.library_path = tuple(p for p in env.get("LD_LIBRARY_PATH", "").split(":") if p)
        self.preloaded = {}
        for path in env.get("LD_PRELOAD", "").replace(":", " ").split():
            info = _elf(path) if os.path.isabs(path) else None
            if info is not None:
                self.preloaded.setdefault(info.soname or os.path.basename(path), os.path.realpath(path))

    def system_path(self, name):
        path = self.system.get(name)
        if path and os.path.isfile(path):
            return os.path.realpath(path)
        for directory in _DEFAULT_DIRECTORIES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate) and _usable(candidate):
                return os.path.realpath(candidate)
        return None

    def find(self, name, requester, info):
        if name in self.preloaded:
            return self.preloaded[name]
        if "/" in name:
            return os.path.realpath(name) if os.path.isfile(name) else None
        origin = os.path.dirname(requester)
        searches = (() if info.runpath else info.rpath) + self.library_path + info.runpath
        for directory in searches:
            directory = directory.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)
            candidate = os.path.join(directory or ".", name)
            if os.path.isfile(candidate) and _usable(candidate):
                return os.path.realpath(candidate)
        return self.system_path(name)


def _vulkan_manifests(env):
    """Return (source, ICD manifest paths) following the Vulkan loader rules."""
    override = env.get("VK_DRIVER_FILES") or env.get("VK_ICD_FILENAMES")
    if override:
        entries = [p for p in override.split(":") if p]
        source = "VK_DRIVER_FILES" if env.get("VK_DRIVER_FILES") else "VK_ICD_FILENAMES"
    else:
        home = env.get("HOME") or os.path.expanduser("~")
        config = env.get("XDG_CONFIG_DIRS") or "/etc/xdg"
        data_home = env.get("XDG_DATA_HOME") or os.path.join(home, ".local/share")
        data = env.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
        entries = [os.path.join(p, "vulkan/icd.d") for p in config.split(":") if p]
        entries += ["/etc/vulkan/icd.d", os.path.join(data_home, "vulkan/icd.d")]
        entries += [os.path.join(p, "vulkan/icd.d") for p in data.split(":") if p]
        source = "system"
    entries += [p for p in env.get("VK_ADD_DRIVER_FILES", "").split(":") if p]
    manifests, seen = [], set()
    for entry in entries:
        if os.path.isdir(entry):
            try:
                found = [os.path.join(entry, n) for n in sorted(os.listdir(entry)) if n.endswith(".json")]
            except OSError:
                found = []
        else:
            found = [entry] if os.path.isfile(entry) else []
        for path in found:
            if os.path.realpath(path) not in seen:
                seen.add(os.path.realpath(path))
                manifests.append(path)
    return source, manifests


def _vulkan_drivers(env, resolver):
    """Return (usable driver names, broken driver descriptions, source)."""
    source, manifests = _vulkan_manifests(env)
    usable, broken = [], []
    for path in manifests:
        name = os.path.basename(path)[:-5] if path.endswith(".json") else os.path.basename(path)
        try:
            if os.path.getsize(path) > 65536:
                raise ValueError("manifest too large")
            with open(path, encoding="utf-8") as stream:
                library = json.load(stream)["ICD"]["library_path"]
            if not isinstance(library, str) or not library:
                raise ValueError("no library_path")
        except (OSError, ValueError, KeyError, TypeError):
            broken.append(f"{name} (unreadable manifest)")
            continue
        if "/" in library:
            candidate = library if os.path.isabs(library) else os.path.join(os.path.dirname(path), library)
            found = candidate if os.path.isfile(candidate) else None
        else:
            found = resolver.system_path(library)
        if found is None:
            broken.append(f"{name} (missing {library})")
            continue
        header = read_elf(found, header_only=True)
        if header is not None and header.machine != _EM_X86_64:
            continue  # 32-bit multilib manifests are normal and irrelevant here
        usable.append(name)
    return usable, broken, source


def _diagnose(executable, env):
    problems, warnings, notes = [], [], []
    machine = platform.machine().lower()
    if machine not in ("x86_64", "amd64"):
        problems.append(f"This Fumaris build is for x86-64 Linux, but this system is {machine or 'unknown'}.")
    glibc = _glibc_version()
    if glibc is None:
        problems.append("No GNU C library (glibc) detected; Fumaris needs a glibc-based "
                        "distribution (musl systems such as Alpine are not supported).")
    else:
        notes.append("glibc " + _dotted(glibc))

    executable = os.path.realpath(executable)
    bridge = _elf(executable)
    if bridge is None:
        problems.append(f"The Fumaris bridge is not a readable Linux executable: {executable}")
        return problems, warnings, notes
    if bridge.machine != _EM_X86_64:
        problems.append("The Fumaris bridge is built for {}, not x86-64.".format(
            _EM_NAMES.get(bridge.machine, f"machine {bridge.machine}")))
    if bridge.interpreter and not os.path.exists(bridge.interpreter):
        problems.append(f"The system dynamic loader {bridge.interpreter} is missing; this "
                        "distribution cannot run standard x86-64 Linux programs.")

    resolver = _Resolver(env, _ldconfig_libraries())
    libs = os.path.join(os.path.dirname(executable), "libs")
    try:
        bundled = sorted((n for n in os.listdir(libs) if ".so" in n),
                         key=lambda n: (n not in _REQUIRED_BUNDLED, n))
    except OSError:
        bundled = []
    # The bridge goes first (it decides which libstdc++ wins), then Flow, then
    # extras that are only reached through dlopen. Gaps there are warnings.
    pending = [(executable, True)]
    pending += [(os.path.realpath(os.path.join(libs, n)), n in _REQUIRED_BUNDLED) for n in bundled]
    visited, selected = {}, {}
    missing = {}       # soname -> [requesters, essential]
    gaps = {}          # (soname, path) -> [versions, requesters, essential]
    while pending and len(visited) < _MAX_OBJECTS:
        path, essential = pending.pop(0)
        if path in visited and (visited[path] or not essential):
            continue
        visited[path] = essential
        info = _elf(path)
        if info is None:
            continue
        for name in info.needed:
            if name not in selected:
                selected[name] = resolver.find(name, path, info)
            target = selected[name]
            if target:
                pending.append((target, essential))
            else:
                entry = missing.setdefault(name, [set(), False])
                entry[0].add(os.path.basename(path))
                entry[1] = entry[1] or essential
        for name, versions in info.requires.items():
            target = selected.get(name)
            provider = _elf(target) if target else None
            if provider is None or not provider.provides:
                continue
            absent = versions - provider.provides
            if absent:
                entry = gaps.setdefault((name, target), [set(), set(), False])
                entry[0] |= absent
                entry[1].add(os.path.basename(path))
                entry[2] = entry[2] or essential

    for name, (requesters, essential) in sorted(missing.items()):
        if essential:
            hint = _PACKAGE_HINTS.get(name, "the distribution package that provides it")
            problems.append(f"Missing system library {name} (needed by {', '.join(sorted(requesters))}); install {hint}.")
        else:
            warnings.append(f"Optional bundled {', '.join(sorted(requesters))} cannot find {name}.")

    needed_glibc = max((_version_key(version)
                        for path, essential in visited.items() if essential
                        for library, versions in getattr(_elf(path), "requires", {}).items()
                        if library in _GLIBC_LIBRARIES
                        for version in versions if version.startswith("GLIBC_")), default=())
    if needed_glibc and glibc and glibc < needed_glibc:
        problems.append(f"glibc {_dotted(glibc)} is too old: Fumaris needs glibc {_dotted(needed_glibc)} "
                        "or newer. Update the distribution (for example Ubuntu 22.04, Debian 12, "
                        "Fedora 35 or RHEL 9 or newer).")
    for (name, target), (versions, requesters, essential) in sorted(gaps.items()):
        if name in _GLIBC_LIBRARIES and glibc:
            continue  # summarised above from the authoritative glibc version
        newest = max(versions, key=_version_key)
        line = f"{target} is too old: it lacks {newest} needed by {', '.join(sorted(requesters))}."
        if name in ("libstdc++.so.6", "libgcc_s.so.1"):
            line += (" Update the GCC runtime, or remove LD_PRELOAD/LD_LIBRARY_PATH overrides "
                     "(Conda, Steam or launcher wrappers) that select it.")
        (problems if essential else warnings).append(line)

    stdcpp = selected.get("libstdc++.so.6")
    if stdcpp:
        origin = "bundled" if os.path.dirname(stdcpp) == os.path.realpath(libs) else "system"
        provider = _elf(stdcpp)
        newest = max((v for v in provider.provides if v.startswith("GLIBCXX_")),
                     key=_version_key, default="") if provider else ""
        notes.append(f"libstdc++ {origin} {newest} ({stdcpp})".replace("  ", " "))

    if not resolver.find("libvulkan.so.1", executable, bridge):
        problems.append("Missing system library libvulkan.so.1; install " + _PACKAGE_HINTS["libvulkan.so.1"] + ".")
    usable, broken, source = _vulkan_drivers(env, resolver)
    if usable:
        notes.append("Vulkan drivers: " + ", ".join(usable) + ("" if source == "system" else f" (from {source})"))
        if all(any(tag in n.lower() for tag in _SOFTWARE_VULKAN) for n in usable):
            warnings.append("Only a CPU software Vulkan driver was found; install your GPU's Vulkan driver.")
    else:
        detail = f" (broken: {', '.join(broken)})" if broken else ""
        override = "" if source == "system" else f" {source} is set and may hide installed drivers."
        problems.append("No Vulkan GPU driver (ICD) found" + detail + ". Install the Vulkan driver for "
                        "your GPU (NVIDIA proprietary driver; Mesa: vulkan-intel/vulkan-radeon or "
                        "mesa-vulkan-drivers)." + override)
    if os.path.exists("/.flatpak-info") or os.environ.get("SNAP"):
        notes.append("Blender runs in a Flatpak/Snap sandbox, which can hide system GPU drivers")
    inherited = [name for name in ("LD_PRELOAD", "LD_LIBRARY_PATH") if os.environ.get(name)]
    if inherited:
        notes.append("inherited " + "/".join(inherited) + " from Blender's launcher")
    return problems, warnings, notes


_ENVIRONMENT_KEYS = (
    "LD_PRELOAD", "LD_LIBRARY_PATH", "VK_DRIVER_FILES", "VK_ICD_FILENAMES",
    "VK_ADD_DRIVER_FILES", "XDG_CONFIG_DIRS", "XDG_DATA_DIRS", "XDG_DATA_HOME", "HOME",
)


def _summary(problems, warnings, notes):
    if problems:
        text = "Linux runtime problem: " + " ".join(problems)
    elif warnings:
        text = "Linux runtime OK with warnings: " + " ".join(warnings)
    else:
        text = "Linux runtime OK"
    return text + (" [" + "; ".join(notes) + "]" if notes else "")


@functools.lru_cache(maxsize=8)
def _cached_diagnostics(executable_stamp, library_stamps, environment):
    problems, warnings, notes = _diagnose(executable_stamp[0], dict(environment))
    return (tuple(problems), tuple(warnings), tuple(notes))


def runtime_diagnostics(executable, env=None):
    """Return a cached loader report for the bridge, or None off Linux.

    Keys: ok (bool), problems / warnings / notes (lists of sentences) and
    summary (one compact line for logs and the Diagnostics panel). Cached
    until the bridge, a bundled library or a loader variable changes. Never
    raises; an internal error yields ok=True with an explanatory note.
    """
    if not sys.platform.startswith("linux"):
        return None
    env = os.environ if env is None else env
    try:
        libs = os.path.join(os.path.dirname(executable), "libs")
        try:
            names = sorted(n for n in os.listdir(libs) if ".so" in n)
        except OSError:
            names = []
        stamps = tuple(_stamp(os.path.join(libs, n)) for n in names)
        environment = tuple((key, env.get(key, "")) for key in _ENVIRONMENT_KEYS)
        problems, warnings, notes = _cached_diagnostics(_stamp(executable), stamps, environment)
    except Exception as error:  # diagnostics must never break a launch
        problems, warnings, notes = (), (), (f"runtime diagnostics unavailable: {type(error).__name__}: {error}",)
    return {
        "ok": not problems,
        "problems": list(problems),
        "warnings": list(warnings),
        "notes": list(notes),
        "summary": _summary(problems, warnings, notes),
    }


_MISSING_LIBRARY = re.compile(r"error while loading shared libraries: ([^:\s]+): cannot open shared object file")
_MISSING_VERSION = re.compile(r"(?:(\S+): )?version [`']([^']+)' not found \(required by ([^)]+)\)")
_MISSING_SYMBOL = re.compile(r"symbol lookup error: ([^:]+): undefined symbol: (\S+)")


def loader_error_hints(text):
    """Translate ld.so error text (stderr or an exception) into sentences."""
    text = text or ""
    hints = []
    for name in _MISSING_LIBRARY.findall(text):
        hint = _PACKAGE_HINTS.get(name, "the distribution package that provides it")
        hints.append(f"The system is missing {name}; install {hint}.")
    for library, version, requester in _MISSING_VERSION.findall(text):
        requester = os.path.basename(requester.strip())
        where = f" ({library})" if library else ""
        if version.startswith("GLIBC_"):
            hints.append(f"glibc is too old: {requester} needs {version}. Update the distribution.")
        elif version.startswith(("GLIBCXX_", "CXXABI_", "GCC_")):
            hints.append(f"The loaded C++ runtime{where} is too old: {requester} needs {version}. "
                         "Update the GCC runtime, or remove LD_PRELOAD/LD_LIBRARY_PATH overrides "
                         "(Conda, Steam or launcher wrappers) that select an older libstdc++.")
        else:
            hints.append(f"A system library{where} lacks {version} needed by {requester}.")
    for library, symbol in _MISSING_SYMBOL.findall(text):
        hints.append(f"{os.path.basename(library)} could not find symbol {symbol}; a preloaded or "
                     "outdated system library is probably taking precedence.")
    if "Exec format error" in text:
        hints.append("The bridge cannot run on this CPU architecture; Fumaris for Linux needs x86-64.")
    return list(dict.fromkeys(hints))


def explain_launch_failure(executable, env, message, stderr=""):
    """Append loader hints and the runtime report to a bridge failure message.

    Returns the message unchanged off Linux, when nothing useful is found, or
    if anything goes wrong.
    """
    try:
        if not sys.platform.startswith("linux") or not executable:
            return message
        lines = loader_error_hints(f"{message}\n{stderr}")
        report = runtime_diagnostics(executable, env)
        if report:
            lines += [line for line in report["problems"] if line not in lines]
        if not lines:
            return message
        details = " ".join(lines)
        if report and report["notes"]:
            details += " [" + "; ".join(report["notes"]) + "]"
        return f"{message}\nLinux runtime check: {details}"
    except Exception:
        return message
