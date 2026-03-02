import os
import sys
import json
import time
import logging
import subprocess  # nosec B404
import threading
import queue
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("backup_daemon.log"),
        logging.StreamHandler()
    ]
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

def load_config():
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)


def validate_config_for_daemon(config):
    """Ensure required config keys are present for the daemon to start."""
    required = ['watch_dir', 'bucket_name', 'b2_path_prefix']
    missing = [k for k in required if k not in config or not config.get(k)]
    if missing:
        logging.error(f"Missing required config keys for daemon: {', '.join(missing)}")
        sys.exit(1)

class BackupHandler(FileSystemEventHandler):
    def __init__(self, upload_queue, watch_dir, b2_prefix):
        super().__init__()
        self.upload_queue = upload_queue
        self.watch_dir = watch_dir
        self.b2_prefix = b2_prefix

    def process_event(self, event):
        if event.is_directory:
            return
        
        filepath = event.src_path
        # Calculate relative path to construct b2 destination
        try:
            rel_path = os.path.relpath(filepath, self.watch_dir)
            b2_dest = f"{self.b2_prefix}/{rel_path}".replace("\\", "/") # Ensure forward slashes
            
            self.upload_queue.put({
                'action': 'upload',
                'local_path': filepath,
                'b2_dest': b2_dest
            })
        except ValueError as e:
            logging.error(f"Error calculating relative path for {filepath}: {e}")

    def on_created(self, event):
        logging.info(f"File created: {event.src_path}")
        self.process_event(event)

    def on_modified(self, event):
        logging.info(f"File modified: {event.src_path}")
        self.process_event(event)
        
    def on_moved(self, event):
        logging.info(f"File moved from {event.src_path} to {event.dest_path}")
        if not event.is_directory:
            # We treat a move as a new creation at the destination
            filepath = event.dest_path
            try:
                dest_real = os.path.realpath(filepath)
                watch_real = os.path.realpath(self.watch_dir)
                # ensure dest is under watch_dir
                if os.path.commonpath([watch_real, dest_real]) != watch_real:
                    logging.warning(f"Moved destination outside watch_dir, skipping upload: {event.dest_path}")
                    return

                rel_path = os.path.relpath(dest_real, self.watch_dir)
                b2_dest = f"{self.b2_prefix}/{rel_path}".replace("\\", "/")

                self.upload_queue.put({
                    'action': 'upload',
                    'local_path': dest_real,
                    'b2_dest': b2_dest
                })
            except ValueError as e:
                logging.error(f"Error calculating relative path for {filepath}: {e}")
            except Exception as e:
                logging.exception(f"Error validating moved path containment: {e}")

def upload_worker(upload_queue, bucket_name):
    """Processes uploads from the queue with exponential backoff retries."""
    while True:
        task = upload_queue.get()
        if task is None:
            break
            
        local_path = task['local_path']
        b2_dest = task['b2_dest']
        
        if not os.path.exists(local_path):
            logging.warning(f"File {local_path} no longer exists. Skipping upload.")
            upload_queue.task_done()
            continue

        max_retries = 5
        base_delay = 2
        
        for attempt in range(max_retries):
            try:
                logging.info(f"Uploading {local_path} to {bucket_name}/{b2_dest} (Attempt {attempt+1}/{max_retries})")

                # Execute B2 upload-file CLI command
                subprocess.run(  # nosec B603 # NOSONAR
                    ['b2', 'upload-file', str(bucket_name), str(local_path), str(b2_dest)],
                    capture_output=True,
                    text=True,
                    check=True
                )
                logging.info(f"Successfully uploaded {local_path}")
                break

            except subprocess.CalledProcessError as e:
                logging.error(f"Upload failed for {local_path}. Exit code: {e.returncode}")
                logging.error(f"STDOUT: {e.stdout}")
                logging.error(f"STDERR: {e.stderr}")

            except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
                logging.exception(f"Exception during upload for bucket={bucket_name}, local_path={local_path}, b2_dest={b2_dest}: {e}")

            # Retry logic (same for all exceptions above)
            if attempt < max_retries - 1:
                sleep_time = base_delay * (2 ** attempt)
                logging.info(f"Retrying in {sleep_time} seconds...")
                time.sleep(sleep_time)
            else:
                logging.error(f"Max retries reached for {local_path}. Giving up.")

        upload_queue.task_done()

def daily_integrity_scan(config):
    """Runs a b2 sync operation once a day to ensure no files were missed."""
    bucket_name = config['bucket_name']
    watch_dir = config['watch_dir']
    b2_prefix = config['b2_path_prefix']
    
    while True:
        # Sleep for 24 hours between scans
        time.sleep(24 * 60 * 60)
        
        logging.info("Starting automated daily integrity scan using b2 sync...")
        try:
            b2_dest = f"b2://{bucket_name}/{b2_prefix}"
            subprocess.run(  # nosec B603 # NOSONAR
                ['b2', 'sync', str(watch_dir), str(b2_dest)],
                capture_output=True,
                text=True,
                check=True
            )
            logging.info("Daily integrity scan completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Integrity scan failed. Exit code: {e.returncode}")
            logging.error(f"STDERR: {e.stderr}")
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
            logging.exception(f"Exception during integrity scan for bucket={bucket_name}: {e}")

def main():
    try:
        config = load_config()
    except Exception as e:
        logging.error(f"Failed to load config.json: {e}")
        return

    watch_dir = config['watch_dir']
    
    # Initialize queue and worker
    upload_queue = queue.Queue()
    worker_thread = threading.Thread(target=upload_worker, args=(upload_queue, config['bucket_name']), daemon=True)
    worker_thread.start()
    
    # Initialize integrity scanner
    scanner_thread = threading.Thread(target=daily_integrity_scan, args=(config,), daemon=True)
    scanner_thread.start()

    # Initialize Watchdog
    event_handler = BackupHandler(upload_queue, watch_dir, config['b2_path_prefix'])
    observer = Observer()
    try:
        observer.schedule(event_handler, watch_dir, recursive=True)
    except Exception as e:
        logging.error(f"Watch directory error ({watch_dir}): {e}. Please create it or fix the config.")
        sys.exit(1)
    observer.start()
    
    logging.info(f"Started monitoring {watch_dir}...")
    logging.info(f"Target B2 Bucket: {config['bucket_name']}/{config['b2_path_prefix']}")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("Stopping daemon...")
        observer.stop()
        upload_queue.put(None) # Signal worker to stop
        
    observer.join()
    worker_thread.join()

if __name__ == "__main__":
    main()
