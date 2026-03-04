import json
import os
import queue
import types
import time

import pytest

import restore_tool


class DummyResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_list_file_versions_parses_json(monkeypatch):
    sample = {'files': [{'fileName': 'uploads/example.txt', 'fileId': 'f1', 'uploadTimestamp': 1, 'action': 'upload'}], 'nextFileName': None, 'nextFileId': None}
    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        return DummyResult(returncode=0, stdout=json.dumps(sample))

    monkeypatch.setattr('subprocess.run', fake_run)
    out = restore_tool.list_file_versions('bucket', 'uploads')
    assert len(out) == 1
    assert out[0]['fileName'] == 'uploads/example.txt'


def test_restore_skips_path_traversal(monkeypatch, tmp_path):
    # config mock
    monkeypatch.setattr('config_utils.load_config', lambda: {'bucket_name': 'wp-media-backups', 'b2_path_prefix': 'uploads'})

    # list_file_versions returns a traversal file only
    monkeypatch.setattr('restore_tool.list_file_versions', lambda bucket, prefix: [
        {'fileId': 'f1', 'fileName': 'uploads/../../etc/passwd', 'uploadTimestamp': 0, 'action': 'upload'}
    ])

    # mock subprocess.run (should not be called)
    called = []
    def fake_run(*args, **kwargs):
        called.append(args)
        return DummyResult(returncode=0)
    monkeypatch.setattr('subprocess.run', fake_run)

    restore_tool.restore('2021-01-01T00:00:00Z', str(tmp_path))

    # no files created inside tmp_path
    assert not any(tmp_path.rglob('*'))
    assert called == []


def test_restore_download_retries_and_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr('config_utils.load_config', lambda: {'bucket_name': 'wp-media-backups', 'b2_path_prefix': 'uploads'})

    file_rel = 'uploads/example/path/file.txt'
    monkeypatch.setattr('restore_tool.list_file_versions', lambda bucket, prefix: [
        {'fileId': 'file-1', 'fileName': file_rel, 'uploadTimestamp': 0, 'action': 'upload'}
    ])

    calls = {'n': 0}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        # simulate two failures then success
        calls['n'] += 1
        if calls['n'] < 3:
            return DummyResult(returncode=1, stderr='transient error')
        # on success, write the file
        dest = cmd[-1]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'w') as f:
            f.write('ok')
        return DummyResult(returncode=0)

    monkeypatch.setattr('subprocess.run', fake_run)

    restore_tool.restore('2021-01-01T00:00:00Z', str(tmp_path))

    # file should exist after successful retries
    assert (tmp_path / 'example' / 'path' / 'file.txt').exists()
