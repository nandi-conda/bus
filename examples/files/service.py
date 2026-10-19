"""The file proc as an HTTPS service: serves org.latha.agentbus.files.list, .read, .write, .delete,
.mkdir, .rmdir and .rename over XRPC on the objects in an S3 bucket (the project's MinIO on Modal). This is what runs on Modal; hubs find it
through the relay and File -> Open lists its files next to the local ones.

    AGENTBUS_FILES_S3       S3 endpoint, e.g. https://codegod100--minio-web.modal.run (required)
    AGENTBUS_FILES_BUCKET   bucket to list (default agentbus-files; created if missing)
    AGENTBUS_FILES_LABEL    provider name File -> Open shows (default "cloud")
    MINIO_ROOT_USER / MINIO_ROOT_PASSWORD   S3 access key / secret (Modal secret minio-root)

Identity works as in weather_service.py: with $AGENTBUS_PDS it enrolls as $AGENTBUS_HANDLE
(default "filebox"; rookery reserves "files"), writes its provider record (collection org.latha.agentbus.files, rkey self)
naming $AGENTBUS_ENDPOINT, and only answers callers presenting a service JWT addressed to its DID."""

import io
import json
import os
import sys
import threading
import time
from urllib.parse import urlsplit

from agentbus import rookery, xrpc
from minio import Minio
from minio.commonconfig import CopySource
from minio.error import S3Error

from common import MAX_READ, check_name, check_path, check_size, content_bytes, file_content, mime_of

MAX_FILES = 1000  # files.list answers at most this many; narrow with path


def env(name, default=None):
    value = os.environ.get(name, default)
    if not value:
        sys.exit(f"files: {name} is required")
    return value


s3_url = urlsplit(env("AGENTBUS_FILES_S3"))
bucket = env("AGENTBUS_FILES_BUCKET", "agentbus-files")
label = env("AGENTBUS_FILES_LABEL", "cloud")
s3 = Minio(s3_url.netloc, access_key=env("MINIO_ROOT_USER"), secret_key=env("MINIO_ROOT_PASSWORD"),
           secure=s3_url.scheme == "https")

if not s3.bucket_exists(bucket):
    s3.make_bucket(bucket)

files = xrpc.Service("files")
MISSING = ("NoSuchKey", "NoSuchObject", "InvalidObjectName")


# Folders are prefixes. An empty one (from files.mkdir) is a zero-byte marker object "<dir>/".
def entry(obj):
    return {"name": obj.object_name, "uri": f"s3://{bucket}/{obj.object_name}", "size": obj.size,
            "mtime": int(obj.last_modified.timestamp()), "mime": mime_of(obj.object_name)}


def objects(prefix):
    """Up to MAX_FILES objects under prefix; a folder past that is refused rather than cut short."""
    out = []
    for obj in s3.list_objects(bucket, prefix=prefix or None, recursive=True):
        if len(out) == MAX_FILES:
            raise ValueError(f"more than {MAX_FILES} files under {prefix!r}")
        out.append(obj)
    return out


def stat(name):
    try:
        return s3.stat_object(bucket, name)
    except S3Error as e:
        if e.code in MISSING:
            return None
        raise


def is_dir(name):
    return next(iter(s3.list_objects(bucket, prefix=name + "/")), None) is not None


@files.method("list")
def list_files(path=""):
    if not isinstance(path, str) or len(path) > 1024:
        raise ValueError("path must be a string of at most 1024 characters")
    out, dirs = [], set()
    for obj in s3.list_objects(bucket, prefix=path or None, recursive=True):
        if len(out) == MAX_FILES:
            break
        name = obj.object_name
        parts = name.rstrip("/").split("/")
        dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        if name.endswith("/"):
            dirs.add(name.rstrip("/"))
        else:
            out.append(entry(obj))
    return {"provider": label, "files": out, "dirs": sorted(d for d in dirs if d.startswith(path))}


@files.method("read")
def read_file(file):
    name = file
    try:
        resp = s3.get_object(bucket, check_name(name))  # one round trip to S3: no stat first
    except S3Error as e:
        if e.code in MISSING:
            raise ValueError(f"no file {name!r} here") from None
        raise
    try:
        check_size(name, int(resp.headers.get("Content-Length", 0)))
        data = resp.read(MAX_READ + 1)
    finally:
        resp.close()
        resp.release_conn()
    return file_content(name, data)


@files.method("write")
def write_file(file, encoding, content):
    data = content_bytes({"encoding": encoding, "content": content})
    check_size(check_path(file), len(data))
    if is_dir(file):
        raise ValueError(f"{file!r} is a folder")
    s3.put_object(bucket, file, io.BytesIO(data), len(data), content_type=mime_of(file))
    return {"name": file, "uri": f"s3://{bucket}/{file}", "size": len(data), "mtime": int(time.time()),
            "mime": mime_of(file)}


@files.method("delete")
def delete_file(file):
    if stat(check_path(file)) is None:
        raise ValueError(f"no file {file!r} here")
    s3.remove_object(bucket, file)
    return {"name": file}


@files.method("mkdir")
def make_dir(dir):
    if stat(check_path(dir)) is not None or is_dir(dir):
        raise ValueError(f"{dir!r} already exists")
    s3.put_object(bucket, dir + "/", io.BytesIO(b""), 0)
    return {"name": dir}


@files.method("rmdir")
def remove_dir(dir):
    inside = [o.object_name for o in objects(check_path(dir) + "/")]
    if not inside:
        raise ValueError(f"no folder {dir!r} here")
    if inside != [dir + "/"]:
        raise ValueError(f"folder {dir!r} is not empty")
    s3.remove_object(bucket, dir + "/")
    return {"name": dir}


@files.method("rename")
def rename(file, to):
    """A file is copied then removed; a folder moves every object under it the same way."""
    check_path(file), check_path(to)
    if is_dir(to):
        raise ValueError(f"{to!r} is a folder")
    if stat(file) is not None:
        moves = [(file, to)]
    elif is_dir(file):
        if (to + "/").startswith(file + "/"):
            raise ValueError(f"can't move {file!r} into itself")
        if stat(to) is not None:
            raise ValueError(f"{to!r} is a file")
        moves = [(o.object_name, to + o.object_name[len(file):]) for o in objects(file + "/")]
    else:
        raise ValueError(f"no file {file!r} here")
    for src, dst in moves:
        s3.copy_object(bucket, dst, CopySource(bucket, src))
    for src, _ in moves:
        s3.remove_object(bucket, src)
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
print(f"files: listing s3 bucket {bucket} on {s3_url.netloc}, listening on :{port}", flush=True)
files.serve(port, did=did, allow=trust)
