import os
import sys
import json
import pytest
from unittest.mock import Mock, MagicMock, patch, mock_open
import subprocess

import b2_client


class TestB2ClientSDKPath:
    """Test B2Client using the SDK path (mocking b2sdk objects)."""

    def test_from_config_initializes_sdk(self, monkeypatch):
        """Test that from_config creates SDK client when credentials are present."""
        mock_api = Mock()
        mock_info = Mock()

        # Mock the SDK imports inside b2_client module
        mock_sdk_module = Mock()
        mock_sdk_module.InMemoryAccountInfo = Mock(return_value=mock_info)
        mock_sdk_module.B2Api = Mock(return_value=mock_api)

        import sys
        with patch.dict('sys.modules', {'b2sdk': Mock(), 'b2sdk.v2': mock_sdk_module}):
            config = {'b2_account_id': 'test_id', 'b2_application_key': 'test_key'}
            client = b2_client.B2Client.from_config(config)

            assert client._use_sdk is True
            assert client._api is mock_api
            mock_api.authorize_account.assert_called_once_with('production', 'test_id', 'test_key')

    def test_from_config_uses_env_vars(self, monkeypatch):
        """Test that from_config uses environment variables when config is empty."""
        monkeypatch.setenv('B2_ACCOUNT_ID', 'env_id')
        monkeypatch.setenv('B2_APPLICATION_KEY', 'env_key')

        mock_api = Mock()
        mock_info = Mock()

        mock_sdk_module = Mock()
        mock_sdk_module.InMemoryAccountInfo = Mock(return_value=mock_info)
        mock_sdk_module.B2Api = Mock(return_value=mock_api)

        with patch.dict('sys.modules', {'b2sdk': Mock(), 'b2sdk.v2': mock_sdk_module}):
            client = b2_client.B2Client.from_config({})

            assert client._use_sdk is True
            mock_api.authorize_account.assert_called_once_with('production', 'env_id', 'env_key')

    def test_from_config_falls_back_to_cli_on_sdk_failure(self, monkeypatch, caplog):
        """Test that from_config falls back to CLI when SDK init fails."""
        mock_sdk_module = Mock()
        mock_sdk_module.InMemoryAccountInfo = Mock(side_effect=ImportError('no b2sdk'))

        with patch.dict('sys.modules', {'b2sdk': Mock(), 'b2sdk.v2': mock_sdk_module}):
            config = {'b2_account_id': 'test_id', 'b2_application_key': 'test_key'}
            client = b2_client.B2Client.from_config(config)

            assert client._use_sdk is False
            assert client._api is None
            assert 'Failed to initialize b2sdk client' in caplog.text

    def test_list_file_versions_sdk_path(self):
        """Test list_file_versions using SDK mock."""
        mock_api = Mock()
        mock_bucket = Mock()
        mock_api.get_bucket_by_name.return_value = mock_bucket

        # Mock file version object
        mock_fv = Mock()
        mock_fv.file_name = 'test.txt'
        mock_fv.id_ = 'file123'
        mock_fv.upload_timestamp = 1234567890000
        mock_fv.action = 'upload'

        mock_bucket.ls.return_value = [mock_fv]

        client = b2_client.B2Client(sdk_api=mock_api)
        versions = client.list_file_versions('my-bucket', 'prefix/')

        assert len(versions) == 1
        assert versions[0]['fileName'] == 'test.txt'
        assert versions[0]['fileId'] == 'file123'
        assert versions[0]['uploadTimestamp'] == 1234567890000
        assert versions[0]['action'] == 'upload'
        mock_bucket.ls.assert_called_once()

    def test_list_file_versions_sdk_tuple_response(self):
        """Test list_file_versions when SDK returns tuples."""
        mock_api = Mock()
        mock_bucket = Mock()
        mock_api.get_bucket_by_name.return_value = mock_bucket

        mock_fv = Mock()
        mock_fv.file_name = 'doc.pdf'
        mock_fv.id_ = 'abc456'
        mock_fv.upload_timestamp = 9999999999000
        mock_fv.action = 'upload'

        # SDK returns tuple (file_version, folder_name)
        mock_bucket.ls.return_value = [(mock_fv, None)]

        client = b2_client.B2Client(sdk_api=mock_api)
        versions = client.list_file_versions('bucket', '')

        assert len(versions) == 1
        assert versions[0]['fileName'] == 'doc.pdf'
        assert versions[0]['fileId'] == 'abc456'

    def test_download_file_version_sdk_with_save_to(self, tmp_path):
        """Test download using SDK when returned object has save_to method."""
        mock_api = Mock()
        mock_downloaded = Mock()
        mock_downloaded.save_to = Mock()
        mock_api.download_file_by_id.return_value = mock_downloaded

        client = b2_client.B2Client(sdk_api=mock_api)
        dest = str(tmp_path / 'output.bin')
        result = client.download_file_version('file789', dest)

        assert result is True
        mock_downloaded.save_to.assert_called_once_with(dest)

    def test_download_file_version_sdk_with_read(self, tmp_path):
        """Test download using SDK when returned object is file-like with read()."""
        mock_api = Mock()
        mock_downloaded = Mock()
        del mock_downloaded.save_to  # no save_to method
        mock_downloaded.read.return_value = b'test data'

        mock_api.download_file_by_id.return_value = mock_downloaded

        client = b2_client.B2Client(sdk_api=mock_api)
        dest = str(tmp_path / 'output2.bin')
        result = client.download_file_version('file999', dest)

        assert result is True
        assert os.path.exists(dest)
        with open(dest, 'rb') as f:
            assert f.read() == b'test data'

    def test_download_file_version_sdk_with_get_bytes(self, tmp_path):
        """Test download using SDK when returned object has get_bytes()."""
        mock_api = Mock()
        mock_downloaded = Mock()
        del mock_downloaded.save_to
        del mock_downloaded.read
        mock_downloaded.get_bytes.return_value = b'bytes content'

        mock_api.download_file_by_id.return_value = mock_downloaded

        client = b2_client.B2Client(sdk_api=mock_api)
        dest = str(tmp_path / 'output3.bin')
        result = client.download_file_version('fileXYZ', dest)

        assert result is True
        with open(dest, 'rb') as f:
            assert f.read() == b'bytes content'

    def test_upload_file_sdk_path(self, tmp_path):
        """Test upload using SDK."""
        mock_api = Mock()
        mock_bucket = Mock()
        mock_bucket.upload_local_file = Mock()
        mock_api.get_bucket_by_name.return_value = mock_bucket

        # Create temp file to upload
        local = tmp_path / 'upload.txt'
        local.write_text('upload me')

        client = b2_client.B2Client(sdk_api=mock_api)
        result = client.upload_file('test-bucket', str(local), 'remote/upload.txt')

        assert result is True
        mock_bucket.upload_local_file.assert_called_once_with(str(local), file_name='remote/upload.txt')

    def test_sync_sdk_path_success(self, tmp_path):
        """Test sync using SDK (upload-only integrity scan)."""
        mock_api = Mock()
        mock_bucket = Mock()
        mock_bucket.upload_local_file = Mock()
        mock_api.get_bucket_by_name.return_value = mock_bucket

        watch_dir = tmp_path / 'watch'
        watch_dir.mkdir()
        (watch_dir / 'file1.txt').write_text('hello')
        (watch_dir / 'sub').mkdir()
        (watch_dir / 'sub' / 'file2.log').write_text('world')

        client = b2_client.B2Client(sdk_api=mock_api)
        result = client.sync(str(watch_dir), 'my-bucket', 'backups')

        assert result is True
        assert mock_bucket.upload_local_file.call_count == 2

    def test_sync_sdk_path_with_failure(self, tmp_path, caplog):
        """Test sync returns False when any upload fails."""
        mock_api = Mock()
        mock_bucket = Mock()
        # First upload succeeds, second fails
        mock_bucket.upload_local_file.side_effect = [None, Exception('upload error')]
        mock_api.get_bucket_by_name.return_value = mock_bucket

        watch_dir = tmp_path / 'watch'
        watch_dir.mkdir()
        (watch_dir / 'ok.txt').write_text('ok')
        (watch_dir / 'fail.txt').write_text('fail')

        client = b2_client.B2Client(sdk_api=mock_api)
        result = client.sync(str(watch_dir), 'bucket', 'prefix')

        assert result is False
        assert 'Failed uploading during SDK sync' in caplog.text


