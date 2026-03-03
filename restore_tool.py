import os
import sys
import json
import logging
import argparse
import subprocess  # nosec B404
import dateutil.parser
import datetime as dt
import time

from config_utils import load_config

# Timeouts (seconds)
SUBPROCESS_TIMEOUT_LIST = 120
SUBPROCESS_TIMEOUT_DOWNLOAD = 300

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def list_file_versions(bucket_name, prefix):
    """
    List all file versions from B2 bucket with specific prefix.
    Handles pagination.
    """
    versions = []
    start_file_name = None
    start_file_id = None
    
    logging.info(f"Fetching file versions for b2://{bucket_name}/{prefix} ...")
    
    while True:
        # B2 CLI command to list all file versions
        cmd = ['b2', 'list-file-versions', bucket_name]
        if start_file_name:
            cmd.extend(['--startFileName', str(start_file_name)])
            if start_file_id:
                cmd.extend(['--startFileId', str(start_file_id)])
                
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_LIST)  # nosec B603 B607 # NOSONAR
            if result.returncode != 0:
                logging.error(f"Failed to list file versions: {result.stderr}")
                sys.exit(1)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            logging.exception(f"Exception invoking b2 list-file-versions for bucket={bucket_name}: {e}")
            sys.exit(1)
            
        try:
            data = json.loads(result.stdout)
            files = data.get('files', [])
            
            # Filter files by prefix and add to our list
            filtered_files = [f for f in files if f['fileName'].startswith(prefix)]
            versions.extend(filtered_files)
            
            start_file_name = data.get('nextFileName')
            start_file_id = data.get('nextFileId')
            
            # If no more files, break pagination loop
            if not start_file_name:
                break
                
        except json.JSONDecodeError as e:
            logging.exception("Failed to parse JSON. This tool expects B2 CLI JSON output format.")
            logging.error(f"CLI Output chunk: {result.stdout[:200]}")
            sys.exit(1)
            
    return versions

def restore(target_date_str, restore_dir):
    config = load_config()
    # Validate required config keys
    missing = [k for k in ('bucket_name', 'b2_path_prefix') if k not in config or not config.get(k)]
    if missing:
        logging.error(f"Missing required config key(s): {', '.join(missing)}")
        sys.exit(1)

    bucket_name = config['bucket_name']
    prefix = config['b2_path_prefix']
    
    # Parse target date and convert to milliseconds timestamp
    try:
        target_dt = dateutil.parser.parse(target_date_str)
        # Assume UTC if timezone isn't specified
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=dt.timezone.utc)

        target_ts = target_dt.timestamp() * 1000
    except Exception as e:
        logging.exception(f"Failed to parse target date '{target_date_str}': {e}")
        sys.exit(1)
        
    logging.info(f"Target date for restore: {target_dt} (Timestamp: {target_ts})")
    
    versions = list_file_versions(bucket_name, prefix)
    
    # Group file versions by their unique fileName
    file_history = {}
    for v in versions:
        name = v['fileName']
        if name not in file_history:
            file_history[name] = []
        file_history[name].append(v)
        
    to_download = []
    
    # For each file, pick the version closest to but not after target_date
    for name, history in file_history.items():
        # Sort history by uploadTimestamp descending (newest first)
        history.sort(key=lambda x: x['uploadTimestamp'], reverse=True)
        
        selected_version = None
        for v in history:
            if v['uploadTimestamp'] <= target_ts:
                selected_version = v
                break
                
        if selected_version:
            # Only download if the version wasn't a deletion (hide)
            if selected_version.get('action') == 'upload':
                to_download.append(selected_version)
            else:
                logging.debug(f"File {name} was hidden/deleted at target date. Skipping.")
        else:
            logging.debug(f"File {name} did not exist before target date.")
            
    logging.info(f"Found {len(to_download)} files to restore out of {len(file_history)} unique files.")
    
    abs_restore_dir = os.path.abspath(restore_dir)
    if not os.path.exists(abs_restore_dir):
        os.makedirs(abs_restore_dir, exist_ok=True)
        
    # Download the selected versions with retry/backoff
    failures = []
    for v in to_download:
        file_id = v['fileId']
        file_name = v['fileName']
        
        # Calculate the local file path
        rel_name = file_name
        if rel_name.startswith(prefix + '/'):
            rel_name = rel_name[len(prefix)+1:]

        # Compute normalized absolute path and ensure it is within restore_dir
        candidate = os.path.normpath(os.path.join(abs_restore_dir, rel_name))
        try:
            if os.path.commonpath([abs_restore_dir, candidate]) != abs_restore_dir:
                logging.error(f"Skipping restore for {file_name}: resolved path outside restore_dir -> {candidate}")
                continue
        except Exception as e:
            logging.exception(f"Path containment check failed for {file_name}: {e}")
            continue

        local_path = candidate
        local_dir = os.path.dirname(local_path)
        if not os.path.exists(local_dir):
            os.makedirs(local_dir, exist_ok=True)
            
        logging.info(f"Downloading {rel_name} (version: {file_id})")

        cmd = ['b2', 'download-file-by-id', str(file_id), str(local_path)]
        max_retries = 5
        base_delay = 2
        success = False

        for attempt in range(max_retries):
            try:
                download_result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_DOWNLOAD)  # nosec B603 B607 # NOSONAR
                if download_result.returncode == 0:
                    logging.info(f"Downloaded {file_name} successfully")
                    success = True
                    break
                else:
                    logging.error(f"Failed to download {file_name} (rc={download_result.returncode}): {download_result.stderr}")
            except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
                logging.exception(f"Exception during download attempt {attempt+1} for {file_name}: {e}")

            if attempt < max_retries - 1:
                sleep_time = base_delay * (2 ** attempt)
                logging.info(f"Retrying download of {file_name} in {sleep_time} seconds...")
                time.sleep(sleep_time)

        if not success:
            logging.error(f"Exhausted retries downloading {file_name}")
            failures.append(file_name)
    if failures:
        logging.error(f"Restore completed with failures for {len(failures)} file(s): {failures}")
        sys.exit(1)

    logging.info("Restore process completed successfully.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore WP media from Backblaze B2 by point-in-time date.")
    parser.add_argument('target_date', help='Target date to restore to (e.g., "2024-01-15T12:00:00Z" or "2024-01-15")')
    parser.add_argument('restore_dir', help='Local directory to restore files into (e.g., /tmp/wp-restore)')
    
    args = parser.parse_args()
    restore(args.target_date, args.restore_dir)
