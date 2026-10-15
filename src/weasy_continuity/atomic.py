"""Per-file atomic publication with receipt-last commit and ordinary rollback.

There is no joint two-path atomic filesystem transaction. A process crash after
PDF replacement and before receipt replacement is detected by receipt/PDF hashes.
"""

from __future__ import annotations
from contextlib import contextmanager
from pathlib import Path
import fcntl, hashlib, os, tempfile
from .errors import ContinuityError


def digest(path: Path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


@contextmanager
def lock_targets(paths):
    handles = []
    try:
        for target in sorted(set(paths), key=str):
            lock_name = (
                ".weasy-continuity-"
                + hashlib.sha256(str(target).encode()).hexdigest()[:24]
                + ".lock"
            )
            fd = os.open(
                target.parent / lock_name,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            handle = os.fdopen(fd, "a+")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise ContinuityError(
                    "output_busy", "Another render owns a requested output path."
                ) from exc
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_files(
    staged_pdf: Path, staged_receipt: Path, output: Path, receipt: Path, *, force: bool
):
    """Publish validated files, receipt last; restore old targets on exceptions."""
    staged = [(staged_pdf, output), (staged_receipt, receipt)]
    backups = {}
    published = []
    retain_backups = False
    try:
        for _, destination in staged:
            if destination.is_symlink():
                raise ContinuityError(
                    "unsafe_output", "Symbolic-link output targets are unsupported."
                )
            if destination.exists():
                if not force:
                    raise ContinuityError(
                        "output_exists",
                        "An output already exists; use a matching prior receipt and --force to replace it.",
                    )
                fd, name = tempfile.mkstemp(
                    prefix=".weasy-continuity-backup-", dir=destination.parent
                )
                os.close(fd)
                backup = Path(name)
                backup.unlink()
                os.link(destination, backup)
                backups[destination] = backup
        for stage, destination in staged:
            with stage.open("rb") as stream:
                os.fsync(stream.fileno())
            identity = stage.stat().st_dev, stage.stat().st_ino
            if force:
                os.replace(stage, destination)
            else:
                # Atomic creation without a check/replace race over another file.
                os.link(stage, destination)
                stage.unlink()
            published.append((destination, identity))
            fsync_directory(destination.parent)
    except Exception as exc:
        rollback_errors = []
        for destination, identity in reversed(published):
            try:
                stat = destination.stat()
                if (stat.st_dev, stat.st_ino) != identity:
                    rollback_errors.append(str(destination))
                    continue
                if destination in backups:
                    os.replace(backups.pop(destination), destination)
                else:
                    destination.unlink()
                fsync_directory(destination.parent)
            except Exception:
                rollback_errors.append(str(destination))
        if rollback_errors:
            retain_backups = True
            raise ContinuityError(
                "publication_recovery_required",
                "Publication was interrupted and a target changed externally; verify the receipt before use.",
                {
                    "paths": rollback_errors,
                    "retained_backups": [str(p) for p in backups.values()],
                },
            ) from exc
        if isinstance(exc, ContinuityError):
            raise
        raise ContinuityError(
            "publication_failed",
            "Output publication failed; prior output files were restored.",
            {"reason": str(exc)[:300]},
        ) from exc
    finally:
        if not retain_backups:
            for backup in backups.values():
                backup.unlink(missing_ok=True)
