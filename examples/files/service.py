"""The file proc as an HTTPS service: serves org.latha.agentbus.files.list, .read, .write, .delete,
.mkdir, .rmdir and .rename over XRPC on the files in one folder. This is what runs on Modal, where
the folder is a Modal Volume of its own; hubs find it through the relay and File -> Open and the
mount list its files next to the local ones.

    AGENTBUS_FILES_DIR      the folder to serve (required; on Modal the Volume agentbus-files-data
                            mounted at /files). Created if missing.
    AGENTBUS_FILES_LABEL    provider name File -> Open shows (default "cloud")

One container serves at a time (max_containers=1), so it never reads another's stale view of
the Volume; Modal commits the Volume's changes in the background and when the container stops.

Identity works as in weather_service.py: with $AGENTBUS_PDS it enrolls as $AGENTBUS_HANDLE
(default "filebox"; rookery reserves "files"), writes its provider record (collection
org.latha.agentbus.files, rkey self) naming $AGENTBUS_ENDPOINT, and only answers callers
presenting a service JWT addressed to its DID."""

import json
import os
import sys
import threading
import time

from agentbus import rookery, xrpc

from common import content_bytes, file_content
from dirstore import DirStore

MAX_FILES = 1000  # files.list refuses more than this many; narrow with path


def env(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"files: {name} is required")
    return value


root = env("AGENTBUS_FILES_DIR")
label = env("AGENTBUS_FILES_LABEL", "cloud")
os.makedirs(root, exist_ok=True)
store = DirStore(root, uri=lambda name: f"agentbus-files:{name}", max_files=MAX_FILES)
files = xrpc.Service("files")


@files.method("list")
def list_files(path=""):
    if not isinstance(path, str) or len(path) > 1024:
        raise ValueError("path must be a string of at most 1024 characters")
    found, dirs = store.list(path)
    return {"provider": label, "files": found, "dirs": dirs}


@files.method("read")
def read_file(file):
    return file_content(file, store.read(file))


@files.method("write")
def write_file(file, encoding, content):
    return store.write(file, content_bytes({"encoding": encoding, "content": content}))


@files.method("delete")
def delete_file(file):
    store.delete(file)
    return {"name": file}


@files.method("mkdir")
def make_dir(dir):
    store.mkdir(dir)
    return {"name": dir}


@files.method("rmdir")
def remove_dir(dir):
    store.rmdir(dir)
    return {"name": dir}


@files.method("rename")
def rename(file, to):
    store.rename(file, to)
    return {"name": to}


port = int(os.environ.get("PORT", "8000"))
did = None
if os.environ.get("AGENTBUS_PDS"):
    endpoint = env("AGENTBUS_ENDPOINT")
    os.environ.setdefault("AGENTBUS_HANDLE", "filebox")
    # Signing in to rookery waits on the PDS, which may itself be waking from zero (~30 s).
    # Once enrolled, the DID is on the state volume, so serve at once and sign in behind it.
    saved = rookery.STATE_DIR / "files" / "identity.json"
    did = json.loads(saved.read_text())["did"] if saved.exists() else rookery.Identity.from_env("files").did

    def publish():
        while True:
            try:
                ident = rookery.Identity.from_env("files")
                ident.put_record(files.nsid, "self", files.record(endpoint))
                break
            except (OSError, rookery.RookeryError) as e:
                print(f"files: can't publish the provider record yet ({e}); retrying in 10s", flush=True)
                time.sleep(10)
        print(f"files: {ident.handle} ({did}) provides {files.nsid} at {endpoint}", flush=True)

    threading.Thread(target=publish, name="publish-record", daemon=True).start()
else:
    print("files: no AGENTBUS_PDS, serving without identity: open to anyone, not discoverable",
          flush=True)

trust = {d for d in os.environ.get("AGENTBUS_TRUST", "").split(",") if d} or None
print(f"files: serving {root}, listening on :{port}", flush=True)
files.serve(port, did=did, allow=trust)
