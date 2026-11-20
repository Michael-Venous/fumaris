_ACTIVE_JOB = None
_GEOMETRY_REVISIONS = {}
# Bake in Background (background.py): each domain's queued or running bake,
# by the domain's session_uid. They run beside the one job above.
_BACKGROUND_JOBS = {}
# Set in a background bake's own Blender (background_runner.py): what its
# main process passed on, which a factory-settings Blender and a snapshot
# file do not have.
_BACKGROUND_BAKE = None

GPU_INITIALIZATION_MESSAGE = (
    "Initializing Flow GPU; first-run shader compilation may take several minutes"
)

# Said once per Blender session: shaders compiled once stay in the driver's
# cache, and the report on every Play read as a problem (2026-11-14 UX test).
_GPU_NOTICE_GIVEN = False


def gpu_notice_due():
    """True the first time a preview or bake starts in this Blender."""
    global _GPU_NOTICE_GIVEN
    due = not _GPU_NOTICE_GIVEN
    _GPU_NOTICE_GIVEN = True
    return due


def active_job():
    global _ACTIVE_JOB
    job = _ACTIVE_JOB
    if job is not None:
        try:
            getattr(job, "_job_mode", None)
        except ReferenceError:
            # Blender can remove operator RNA while its Python state survives.
            # Detach first: resource cleanup can itself trigger UI callbacks.
            _ACTIVE_JOB = None
            from .jobs import recover_removed_job
            recover_removed_job(job)
    return _ACTIVE_JOB


# F8: releases a stopped preview that only keeps its cached frames (set by
# operators.py: the job's own cleanup).
_PARKED_RELEASE = [None]


def blocking_job():
    """The active job, unless it is a stopped preview keeping only its cached
    frames (F8): that one steps aside for anything else and is released when
    another job claims the GPU."""
    job = active_job()
    return None if job is not None and getattr(job, "_parked", False) else job


def release_parked_preview():
    """Ends a stopped preview's kept cache, if there is one."""
    job = active_job()
    if job is not None and getattr(job, "_parked", False) and _PARKED_RELEASE[0] is not None:
        _PARKED_RELEASE[0](job)


def active_mode():
    job = active_job()
    return getattr(job, "_job_mode", None) if job else None


def claim_job(job, mode):
    global _ACTIVE_JOB
    current = active_job()
    if current is not None and current is not job and getattr(current, "_parked", False):
        release_parked_preview()
        current = active_job()
    if current is not None and current is not job:
        raise RuntimeError(f"Fumaris is already {active_mode()}")
    job._job_mode = mode
    _ACTIVE_JOB = job


def release_job(job):
    global _ACTIVE_JOB
    if _ACTIVE_JOB is job:
        _ACTIVE_JOB = None


def background_job(domain):
    """The domain's queued or running background bake, or None. Polls and
    draw code call this: it reads memory only."""
    if domain is None or not _BACKGROUND_JOBS:
        return None
    return _BACKGROUND_JOBS.get(int(domain.session_uid))


def background_bake():
    """In a background bake's Blender: its preferences and the folder of the
    scene's own .blend file. None in the main process."""
    return _BACKGROUND_BAKE


def set_background_bake(preferences, blend_directory):
    global _BACKGROUND_BAKE
    _BACKGROUND_BAKE = {"preferences": preferences, "blend_directory": blend_directory}


def geometry_revision(mesh):
    return _GEOMETRY_REVISIONS.get(mesh.as_pointer(), 0)


def record_geometry_updates(depsgraph):
    changed = set()
    for update in depsgraph.updates:
        if not getattr(update, "is_updated_geometry", False):
            continue
        owner = getattr(update.id, "original", update.id)
        data = getattr(owner, "data", owner)
        if getattr(data, "bl_rna", None) is None:
            continue
        if data.bl_rna.identifier != "Mesh":
            continue
        changed.add(data.as_pointer())
    for key in changed:
        _GEOMETRY_REVISIONS[key] = _GEOMETRY_REVISIONS.get(key, 0) + 1


def clear_geometry_revisions():
    _GEOMETRY_REVISIONS.clear()
