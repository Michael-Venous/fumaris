"""Select GCC runtime libraries for the isolated Linux bridge process.

Keep Flow/Slang private, but allow newer host Vulkan drivers to use their
native C++ ABI. Do not load these libraries into Blender itself.
"""

import functools
import os
import subprocess


def _system_gcc_libraries():
    # ldconfig's cache reflects the system's multiarch layout. Never search
    # Blender/Conda's PATH or LD_LIBRARY_PATH for the native runtime.
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
            if not fields or fields[0] not in ("libstdc++.so.6", "libgcc_s.so.1"):
                continue
            if "x86-64" in line and "=>" in line:
                path = line.split("=>", 1)[1].strip()
                if os.path.isabs(path) and os.path.isfile(path):
                    found.setdefault(fields[0], os.path.realpath(path))
        if len(found) == 2:
            return tuple(found[name] for name in ("libstdc++.so.6", "libgcc_s.so.1"))
    return ()


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
