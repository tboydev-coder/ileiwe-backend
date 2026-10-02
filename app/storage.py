from pathlib import Path
from .core.config import get_settings


class Storage:
    def __init__(self):
        self.settings = get_settings()

    def local_path(self, key):
        root = Path(self.settings.local_storage_path).resolve()
        path = (root / key).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("Invalid storage key")
        return path

    def put(self, key, content, content_type):
        path = self.local_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def get(self, key):
        return self.local_path(key).read_bytes()
