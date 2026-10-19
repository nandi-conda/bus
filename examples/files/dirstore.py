"""A folder's files as a file proc serves them: what the directory proc (provider.py dir) and the
HTTPS files service (service.py, on a Modal Volume) both answer with. Every name is checked to
stay inside the folder, through ``..`` and symlinks alike."""

import os

from common import MAX_READ, check_name, check_path, check_size, mime_of


class DirStore:
    """The files under ``root``. ``uri(name)`` says where a file lives, for File -> Open's tooltip;
    a listing of more than ``max_files`` files is refused (None: no limit)."""

    def __init__(self, root, uri=None, max_files=None):
        self.root = os.path.realpath(root)
        self.uri = uri or (lambda name: "file://" + os.path.join(self.root, name))
        self.max_files = max_files

    def entry(self, full):
        st = os.stat(full)
        name = os.path.relpath(full, self.root)
        return {"name": name, "uri": self.uri(name), "size": st.st_size, "mtime": int(st.st_mtime),
                "mime": mime_of(name)}

    def list(self, path=""):
        """(files, dirs) whose names start with ``path``, skipping hidden folders and __pycache__."""
        files, dirs = [], []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            dirs.extend(os.path.relpath(os.path.join(dirpath, d), self.root) for d in dirnames)
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                if os.path.relpath(full, self.root).startswith(path):
                    if len(files) == self.max_files:
                        raise ValueError(f"more than {self.max_files} files; narrow the listing with path")
                    files.append(self.entry(full))
        return files, [d for d in dirs if d.startswith(path)]

    def under(self, name):
        """The real path of ``name``; one reaching outside root is refused."""
        path = os.path.realpath(os.path.join(self.root, check_name(name)))
        if path == self.root or os.path.commonpath([self.root, path]) != self.root:
            raise ValueError(f"no file {name!r} here")
        return path

    def target(self, name):
        """Where a new or changed ``name`` goes: its parent must resolve inside root."""
        check_path(name)
        parent = self.under(os.path.dirname(name)) if "/" in name else self.root
        return os.path.join(parent, os.path.basename(name))

    def read(self, name):
        path = self.under(name)
        if not os.path.isfile(path):
            raise ValueError(f"no file {name!r} here")
        check_size(name, os.path.getsize(path))
        with open(path, "rb") as f:
            return f.read(MAX_READ + 1)

    def write(self, name, data):
        """Create or replace ``name`` (atomically: a reader sees the old file or the new one)."""
        check_size(name, len(data))
        path = self.target(name)
        if os.path.isdir(path):
            raise ValueError(f"{name!r} is a folder")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.agentbus-{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        return self.entry(path)

    def delete(self, name):
        path = self.target(name)
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError(f"no file {name!r} here")
        os.remove(path)

    def mkdir(self, name):
        path = self.target(name)
        if os.path.lexists(path):
            raise ValueError(f"{name!r} already exists")
        os.makedirs(path)

    def rmdir(self, name):
        path = self.target(name)
        if not os.path.isdir(path) or os.path.islink(path):
            raise ValueError(f"no folder {name!r} here")
        if os.listdir(path):
            raise ValueError(f"folder {name!r} is not empty")
        os.rmdir(path)

    def rename(self, old, new):
        src, dst = self.target(old), self.target(new)
        if not os.path.lexists(src):
            raise ValueError(f"no file {old!r} here")
        if os.path.isdir(dst):
            raise ValueError(f"{new!r} is a folder")
        if os.path.isdir(src) and os.path.commonpath([src, dst]) == src:
            raise ValueError(f"can't move {old!r} into itself")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.replace(src, dst)
