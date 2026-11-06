"""Bounded media staging and conservative recovery of our own abandoned files."""
from pathlib import Path
import re
import os
import shutil
import tempfile

PREFIX = 'p1s_guard_tmp_'
NAME = re.compile(r'^p1s_guard_tmp_[a-z0-9_]{8}$')
FILES = {'frame_1.jpg', 'frame_2.jpg', 'frame_3.jpg'}
MARKER = '.p1s_guard_owner'
MARKER_CONTENT = b'p1s_guard staging v1\n'
MAX_SIZE = 8_000_000

def cleanup_directory(path: Path):
    if not NAME.fullmatch(path.name) or path.is_symlink() or not path.is_dir():
        raise ValueError('invalid staging directory')
    marker=path / MARKER
    if (marker.is_symlink() or not marker.is_file()
            or marker.stat().st_size != len(MARKER_CONTENT)
            or marker.read_bytes() != MARKER_CONTENT):
        raise ValueError('staging ownership cannot be verified')
    for child in path.iterdir():
        if child.name not in FILES | {MARKER} or child.is_symlink() or not child.is_file():
            raise ValueError('unexpected item in staging directory')
    shutil.rmtree(path)

def recover(root: Path):
    root = root.resolve(strict=True)
    for path in root.iterdir():
        if NAME.fullmatch(path.name):
            cleanup_directory(path)

def prepare(root: Path, frames):
    if len(frames) not in (1, 3) or any(not isinstance(f.content,bytes) or not 4 <= len(f.content) <= MAX_SIZE for f in frames):
        raise ValueError('invalid frame count or size')
    directory=Path(tempfile.mkdtemp(prefix=PREFIX,dir=root))
    try:
        marker=directory / MARKER
        with os.fdopen(os.open(marker,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600),'wb') as file:
            file.write(MARKER_CONTENT)
        paths=[]
        for index,frame in enumerate(frames,1):
            path=directory / f'frame_{index}.jpg'
            with os.fdopen(os.open(path,os.O_WRONLY | os.O_CREAT | os.O_EXCL,0o600),'wb') as file:
                file.write(frame.content)
            paths.append(path)
        return directory,paths
    except BaseException:
        if marker.is_file() and marker.read_bytes()==MARKER_CONTENT:
            cleanup_directory(directory)
        elif not any(directory.iterdir()):
            directory.rmdir()
        raise

class Staging:
    """Shared in-process quota, reserved before writes and latched on failure."""
    MAX_ACTIVE = 2
    MAX_BYTES = 48_000_000

    def __init__(self, root: Path):
        import threading
        if not root.is_absolute(): raise ValueError('absolute media root required')
        self.root=root
        self.lock=threading.Lock()
        self.active={}
        self.blocked=True

    def startup(self):
        with self.lock:
            if self.active: raise RuntimeError('staging in use')
            self.blocked=True
            recover(self.root)
            self.blocked=False

    def prepare(self, frames):
        size=sum(len(f.content) for f in frames) + len(MARKER_CONTENT)
        with self.lock:
            if self.blocked: raise RuntimeError('staging disabled')
            if len(self.active) >= self.MAX_ACTIVE or sum(self.active.values()) + size > self.MAX_BYTES:
                raise RuntimeError('staging quota exceeded')
            try:
                directory,paths=prepare(self.root,frames)
            except BaseException:
                self.blocked=True
                raise
            self.active[directory]=size
            return directory,paths

    def cleanup(self, directory):
        with self.lock:
            if directory not in self.active:
                self.blocked=True
                raise RuntimeError('unknown staging directory')
            try:
                cleanup_directory(directory)
            except BaseException:
                self.blocked=True
                raise
            del self.active[directory]
