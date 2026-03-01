import os
import sys
import json
import logging
import argparse
import subprocess  # nosec B404
import dateutil.parser

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def load_config():
    config_path = os.path.join(os.path.dirname(__file__), 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)

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
            cmd.extend(['--startFileName', start_file_name])
            if start_file_id:
                cmd.extend(['--startFileId', start_file_id])
                
        result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603 B607
        if result.returncode != 0:
            logging.error(f"Failed to list file versions: {result.stderr}")
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
                
        except json.JSONDecodeError:
            logging.error("Failed to parse JSON. This tool expects B2 CLI JSON output format.")
            logging.error(f"CLI Output chunk: {result.stdout[:200]}")
            sys.exit(1)
            
    return versions

def restore(target_date_str, restore_dir):
    config = load_config()
    bucket_name = config['bucket_name']
    prefix = config['b2_path_prefix']
    
    # Parse target date and convert to milliseconds timestamp
    try:
        target_dt = dateutil.parser.parse(target_date_str)
        # Assume local if timezone isn't specified, but fallback to naive
        if target_dt.tzinfo is None:
            # Assume UTC for safety if not specified
            import datetime as dt
            target_dt = target_dt.replace(tzinfo=dt.timezone.utc)
            
        target_ts = target_dt.timestamp() * 1000
    except Exception as e:
        logging.error(f"Failed to parse target date '{target_date_str}': {e}")
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
    
    if not os.path.exists(restore_dir):
        os.makedirs(restore_dir)
        
    # Download the selected versions
    for v in to_download:
        file_id = v['fileId']
        file_name = v['fileName']
        
        # Calculate the local file path
        rel_name = file_name
        if rel_name.startswith(prefix + '/'):
            rel_name = rel_name[len(prefix)+1:]
            
        local_path = os.path.join(restore_dir, rel_name)
        local_dir = os.path.dirname(local_path)
        
        if not os.path.exists(local_dir):
            os.makedirs(local_dir, exist_ok=True)
            
        logging.info(f"Downloading {rel_name} (version: {file_id})")
        
        # Download the specific version by ID
        cmd = ['b2', 'download-file-by-id', file_id, local_path]
        download_result = subprocess.run(cmd, capture_output=True, text=True)  # nosec B603 B607
        
        if download_result.returncode != 0:
            logging.error(f"Failed to download {file_name}: {download_result.stderr}")
            
    logging.info("Restore process completed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Restore WP media from Backblaze B2 by point-in-time date.")
    parser.add_argument('target_date', help='Target date to restore to (e.g., "2024-01-15T12:00:00Z" or "2024-01-15")')
    parser.add_argument('restore_dir', help='Local directory to restore files into (e.g., /tmp/wp-restore)')
    
    args = parser.parse_args()
    restore(args.target_date, args.restore_dir)