class TestB2ClientCLIFallback:
    """Test B2Client CLI fallback path."""

    def test_from_config_no_credentials(self):
        """Test that from_config creates CLI-only client when no credentials."""
        client = b2_client.B2Client.from_config({})
        assert client._use_sdk is False
        assert client._api is None

    def test_list_file_versions_cli_fallback(self, monkeypatch):
        """Test list_file_versions using CLI fallback."""
        client = b2_client.B2Client(sdk_api=None)

        # Mock subprocess.run for `b2 ls --versions --json`
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({'fileName': 'test.dat', 'fileId': 'id123', 'uploadTimestamp': 111, 'action': 'upload'}) + '\n'

        with patch('subprocess.run', return_value=mock_result) as mock_run:
            versions = client.list_file_versions('bucket', 'pre')

            assert len(versions) == 1
            assert versions[0]['fileName'] == 'test.dat'
            assert versions[0]['fileId'] == 'id123'
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert 'b2' in cmd
            assert 'ls' in cmd
            assert '--versions' in cmd
            assert 'bucket' in cmd
            assert 'pre' in cmd

    def test_list_file_versions_cli_with_files_array(self, monkeypatch):
        """Test list_file_versions CLI when response contains 'files' array."""
        client = b2_client.B2Client(sdk_api=None)

        mock_result = Mock()
        mock_result.returncode = 0
        # Simulate CLI returning a JSON object with 'files' array
        obj = {
            'files': [
                {'name': 'a.txt', 'id': 'id1', 'time': 100, 'action': 'upload'},
                {'fileName': 'b.log', 'fileId': 'id2', 'uploadTimestamp': 200}
            ]
        }
        mock_result.stdout = json.dumps(obj) + '\n'

        with patch('subprocess.run', return_value=mock_result):
            versions = client.list_file_versions('mybucket', '')

            assert len(versions) == 2
            assert versions[0]['fileName'] == 'a.txt'
            assert versions[0]['fileId'] == 'id1'
            assert versions[0]['uploadTimestamp'] == 100
            assert versions[1]['fileName'] == 'b.log'
            assert versions[1]['fileId'] == 'id2'
            assert versions[1]['uploadTimestamp'] == 200

    def test_download_file_version_cli_fallback(self, tmp_path):
        """Test download using CLI fallback."""
        client = b2_client.B2Client(sdk_api=None)

        dest = str(tmp_path / 'downloaded.bin')
        mock_result = Mock()
        mock_result.returncode = 0

        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = client.download_file_version('fileid111', dest)

            assert result is True
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert 'b2' in cmd
            assert 'download-file-by-id' in cmd
            assert 'fileid111' in cmd
            assert dest in cmd

    def test_upload_file_cli_fallback(self, tmp_path):
        """Test upload using CLI fallback."""
        client = b2_client.B2Client(sdk_api=None)

        local = tmp_path / 'local.dat'
        local.write_bytes(b'data')

        mock_result = Mock()
        mock_result.returncode = 0

        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = client.upload_file('test-bucket', str(local), 'remote.dat')

            assert result is True
            cmd = mock_run.call_args[0][0]
            assert 'b2' in cmd
            assert 'upload-file' in cmd
            assert 'test-bucket' in cmd
            assert str(local) in cmd
            assert 'remote.dat' in cmd

    def test_sync_cli_fallback(self, tmp_path):
        """Test sync using CLI fallback."""
        client = b2_client.B2Client(sdk_api=None)

        watch = tmp_path / 'watch'
        watch.mkdir()

        mock_result = Mock()
        mock_result.returncode = 0

        with patch('subprocess.run', return_value=mock_result) as mock_run:
            result = client.sync(str(watch), 'mybucket', 'prefix')

            assert result is True
            cmd = mock_run.call_args[0][0]
            assert 'b2' in cmd
            assert 'sync' in cmd
            assert str(watch) in cmd
            assert 'b2://mybucket/prefix' in cmd

    def test_cli_fallback_handles_errors(self):
        """Test that CLI fallback logs and returns False on subprocess errors."""
        client = b2_client.B2Client(sdk_api=None)

        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stderr = 'b2 error'

        with patch('subprocess.run', return_value=mock_result):
            result = client.download_file_version('badfile', '/tmp/out')
            assert result is False
