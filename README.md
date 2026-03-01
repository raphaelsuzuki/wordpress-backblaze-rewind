# WordPress Backblaze Rewind

A lightweight, reliable, and real-time backup system for WordPress `wp-content/uploads` using Backblaze B2 versioned buckets.

## Overview
This system provides:
1. A **real-time backup daemon** that monitors the uploads directory and instantly syncs new/modified files to B2.
2. An automatic **daily integrity scan** that ensures no files are missed securely via `b2 sync`.
3. A **point-in-time restore tool** which allows you to seamlessly restore your media to exactly how it was on a specific date (powered by B2 bucket versioning).

## Prerequisites
- Python 3.7+
- A Backblaze B2 account
- A bucket with **Object Versioning** enabled (configured via lifecycle rules).
  - Recommended Rule: **Keep prior versions for 30 days** (or 60/90 depending on your needs).

## Setup Instructions

### 1. Install Dependencies
```bash
pip3 install -r requirements.txt
```

### 2. Configure B2 CLI Authorization
Run the B2 CLI tool to authorize your account. The scripts rely on the B2 CLI's native configuration.
```bash
b2 account authorize <applicationKeyId> <applicationKey>
```
*Note: Make sure to authorize the B2 CLI under the same user that will run the daemon.*

### 3. Update `config.json`
Edit `config.json` with your details:
```json
{
    "bucket_name": "your-bucket-name",
    "watch_dir": "/var/www/html/wp-content/uploads",
    "b2_path_prefix": "uploads"
}
```

### 4. Install the Daemon (systemd)
1. Copy the service template:
   `sudo cp b2-backup-daemon.service /etc/systemd/system/`
2. Edit `/etc/systemd/system/b2-backup-daemon.service` to reflect your actual installation path and run user (e.g. `www-data` or your WP user).
3. Reload systemd, enable, and start the daemon:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable b2-backup-daemon
   sudo systemctl start b2-backup-daemon
   ```
4. Check the status:
   `sudo systemctl status b2-backup-daemon`

## Point-In-Time Restores
To restore your media folder to exactly how it looked on a particular date:

```bash
python3 restore_tool.py "2024-01-15T00:00:00Z" /tmp/my-wp-restore
```
*Note: The script filters the B2 bucket version history and picks the latest version of every file that existed up to the timestamp you provide. Deleted files will be ignored.*

Once the script completes downloading, you can use `rsync` or simply move the files back to your live WordPress installation.
