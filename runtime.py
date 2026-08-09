_ACTIVE_JOB = None
_GEOMETRY_REVISIONS = {}

GPU_INITIALIZATION_MESSAGE = (
    "Initializing Flow GPU; first-run shader compilation may take several minutes"
)


def active_job():
    return _ACTIVE_JOB


def active_mode():
    job = active_job()
    return getattr(job, "_job_mode", None) if job else None


def claim_job(job, mode):
    global _ACTIVE_JOB
    if _ACTIVE_JOB is not None and _ACTIVE_JOB is not job:
        raise RuntimeError(f"Fumaris is already {active_mode()}")
    job._job_mode = mode
    _ACTIVE_JOB = job


def release_job(job):
    global _ACTIVE_JOB
    if _ACTIVE_JOB is job:
        _ACTIVE_JOB = None


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
