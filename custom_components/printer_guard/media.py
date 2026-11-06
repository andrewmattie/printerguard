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

PREFIX = 'printer_guard_tmp_'
NAME = re.compile(r'^printer_guard_tmp_[a-z0-9_]{8}$')
FILES = {'frame_1.jpg', 'frame_2.jpg', 'frame_3.jpg'}
MARKER = '.printer_guard_owner'
MARKER_CONTENT = b'printer_guard staging v2\n'
TERMINAL_PREFIX = '.printer_guard_done_'
TERMINAL_NAME = re.compile(r'^\.printer_guard_done_[a-z0-9_]{8}$')
TERMINAL_MAGIC = b'printer_guard cleanup v1\n'
MAX_MARKER_SIZE = 128
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
    content=os.pread(lease.fileno(),MAX_MARKER_SIZE+1,0)
    terminal=_terminal_content(os.fstat(directory_fd))
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or not _same_file(opened, named) or opened.st_size > MAX_MARKER_SIZE
            or content not in (MARKER_CONTENT,terminal)):
        raise ValueError('staging ownership cannot be verified')
    if content == terminal and set(os.listdir(directory_fd)) != {MARKER}:
        raise ValueError('unexpected item in cleanup directory')


def _terminal_content(directory_stat):
    return TERMINAL_MAGIC + f'{directory_stat.st_dev} {directory_stat.st_ino}\n'.encode('ascii')


def _verify_terminal(root_fd, name, lease):
    opened=os.fstat(lease.fileno())
    named=os.stat(name,dir_fd=root_fd,follow_symlinks=False)
    content=os.pread(lease.fileno(),MAX_MARKER_SIZE+1,0)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink not in (1,2)
            or not _same_file(opened,named) or opened.st_size > MAX_MARKER_SIZE
            or not content.startswith(TERMINAL_MAGIC)):
        raise ValueError('cleanup ownership cannot be verified')
    identity=content[len(TERMINAL_MAGIC):]
    if not re.fullmatch(rb'[0-9]{1,24} [0-9]{1,24}\n',identity):
        raise ValueError('cleanup ownership cannot be verified')
    return tuple(int(value) for value in identity.split())


def _finish_terminal(root_fd, name, lease):
    """Remove only the recorded inode's empty directory, then the durable record.

    The record is a second link to the leased marker. It remains durable until
    rmdir has been flushed, including across a crash after marker removal.
    """
    identity=_verify_terminal(root_fd,name,lease)
    directory_name=PREFIX+name[len(TERMINAL_PREFIX):]
    directory_fd=None
    try:
        try:
            directory_fd=os.open(directory_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
        except FileNotFoundError:
            if os.fstat(lease.fileno()).st_nlink != 1:
                raise ValueError('cleanup ownership cannot be verified') from None
        else:
            current=os.fstat(directory_fd)
            if (current.st_dev,current.st_ino) != identity:
                raise ValueError('cleanup directory identity changed')
            entries=set(os.listdir(directory_fd))
            if entries not in (set(),{MARKER}):
                raise ValueError('unexpected item in cleanup directory')
            if MARKER in entries:
                marker=os.stat(MARKER,dir_fd=directory_fd,follow_symlinks=False)
                if not _same_file(marker,os.fstat(lease.fileno())) or marker.st_nlink != 2:
                    raise ValueError('cleanup ownership cannot be verified')
                os.unlink(MARKER,dir_fd=directory_fd)
                os.fsync(directory_fd)
            elif os.fstat(lease.fileno()).st_nlink != 1:
                raise ValueError('cleanup ownership cannot be verified')
            named=os.stat(directory_name,dir_fd=root_fd,follow_symlinks=False)
            if not _same_file(current,named):
                raise ValueError('cleanup directory identity changed')
            os.rmdir(directory_name,dir_fd=root_fd)
        # Only retire the record after directory removal is durable.
        os.fsync(root_fd)
        _verify_terminal(root_fd,name,lease)
        os.unlink(name,dir_fd=root_fd)
        os.fsync(root_fd)
        return True
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def cleanup_terminal(root: Path, name: str, lease=None):
    if not TERMINAL_NAME.fullmatch(name):
        raise ValueError('invalid cleanup record')
    root_fd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    opened_lease=None
    try:
        if lease is None:
            try:
                fd=os.open(name,os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root_fd)
            except FileNotFoundError:
                return False
            opened_lease=lease=_Lease(fd)
        if not stat.S_ISREG(os.fstat(lease.fileno()).st_mode):
            raise ValueError('cleanup ownership cannot be verified')
        try:
            fcntl.flock(lease.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return _finish_terminal(root_fd,name,lease)
    except OSError as error:
        if error.errno in (errno.ELOOP,errno.ENOTDIR):
            raise ValueError('cleanup ownership cannot be verified') from None
        raise
    finally:
        if opened_lease is not None:
            opened_lease.close()
        os.close(root_fd)


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
    terminal_name=TERMINAL_PREFIX+path.name[len(PREFIX):]
    # Retry a cleanup that already crossed the durable terminal boundary.
    if os.path.lexists(path.parent/terminal_name):
        return cleanup_terminal(path.parent,terminal_name,lease)
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
        # No images remain. Convert the still-leased marker into an inode-bound
        # terminal record before removing its directory entry. A crash while
        # writing this metadata can only leave an image-free, fail-closed remnant.
        content=_terminal_content(os.fstat(directory_fd))
        os.ftruncate(lease.fileno(),0)
        if os.pwrite(lease.fileno(),content,0) != len(content):
            raise OSError('incomplete cleanup ownership marker')
        os.fsync(lease.fileno())
        root_fd=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            # link() never overwrites a colliding unrelated record.
            os.link(MARKER,terminal_name,src_dir_fd=directory_fd,dst_dir_fd=root_fd,follow_symlinks=False)
            os.fsync(root_fd)
            return _finish_terminal(root_fd,terminal_name,lease)
        finally:
            os.close(root_fd)
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


class RecoveryError(ValueError):
    """A fixed summary after every independent recovery candidate was tried."""
    def __init__(self, removed, failed):
        super().__init__('staging recovery requires attention')
        self.removed=removed
        self.failed=failed


def recover(root: Path) -> int:
    """Try all independent records; preserve ambiguous entries and active work."""
    root=root.resolve(strict=True)
    removed=failed=0
    paths=list(root.iterdir())
    # Finish terminal records first; their directories may have no marker now.
    for terminal in (True,False):
        for path in paths:
            if not (TERMINAL_NAME.fullmatch(path.name) if terminal else NAME.fullmatch(path.name)):
                continue
            try:
                removed += cleanup_terminal(root,path.name) if terminal else cleanup_directory(path)
            except (OSError,ValueError):
                failed+=1
    if failed:
        raise RecoveryError(removed,failed)
    return removed


def prepare(root: Path, frames):
    """Return (directory, paths, lease); the caller must retain the live lease."""
    if len(frames) not in (1, 3) or any(not isinstance(f.content, bytes) or not 4 <= len(f.content) <= MAX_SIZE for f in frames):
        raise ValueError('invalid frame count or size')
    root = root.resolve(strict=True)
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
        size = sum(len(f.content) for f in frames) + MAX_MARKER_SIZE
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
