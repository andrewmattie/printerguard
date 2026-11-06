"""POSIX crash/race tests using isolated directories and disposable local workers."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parents[1]/'custom_components'))
from p1s_guard import media


def abandoned(root, count=3):
    directory, paths, lease=media.prepare(root,[SimpleNamespace(content=b'1234')]*count)
    lease.close()
    return directory, paths


WORKER = r'''
import os, signal, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,sys.argv[2])
from p1s_guard import media
stage=media.Staging(Path(sys.argv[1])); stage.startup()
directory, paths=stage.prepare([SimpleNamespace(content=b'1234')]*3)
if sys.argv[3]=='cleanup':
    original=media.os.unlink
    def interrupted(name,*args,**kwargs):
        original(name,*args,**kwargs)
        if name=='frame_1.jpg':
            print('ready',flush=True)
            signal.pause()
    media.os.unlink=interrupted
    stage.cleanup(directory)
else:
    print('ready',flush=True)
    signal.pause()
'''


class RecoveryTests(unittest.TestCase):
    def test_active_directory_is_skipped_by_recovery_and_other_managers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            first=media.Staging(root); first.startup()
            directory, paths=first.prepare([SimpleNamespace(content=b'1234')])
            self.assertEqual(media.recover(root),0)
            second=media.Staging(root); second.startup()
            other,_=second.prepare([SimpleNamespace(content=b'5678')])
            self.assertTrue(paths[0].exists())
            self.assertEqual(media.recover(root),0)
            first.cleanup(directory); second.cleanup(other)
            self.assertEqual(list(root.iterdir()),[])

    def test_process_kill_during_request_is_recovered_on_next_startup(self):
        self.check_killed_worker('prepare',3)

    def test_process_kill_during_cleanup_retains_marker_for_remaining_images(self):
        self.check_killed_worker('cleanup',2)

    def check_killed_worker(self, operation, remaining_images):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            unrelated=root/'unrelated.txt'; unrelated.write_text('keep')
            environment={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
            command=[sys.executable,'-c',WORKER,str(root),str(Path(__file__).parents[1]/'custom_components'),operation]
            with subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=environment) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout,selectors.EVENT_READ)
                        self.assertTrue(selector.select(3),'worker did not become ready')
                    self.assertEqual(process.stdout.readline().strip(),'ready')
                    directories=[path for path in root.iterdir() if media.NAME.fullmatch(path.name)]
                    self.assertEqual(len(directories),1)
                    self.assertEqual(media.recover(root),0)
                    self.assertEqual(len(list(directories[0].glob('frame_*.jpg'))),remaining_images)
                    process.kill(); process.wait(timeout=3)
                    self.assertEqual((directories[0]/media.MARKER).read_bytes(),media.MARKER_CONTENT)
                    stage=media.Staging(root); stage.startup()
                    self.assertFalse(directories[0].exists())
                    self.assertEqual(unrelated.read_text(),'keep')
                    self.assertEqual(list(root.iterdir()),[unrelated])
                finally:
                    if process.poll() is None:
                        process.kill(); process.wait(timeout=3)

    def test_two_recoverers_cannot_delete_each_others_active_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); directory,_=abandoned(root)
            entered=threading.Event(); release=threading.Event()
            original=media._verify_marker
            def hold(*args):
                entered.set()
                if not release.wait(3): raise TimeoutError('synthetic test gate')
                return original(*args)
            with patch.object(media,'_verify_marker',hold), ThreadPoolExecutor(max_workers=2) as workers:
                first=workers.submit(media.recover,root)
                try:
                    self.assertTrue(entered.wait(2))
                    second=workers.submit(media.recover,root)
                    self.assertEqual(second.result(timeout=2),0)
                    self.assertTrue(directory.exists())
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=2),1)
            self.assertEqual(list(root.iterdir()),[])

    def test_unrelated_file_added_during_cleanup_is_preserved_with_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); stage=media.Staging(root); stage.startup()
            directory,_=stage.prepare([SimpleNamespace(content=b'1234')]*3)
            original=os.unlink
            def inject(name,*args,**kwargs):
                original(name,*args,**kwargs)
                if name=='frame_1.jpg':
                    (directory/'unrelated.txt').write_text('keep')
            with patch.object(media.os,'unlink',inject):
                with self.assertRaises(ValueError): stage.cleanup(directory)
            self.assertEqual((directory/'unrelated.txt').read_text(),'keep')
            self.assertTrue((directory/media.MARKER).exists())
            self.assertEqual(media.recover(root),0)
            self.assertTrue(stage.blocked)
            (directory/'unrelated.txt').unlink()
            stage.cleanup(directory)

    def test_failed_image_deletion_keeps_ownership_marker_and_blocks_more_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); stage=media.Staging(root); stage.startup()
            directory,paths=stage.prepare([SimpleNamespace(content=b'1234')])
            with patch.object(media.os,'unlink',side_effect=PermissionError('synthetic failure')):
                with self.assertRaises(PermissionError): stage.cleanup(directory)
            self.assertTrue(paths[0].exists())
            self.assertTrue((directory/media.MARKER).exists())
            self.assertEqual(media.recover(root),0)
            with self.assertRaises(RuntimeError): stage.prepare([SimpleNamespace(content=b'1234')])
            stage.cleanup(directory)

    def test_legacy_and_incomplete_markers_are_not_automatically_deleted(self):
        for marker in (b'p1s_guard staging v1\n',b'partial'):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); directory,paths=abandoned(root)
                (directory/media.MARKER).write_bytes(marker)
                stage=media.Staging(root)
                with self.assertRaises(ValueError): stage.startup()
                self.assertTrue(all(path.exists() for path in paths))
                with self.assertRaises(RuntimeError): stage.prepare([SimpleNamespace(content=b'1234')])

    def test_symlinks_hardlinks_and_unexpected_content_are_preserved(self):
        for kind in ('directory_symlink','marker_symlink','frame_symlink','frame_hardlink','unexpected'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary); target=root/'unrelated.txt'; target.write_text('keep')
                if kind=='directory_symlink':
                    directory=root/'p1s_guard_tmp_abcdefgh'; directory.symlink_to(root,target_is_directory=True)
                else:
                    directory,paths=abandoned(root)
                    if kind=='marker_symlink':
                        (directory/media.MARKER).unlink(); (directory/media.MARKER).symlink_to(target)
                    elif kind in ('frame_symlink','frame_hardlink'):
                        paths[0].unlink()
                        if kind=='frame_symlink': paths[0].symlink_to(target)
                        else: os.link(target,paths[0])
                    else:
                        (directory/'unrelated.txt').write_text('keep')
                with self.assertRaises(ValueError): media.recover(root)
                self.assertEqual(target.read_text(),'keep')
                self.assertTrue(directory.exists())

    def test_recovery_tolerates_a_directory_already_removed_by_another_cleaner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'p1s_guard_tmp_abcdefgh'
            self.assertFalse(media.cleanup_directory(path))

    def test_sync_failure_prevents_image_writes_and_leaves_staging_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); stage=media.Staging(root); stage.startup()
            with patch.object(media.os,'fsync',side_effect=OSError('synthetic sync failure')):
                with self.assertRaises(OSError): stage.prepare([SimpleNamespace(content=b'1234')])
            self.assertEqual(list(root.rglob('frame_*.jpg')),[])
            self.assertTrue(stage.blocked)
            self.assertEqual(media.recover(root),1)

    def test_new_media_has_restricted_posix_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); stage=media.Staging(root); stage.startup()
            directory,paths=stage.prepare([SimpleNamespace(content=b'1234')])
            self.assertEqual(directory.stat().st_mode & 0o777,0o700)
            for path in [directory/media.MARKER,*paths]:
                self.assertEqual(path.stat().st_mode & 0o777,0o600)
            stage.cleanup(directory)


if __name__=='__main__': unittest.main()
