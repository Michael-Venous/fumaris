"""Preserve a published bake while its replacement is simulated."""
import json
import os
from pathlib import Path
import shutil
import tempfile

from .cache import SETTINGS_FILE, cache_lock, require_unprotected
from .importers import _frame_files, _frame_number, _particle_files

WORK = ".fumaris_replace"
JOURNAL = "publish.json"


def _files(directory, prefix):
    from .compare import NOTE_FILE, STATS_FILE
    particles, particle_prefix = _particle_files(directory, prefix)
    files = [*_frame_files(directory, prefix), *_frame_files(particles, particle_prefix, ".ply")]
    files += [directory / name for name in (SETTINGS_FILE, STATS_FILE, NOTE_FILE) if (directory / name).is_file()]
    return {Path(path).relative_to(directory).as_posix() for path in files}


def _path(directory, name, prefix):
    """Validate persisted journal entries before touching any files."""
    from .compare import NOTE_FILE, STATS_FILE
    path = Path(name)
    if len(path.parts) == 1 and (name in (SETTINGS_FILE, STATS_FILE, NOTE_FILE)
                                 or _frame_number(name, prefix) is not None):
        return directory / path
    if (len(path.parts) == 2 and path.parts[0] == "particles"
            and _frame_number(path.name, f"{prefix}particles_", ".ply") is not None):
        return directory / path
    raise ValueError(f"Invalid Fumaris replacement file: {name}")


def _install(source, destination, temporary):
    """Publish a complete file atomically and keep the staged source intact."""
    destination.parent.mkdir(exist_ok=True)
    temporary.unlink(missing_ok=True)
    try:
        os.link(source, temporary)
    except OSError:
        shutil.copy2(source, temporary)
    os.replace(temporary, destination)


def _matches_backup(backup, destination):
    try:
        backup_stat = backup.stat()
        destination_stat = destination.stat()
    except FileNotFoundError:
        return False
    if backup_stat.st_ino and os.path.samestat(backup_stat, destination_stat):
        return True
    if backup_stat.st_size != destination_stat.st_size:
        return False
    # On filesystems without hard links a rollback must also skip an
    # original that still matches its backup. An open Windows reader may
    # permit reading that file while denying both publish and replacement.
    with backup.open("rb") as previous, destination.open("rb") as current:
        while True:
            chunk = previous.read(1 << 20)
            if chunk != current.read(1 << 20):
                return False
            if not chunk:
                return True


def _rollback(target, work):
    journal = work / JOURNAL
    metadata = json.loads(journal.read_text(encoding="utf-8"))
    prefix = metadata["prefix"]
    old, new = set(metadata["old"]), set(metadata["new"])
    temporary = work / "frame.tmp"
    for name in sorted(old):
        backup = _path(work / "backup", name, prefix)
        destination = _path(target, name, prefix)
        if not _matches_backup(backup, destination):
            _install(backup, destination, temporary)
    for name in sorted(new - old):
        try:
            _path(target, name, prefix).unlink(missing_ok=True)
        except NotADirectoryError:
            pass  # A plain file blocked creation of the particles directory.
    journal.unlink()


def _keep_partial(target, work, prefix):
    frames = work / "frames"
    # A failed setup can leave only settings, which do not need a version.
    if not _frame_files(frames, prefix):
        return ""
    # Reserve a free directory without racing other Versions writers.
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".fumaris_partial_") as reserved:
        suffix = Path(reserved).name.rsplit("_", 1)[-1]
        partial = target.with_name(f"{target.name}_partial_{suffix}")
        frames.rename(partial)
    return str(partial)


def _discard_committed(work):
    # Keep the commit marker until the backup and staging have both gone.
    for entry in work.iterdir():
        if entry.name == "committed":
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (work / "committed").unlink()
    work.rmdir()


def recover_replacement(directory, prefix):
    """Called under the slot lock. Restore an interrupted publication."""
    target = Path(directory)
    work = target / WORK
    if not work.is_dir():
        return
    # A background worker can still be closing if its parent was killed.
    frames = work / "frames"
    if frames.is_dir():
        with cache_lock(frames):
            pass
    if (work / JOURNAL).is_file():
        _rollback(target, work)
    if (work / "committed").exists():
        _discard_committed(work)
    else:
        _keep_partial(target, work, prefix)
        shutil.rmtree(work)


class BakeReplacement:
    """Hold the destination slot's guard through baking and publication."""

    @classmethod
    def begin(cls, directory, prefix):
        target = Path(directory)
        lock = cache_lock(directory).acquire()
        try:
            require_unprotected(directory, "bake over it")
            recover_replacement(directory, prefix)
            particles, particle_prefix = _particle_files(directory, prefix)
            if not _frame_files(directory, prefix) and not _frame_files(particles, particle_prefix, ".ply"):
                lock.release()
                return None
            return cls(target, prefix, lock)
        except BaseException:
            lock.release()
            raise

    def __init__(self, target, prefix, lock):
        self.target = target
        self.prefix = prefix
        self.lock = lock
        self.work = target / WORK
        self.frames = self.work / "frames"
        self.frames.mkdir(parents=True)
        self.directory = str(self.frames)

    def complete(self):
        from .compare import read_meta, write_note
        require_unprotected(str(self.target), "bake over it")
        meta = read_meta(self.target)
        note = meta.pop("note", "")
        meta["pinned"] = True
        write_note(self.frames, note, **meta)
        old, new = _files(self.target, self.prefix), _files(self.frames, self.prefix)
        temporary = self.work / "frame.tmp"
        (self.work / "backup").mkdir(exist_ok=True)
        for name in sorted(old):
            _install(self.target / name, self.work / "backup" / name, temporary)
        journal = self.work / JOURNAL
        journal_tmp = journal.with_suffix(".tmp")
        journal_tmp.write_text(json.dumps({"prefix": self.prefix, "old": sorted(old), "new": sorted(new)}),
                               encoding="utf-8")
        journal_tmp.replace(journal)
        try:
            for name in sorted(new):
                _install(self.frames / name, self.target / name, temporary)
            for name in sorted(old - new):
                (self.target / name).unlink()
            # Rename is the commit point. Recovery cleans the completed work.
            journal.replace(self.work / "committed")
        except BaseException:
            _rollback(self.target, self.work)
            raise
        # The next access can remove remaining scratch files after an OS error.
        try:
            _discard_committed(self.work)
        except OSError:
            pass
        finally:
            self.lock.release()

    def stop(self):
        try:
            if (self.work / JOURNAL).is_file():
                _rollback(self.target, self.work)
            partial = _keep_partial(self.target, self.work, self.prefix)
            shutil.rmtree(self.work)
            return partial
        finally:
            self.lock.release()
