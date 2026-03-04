import os
import json
from collections.abc import Mapping

CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')


def load_config(path=None):
    """Load JSON config. If path is provided, use it, otherwise default CONFIG_PATH.

    Ensures the parsed JSON is a mapping and raises a TypeError if not.
    """
    cfg_path = path or CONFIG_PATH
    with open(cfg_path, 'r') as f:
        data = json.load(f)

    if not isinstance(data, Mapping):
        raise TypeError(f"Config file {cfg_path} must contain a JSON object (mapping)")

    return data
