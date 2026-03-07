import os
import json
import logging
import subprocess  # nosec B404

LOG = logging.getLogger(__name__)

# Fallback timeouts (seconds)
LIST_TIMEOUT = 120
DOWNLOAD_TIMEOUT = 300
SYNC_TIMEOUT = 3600


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
                # Use SDK's bucket.ls with folder_to_list for recent b2sdk versions
                # and include all versions (latest_only=False)
                try:
                    iterator = bucket.ls(folder_to_list=prefix, latest_only=False, recursive=True)
                except TypeError:
                    # older/newer signatures: try common alternatives
                    iterator = bucket.ls(prefix=prefix, recursive=True)

                for item in iterator:
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

        # CLI fallback: use `b2 ls --recursive --versions --json` which prints
        # JSON objects per line. Include prefix as folder filter when provided.
        versions = []
        cmd = ['b2', 'ls', '--recursive', '--versions', '--json', str(bucket_name)]
        if prefix:
            cmd.append(str(prefix))

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=LIST_TIMEOUT)  # nosec B603 B607
            if result.returncode != 0:
                LOG.error('b2 ls --versions failed: %s', result.stderr)
                raise RuntimeError('b2 CLI failed')
        except Exception:
            LOG.exception('Exception invoking b2 ls --versions')
            raise

        # Parse newline-delimited JSON objects
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                LOG.debug('Skipping non-JSON line from b2 ls output: %s', line)
                continue

            # b2 ls --json emits objects describing files; normalize to our shape
            # Handle files array first
            if 'files' in obj and isinstance(obj.get('files'), list):
                for f in obj['files']:
                    fname = f.get('fileName') or f.get('name')
                    fid = f.get('fileId') or f.get('id')
                    uts = f.get('uploadTimestamp') or f.get('time')
                    action = f.get('action', 'upload')
                    versions.append({
                        'fileName': fname,
                        'fileId': fid,
                        'uploadTimestamp': uts,
                        'action': action,
                    })
            # Handle single file object
            elif 'fileName' in obj or 'name' in obj:
                fname = obj.get('fileName', obj.get('name'))
                versions.append({
                    'fileName': fname,
                    'fileId': obj.get('id') or obj.get('fileId'),
                    'uploadTimestamp': obj.get('uploadTimestamp') or obj.get('time'),
                    'action': obj.get('action', 'upload')
                })

        return versions

    def download_file_version(self, file_id, dest_path):
        if self._use_sdk:
            try:
                downloaded = self._api.download_file_by_id(file_id)
                # Preferred: DownloadedFile.save_to(dest_path)
                if hasattr(downloaded, 'save_to'):
                    downloaded.save_to(dest_path)
                    return True

                # If the returned object is file-like, read bytes and write out
                if hasattr(downloaded, 'read'):
                    try:
                        with open(dest_path, 'wb') as fh:
                            fh.write(downloaded.read())
                        return True
                    except Exception:
                        LOG.exception('Failed writing file-like download to disk')

                # Some SDKs may expose get_bytes / get_contents
                if hasattr(downloaded, 'get_bytes'):
                    try:
                        data = downloaded.get_bytes()
                        with open(dest_path, 'wb') as fh:
                            fh.write(data)
                        return True
                    except Exception:
                        LOG.exception('Failed writing get_bytes() result to disk')

                LOG.error('Downloaded object has no supported save method')
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
            had_failure = False
            for root, _dirs, files in os.walk(watch_dir):
                for fname in files:
                    local_path = os.path.join(root, fname)
                    rel = os.path.relpath(local_path, watch_dir).replace('\\', '/')
                    b2_dest = f"{b2_prefix}/{rel}"
                    try:
                        ok = self.upload_file(bucket_name, local_path, b2_dest)
                        if not ok:
                            had_failure = True
                            LOG.error('Failed uploading during SDK sync: %s', local_path)
                    except Exception:
                        had_failure = True
                        LOG.exception('Failed uploading during SDK sync: %s', local_path)
            return not had_failure

        # CLI fallback: call `b2 sync`
        cmd = ['b2', 'sync', str(watch_dir), f"b2://{bucket_name}/{b2_prefix}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=SYNC_TIMEOUT)  # nosec B603 B607
            if result.returncode != 0:
                LOG.error('b2 sync failed: %s', result.stderr)
                return False
            return True
        except Exception:
            LOG.exception('Exception invoking b2 sync')
            return False
