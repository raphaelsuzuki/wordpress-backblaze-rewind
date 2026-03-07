import os
import queue
import threading
import types

import pytest

import sys as _sys
import types as _types

# Provide a dummy `watchdog` module for tests if not installed in the environment.
if 'watchdog' not in _sys.modules:
    _watchdog = _types.SimpleNamespace()
    _watchdog.observers = _types.SimpleNamespace(Observer=lambda *a, **k: _types.SimpleNamespace(schedule=lambda *a, **k: None, start=lambda *a, **k: None, stop=lambda *a, **k: None, join=lambda *a, **k: None))
    _watchdog.events = _types.SimpleNamespace(FileSystemEventHandler=object)
    _sys.modules['watchdog'] = _watchdog
    _sys.modules['watchdog.observers'] = _watchdog.observers
    _sys.modules['watchdog.events'] = _watchdog.events

from backup_daemon import BackupHandler, validate_config_for_daemon, upload_worker


def test_validate_config_for_daemon_raises():
    with pytest.raises(SystemExit):
        validate_config_for_daemon({'watch_dir': '/tmp'})


class DummyEvent:
    def __init__(self, src, dest, is_dir=False):
        self.src_path = src
        self.dest_path = dest
        self.is_directory = is_dir


def test_on_moved_containment(tmp_path):
    q = queue.Queue()
    watch_dir = str(tmp_path / 'watch')
    os.makedirs(watch_dir, exist_ok=True)
    handler = BackupHandler(q, watch_dir, 'uploads')

    # moved outside watch_dir -> should not enqueue
    ev = DummyEvent('/tmp/a', '/tmp/outside/file.txt', is_dir=False)
    handler.on_moved(ev)
    assert q.empty()

    # moved inside watch_dir -> should enqueue
    dest = os.path.join(watch_dir, 'sub', 'file.txt')
    ev2 = DummyEvent('/tmp/a', dest, is_dir=False)
    handler.on_moved(ev2)
    item = q.get_nowait()
    assert item['action'] == 'upload'
    assert item['local_path'] == os.path.realpath(dest)


def test_upload_worker_retries_and_continues(tmp_path, monkeypatch):
    q = queue.Queue()
    # create dummy file to upload
    local = str(tmp_path / 'file.txt')
    with open(local, 'w') as f:
        f.write('data')

    calls = {'n': 0}

    class FakeClient:
        def upload_file(self, bucket_name, local_path, b2_dest):
            calls['n'] += 1
            if calls['n'] == 1:
                # simulate missing CLI/SDK first
                raise FileNotFoundError('b2 not found')
            return True

    monkeypatch.setattr('backup_daemon.B2Client', types.SimpleNamespace(from_config=lambda cfg=None: FakeClient()))

    q.put({'action': 'upload', 'local_path': local, 'b2_dest': 'uploads/file.txt'})
    q.put(None)

    # Clear any thread-local state (in case test runner reuses threads)
    monkeypatch.setattr('backup_daemon._thread_local', types.SimpleNamespace())

    t = threading.Thread(target=upload_worker, args=(q, 'bucket', {}), daemon=True)
    t.start()
    t.join(timeout=5)

    assert calls['n'] >= 2
