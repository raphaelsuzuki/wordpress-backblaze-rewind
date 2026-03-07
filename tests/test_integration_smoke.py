import os
import queue
import threading
import time
import types

import sys as _sys
import types as _types

# Provide a dummy `watchdog` module for tests if not installed in the environment.
if 'watchdog' not in _sys.modules:
    _watchdog = _types.SimpleNamespace()
    _watchdog.observers = _types.SimpleNamespace(
        Observer=lambda *a, **k: _types.SimpleNamespace(
            schedule=lambda *a, **k: None,
            start=lambda *a, **k: None,
            stop=lambda *a, **k: None,
            join=lambda *a, **k: None,
        )
    )
    _watchdog.events = _types.SimpleNamespace(FileSystemEventHandler=object)
    _sys.modules['watchdog'] = _watchdog
    _sys.modules['watchdog.observers'] = _watchdog.observers
    _sys.modules['watchdog.events'] = _watchdog.events

import backup_daemon
import restore_tool


def test_daemon_to_restore_smoke_with_retries_and_partial_failure(tmp_path, monkeypatch):
    """Smoke test daemon uploads + restore flow using a shared fake backend."""
    watch_dir = tmp_path / 'watch'
    restore_dir = tmp_path / 'restore'
    watch_dir.mkdir()

    flaky = watch_dir / 'flaky.txt'
    fail = watch_dir / 'fail.txt'
    flaky.write_text('flaky-content')
    fail.write_text('fail-content')

    class FakeBackend:
        def __init__(self):
            self.upload_attempts = {}
            self.download_attempts = {}
            self.files = {}

        def upload_file(self, bucket_name, local_path, b2_dest):
            attempts = self.upload_attempts.get(b2_dest, 0) + 1
            self.upload_attempts[b2_dest] = attempts

            # Flaky file succeeds on retry; fail file always fails.
            if b2_dest.endswith('flaky.txt') and attempts < 2:
                return False
            if b2_dest.endswith('fail.txt'):
                return False

            with open(local_path, 'rb') as fh:
                self.files[b2_dest] = fh.read()
            return True

        def list_file_versions(self, bucket_name, prefix):
            out = []
            for idx, b2_path in enumerate(sorted(self.files.keys()), start=1):
                out.append(
                    {
                        'fileName': b2_path,
                        'fileId': f'id-{idx}',
                        'uploadTimestamp': 1,
                        'action': 'upload',
                    }
                )
            return out

        def download_file_version(self, file_id, dest_path):
            key = sorted(self.files.keys())[int(file_id.split('-')[1]) - 1]
            attempts = self.download_attempts.get(file_id, 0) + 1
            self.download_attempts[file_id] = attempts

            # Simulate transient restore download failure.
            if key.endswith('flaky.txt') and attempts < 2:
                return False

            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, 'wb') as fh:
                fh.write(self.files[key])
            return True

    backend = FakeBackend()

    def client_factory(cfg=None):
        return backend

    monkeypatch.setattr('backup_daemon.B2Client', types.SimpleNamespace(from_config=client_factory))
    monkeypatch.setattr('restore_tool.B2Client', types.SimpleNamespace(from_config=client_factory))
    monkeypatch.setattr('restore_tool.load_config', lambda: {'bucket_name': 'bucket', 'b2_path_prefix': 'uploads'})
    monkeypatch.setattr('backup_daemon.time.sleep', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('restore_tool.time.sleep', lambda *_args, **_kwargs: None)
    monkeypatch.setattr('backup_daemon._thread_local', types.SimpleNamespace())

    q = queue.Queue()
    q.put({'action': 'upload', 'local_path': str(flaky), 'b2_dest': 'uploads/flaky.txt'})
    q.put({'action': 'upload', 'local_path': str(fail), 'b2_dest': 'uploads/fail.txt'})
    q.put(None)

    t = threading.Thread(target=backup_daemon.upload_worker, args=(q, 'bucket', {}), daemon=True)
    t.start()
    t.join(timeout=5)

    # Flaky file eventually uploads; fail file remains missing.
    assert 'uploads/flaky.txt' in backend.files
    assert 'uploads/fail.txt' not in backend.files

    restore_tool.restore('2021-01-01T00:00:00Z', str(restore_dir))

    restored = restore_dir / 'flaky.txt'
    assert restored.exists()
    assert restored.read_text() == 'flaky-content'
