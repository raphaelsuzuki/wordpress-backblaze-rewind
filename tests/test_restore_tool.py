import os
import types
import json

import pytest

import restore_tool
import b2_client


class DummyResult:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_b2_client_list_file_versions_parses_json(monkeypatch):
    # Ensure b2_client falls back to CLI parsing when SDK isn't available
    sample = {'files': [{'fileName': 'uploads/example.txt', 'fileId': 'f1', 'uploadTimestamp': 1, 'action': 'upload'}], 'nextFileName': None, 'nextFileId': None}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        return DummyResult(returncode=0, stdout=json.dumps(sample))

    monkeypatch.setattr('subprocess.run', fake_run)
    client = b2_client.B2Client.from_config({})
    out = client.list_file_versions('bucket', 'uploads')
    assert len(out) == 1
    assert out[0]['fileName'] == 'uploads/example.txt'


def test_restore_skips_path_traversal(monkeypatch, tmp_path):
    # config mock
    monkeypatch.setattr('config_utils.load_config', lambda: {'bucket_name': 'wp-media-backups', 'b2_path_prefix': 'uploads'})

    # Provide a fake client that returns a traversal path
    class FakeClient:
        def list_file_versions(self, bucket, prefix):
            return [{'fileId': 'f1', 'fileName': 'uploads/../../etc/passwd', 'uploadTimestamp': 0, 'action': 'upload'}]

        def download_file_version(self, file_id, dest_path):
            # should not be called
            raise RuntimeError('should not download')

    monkeypatch.setattr('restore_tool.B2Client', types.SimpleNamespace(from_config=lambda cfg=None: FakeClient()))

    restore_tool.restore('2021-01-01T00:00:00Z', str(tmp_path))

    # no files created inside tmp_path
    assert not any(tmp_path.rglob('*'))


def test_restore_download_retries_and_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr('config_utils.load_config', lambda: {'bucket_name': 'wp-media-backups', 'b2_path_prefix': 'uploads'})

    file_rel = 'uploads/example/path/file.txt'

    calls = {'n': 0}

    class FakeClient:
        def list_file_versions(self, bucket, prefix):
            return [{'fileId': 'file-1', 'fileName': file_rel, 'uploadTimestamp': 0, 'action': 'upload'}]

        def download_file_version(self, file_id, dest_path):
            # simulate two failures then success (write file on success)
            calls['n'] += 1
            if calls['n'] < 3:
                return False
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, 'w') as f:
                f.write('ok')
            return True

    monkeypatch.setattr('restore_tool.B2Client', types.SimpleNamespace(from_config=lambda cfg=None: FakeClient()))

    restore_tool.restore('2021-01-01T00:00:00Z', str(tmp_path))

    # file should exist after successful retries
    assert (tmp_path / 'example' / 'path' / 'file.txt').exists()
