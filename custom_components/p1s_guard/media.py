"""Bounded media staging with leased ownership and conservative crash recovery."""
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
import tempfile
import weakref

PREFIX = 'p1s_guard_tmp_'
NAME = re.compile(r'^p1s_guard_tmp_[a-z0-9_]{8}$')
FILES = {'frame_1.jpg', 'frame_2.jpg', 'frame_3.jpg'}
MARKER = '.p1s_guard_owner'
MARKER_CONTENT = b'p1s_guard staging v2\n'
MAX_SIZE = 8_000_000


class _Lease:
    """An OS lock handle; finalization closes it without deleting any files."""
    def __init__(self, fd):
        self.fd = fd
        self._close = weakref.finalize(self, os.close, fd)

    def fileno(self):
        if not self._close.alive:
            raise ValueError('staging lease closed')
        return self.fd

    def close(self):
        self._close()


def _same_file(left, right):
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _verify_marker(directory_fd, lease):
    opened = os.fstat(lease.fileno())
    named = os.stat(MARKER, dir_fd=directory_fd, follow_symlinks=False)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or not _same_file(opened, named)
            or opened.st_size != len(MARKER_CONTENT)
            or os.pread(lease.fileno(), len(MARKER_CONTENT) + 1, 0) != MARKER_CONTENT):
        raise ValueError('staging ownership cannot be verified')


def _entries(directory_fd):
    entries = {}
    for name in os.listdir(directory_fd):
        item = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if name not in FILES | {MARKER} or not stat.S_ISREG(item.st_mode) or item.st_nlink != 1:
            raise ValueError('unexpected item in staging directory')
        entries[name] = item
    return entries


def cleanup_directory(path: Path, lease: _Lease | None = None) -> bool:
    """Delete only a verified, exclusively leased directory's allowlisted files.

    A busy lease or an already removed directory is skipped. Version 1 ownership
    markers are deliberately rejected: old writers did not hold leases, so their
    files cannot be proven inactive. Supplied leases remain the caller's property.
    """
    if not NAME.fullmatch(path.name) or path.is_symlink():
        raise ValueError('invalid staging directory')
    directory_fd = None
    opened_lease = None
    try:
        try:
            directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        except NotADirectoryError:
            raise ValueError('invalid staging directory') from None
        if lease is None:
            try:
                fd = os.open(MARKER, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            except FileNotFoundError:
                if not path.exists():
                    return False
                raise ValueError('staging ownership cannot be verified') from None
            opened_lease = lease = _Lease(fd)
        if not stat.S_ISREG(os.fstat(lease.fileno()).st_mode):
            raise ValueError('staging ownership cannot be verified')
        try:
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        _verify_marker(directory_fd, lease)
        entries = _entries(directory_fd)
        for name in sorted(entries.keys() & FILES):
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _same_file(current, entries[name]) or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
                raise ValueError('staging content changed during cleanup')
            os.unlink(name, dir_fd=directory_fd)
        # Keep the ownership marker until image deletions have been flushed.
        os.fsync(directory_fd)
        _verify_marker(directory_fd, lease)
        if set(os.listdir(directory_fd)) != {MARKER}:
            raise ValueError('staging content changed during cleanup')
        if not _same_file(os.fstat(directory_fd), path.stat(follow_symlinks=False)):
            raise ValueError('staging directory changed during cleanup')
        os.unlink(MARKER, dir_fd=directory_fd)
        os.fsync(directory_fd)
        os.rmdir(path)
        return True
    except FileNotFoundError:
        if not path.exists():
            return False
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError('staging ownership cannot be verified') from None
        raise
    finally:
        if opened_lease is not None:
            opened_lease.close()
        if directory_fd is not None:
            os.close(directory_fd)


def recover(root: Path) -> int:
    """Recover verified abandoned v2 media; never delete an active lease."""
    root = root.resolve(strict=True)
    removed = 0
    for path in root.iterdir():
        if NAME.fullmatch(path.name):
            removed += cleanup_directory(path)
    return removed


def prepare(root: Path, frames):
    """Return (directory, paths, lease); the caller must retain the live lease."""
    if len(frames) not in (1, 3) or any(not isinstance(f.content, bytes) or not 4 <= len(f.content) <= MAX_SIZE for f in frames):
        raise ValueError('invalid frame count or size')
    directory = Path(tempfile.mkdtemp(prefix=PREFIX, dir=root))
    lease = None
    try:
        fd = os.open(directory / MARKER, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        lease = _Lease(fd)
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.write(lease.fileno(), MARKER_CONTENT) != len(MARKER_CONTENT):
            raise OSError('incomplete staging ownership marker')
        os.fsync(lease.fileno())
        # Establish ownership before any private frame is written. Durability
        # still depends on the filesystem and storage honoring fsync.
        for parent in (directory, root):
            parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        paths = []
        for index, frame in enumerate(frames, 1):
            path = directory / f'frame_{index}.jpg'
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as file:
                file.write(frame.content)
            paths.append(path)
        return directory, paths, lease
    except BaseException:
        try:
            if lease is not None:
                cleanup_directory(directory, lease)
            elif not any(directory.iterdir()):
                directory.rmdir()
        finally:
            if lease is not None:
                lease.close()
        raise


@dataclass
class Reservation:
    size: int
    lease: _Lease


class Staging:
    """Shared in-process quota, plus per-directory OS leases across processes."""
    MAX_ACTIVE = 2
    MAX_BYTES = 48_000_000

    def __init__(self, root: Path):
        import threading
        if not root.is_absolute():
            raise ValueError('absolute media root required')
        self.root = root
        self.lock = threading.Lock()
        self.active = {}
        self.blocked = True

    def startup(self):
        with self.lock:
            if self.active:
                raise RuntimeError('staging in use')
            self.blocked = True
            recover(self.root)
            self.blocked = False

    def prepare(self, frames):
        size = sum(len(f.content) for f in frames) + len(MARKER_CONTENT)
        with self.lock:
            if self.blocked:
                raise RuntimeError('staging disabled')
            if len(self.active) >= self.MAX_ACTIVE or sum(item.size for item in self.active.values()) + size > self.MAX_BYTES:
                raise RuntimeError('staging quota exceeded')
            try:
                directory, paths, lease = prepare(self.root, frames)
            except BaseException:
                self.blocked = True
                raise
            self.active[directory] = Reservation(size, lease)
            return directory, paths

    def cleanup(self, directory):
        with self.lock:
            if directory not in self.active:
                self.blocked = True
                raise RuntimeError('unknown staging directory')
            reservation = self.active[directory]
            try:
                if not cleanup_directory(directory, reservation.lease):
                    raise RuntimeError('staging lease unavailable')
            except BaseException:
                self.blocked = True
                raise
            reservation.lease.close()
            del self.active[directory]
