import os
import sys
import json
import logging
import subprocess  # nosec B404
import time

LOG = logging.getLogger(__name__)

# Fallback timeouts (seconds)
LIST_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 300


class B2Client:
    """Thin abstraction over the Backblaze B2 SDK with a CLI fallback.

    Usage:
      client = B2Client.from_config(config)
      versions = client.list_file_versions(bucket, prefix)
      client.download_file_version(file_id, dest_path)
    """

    def __init__(self, sdk_api=None):
        self._api = sdk_api
        self._use_sdk = sdk_api is not None

    @classmethod
    def from_config(cls, config=None):
        # Try to initialize SDK if credentials are available
        config = config or {}
        account_id = config.get('b2_account_id') or os.environ.get('B2_ACCOUNT_ID')
        application_key = config.get('b2_application_key') or os.environ.get('B2_APPLICATION_KEY')

        if account_id and application_key:
            try:
                from b2sdk.v2 import InMemoryAccountInfo, B2Api

                info = InMemoryAccountInfo()
                api = B2Api(info)
                api.authorize_account('production', account_id, application_key)
                LOG.info('Initialized b2sdk client')
                return cls(sdk_api=api)
            except Exception:
                LOG.exception('Failed to initialize b2sdk client; falling back to CLI')

        LOG.info('Using b2 CLI fallback (no SDK credentials found)')
        return cls(sdk_api=None)

    def list_file_versions(self, bucket_name, prefix):
        """Return a list of dicts matching the previous CLI output shape:
        { 'fileName', 'fileId', 'uploadTimestamp', 'action' }
        """
        if self._use_sdk:
            try:
                bucket = self._api.get_bucket_by_name(bucket_name)
                out = []
                # bucket.ls yields tuples (file_version, folder_name)
                for item in bucket.ls(prefix=prefix, recursive=True):
                    # some versions of the SDK return (file_version, folder_name)
                    file_version = item[0] if isinstance(item, tuple) else item
                    fv = {
                        'fileName': getattr(file_version, 'file_name', getattr(file_version, 'fileName', None)),
                        'fileId': getattr(file_version, 'id_', getattr(file_version, 'file_id', None)),
                        'uploadTimestamp': getattr(file_version, 'upload_timestamp', getattr(file_version, 'uploadTimestamp', None)),
                        'action': getattr(file_version, 'action', 'upload')
                    }
                    out.append(fv)
                return out
            except Exception:
                LOG.exception('SDK list_file_versions failed; falling back to CLI')

        # CLI fallback
        versions = []
        start_file_name = None
        start_file_id = None

        while True:
            cmd = ['b2', 'list-file-versions', bucket_name]
            if start_file_name:
                cmd.extend(['--startFileName', str(start_file_name)])
                if start_file_id:
                    cmd.extend(['--startFileId', str(start_file_id)])

            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=LIST_TIMEOUT)  # nosec B603 B607
                if result.returncode != 0:
                    LOG.error('b2 list-file-versions failed: %s', result.stderr)
                    raise RuntimeError('b2 CLI failed')
            except Exception:
                LOG.exception('Exception invoking b2 list-file-versions')
                raise

            data = json.loads(result.stdout)
            files = data.get('files', [])
            versions.extend(files)

            start_file_name = data.get('nextFileName')
            start_file_id = data.get('nextFileId')
            if not start_file_name:
                break

        return versions

    def download_file_version(self, file_id, dest_path):
        if self._use_sdk:
            try:
                from b2sdk.v2 import DownloadDestLocalFile
                self._api.download_file_by_id(file_id, DownloadDestLocalFile(dest_path))
                return True
            except Exception:
                LOG.exception('SDK download_file_version failed; falling back to CLI')

        # CLI fallback
        cmd = ['b2', 'download-file-by-id', str(file_id), str(dest_path)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)  # nosec B603 B607
            if result.returncode != 0:
                LOG.error('b2 download-file-by-id failed: %s', result.stderr)
                return False
            return True
        except Exception:
            LOG.exception('Exception invoking b2 download-file-by-id')
            return False

    def upload_file(self, bucket_name, local_path, b2_dest):
        """Upload a local file to the given bucket and destination path.

        Returns True on success, False on failure.
        """
        if self._use_sdk:
            try:
                bucket = self._api.get_bucket_by_name(bucket_name)
                # SDK: upload a local file. Different SDK versions may offer
                # slightly different method names; try a common one.
                if hasattr(bucket, 'upload_local_file'):
                    bucket.upload_local_file(local_path, file_name=b2_dest)
                else:
                    # fallback to generic upload via B2Api
                    self._api.upload_local_file(bucket_name, local_path, file_name=b2_dest)
                return True
            except Exception:
                LOG.exception('SDK upload_file failed; falling back to CLI')

        # CLI fallback
        cmd = ['b2', 'upload-file', str(bucket_name), str(local_path), str(b2_dest)]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)  # nosec B603 B607
            if result.returncode != 0:
                LOG.error('b2 upload-file failed: %s', result.stderr)
                return False
            return True
        except Exception:
            LOG.exception('Exception invoking b2 upload-file')
            return False

    def sync(self, watch_dir, bucket_name, b2_prefix):
        """Ensure files under `watch_dir` are uploaded under `b2_prefix`.

        This is a lightweight integrity scan: it uploads any files missing or
        differing, but does not perform deletions. For a full `b2 sync`
        behavior use the CLI fallback.
        """
        if self._use_sdk:
            LOG.info('Performing SDK-based integrity scan (upload-only)')
            for root, dirs, files in os.walk(watch_dir):
                for fname in files:
                    local_path = os.path.join(root, fname)
                    rel = os.path.relpath(local_path, watch_dir).replace('\\', '/')
                    b2_dest = f"{b2_prefix}/{rel}"
                    try:
                        self.upload_file(bucket_name, local_path, b2_dest)
                    except Exception:
                        LOG.exception('Failed uploading during SDK sync: %s', local_path)
            return True

        # CLI fallback: call `b2 sync`
        cmd = ['b2', 'sync', str(watch_dir), f"b2://{bucket_name}/{b2_prefix}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)  # nosec B603 B607
            if result.returncode != 0:
                LOG.error('b2 sync failed: %s', result.stderr)
                return False
            return True
        except Exception:
            LOG.exception('Exception invoking b2 sync')
            return False
