import os
import json

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

def load_config(path=None):
    """Load JSON config. If path is provided, use it, otherwise default CONFIG_PATH."""
    cfg_path = path or CONFIG_PATH
    with open(cfg_path, 'r') as f:
        return json.load(f)
