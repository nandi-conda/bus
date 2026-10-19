"""The file proc as an HTTPS service: serves org.latha.agentbus.files.list over XRPC, listing the
objects in an S3 bucket (the project's MinIO on Modal). This is what runs on Modal; hubs find it
through the relay and File -> Open lists its files next to the local ones.

    AGENTBUS_FILES_S3       S3 endpoint, e.g. https://codegod100--minio-web.modal.run (required)
    AGENTBUS_FILES_BUCKET   bucket to list (default agentbus-files; created if missing)
    AGENTBUS_FILES_LABEL    provider name File -> Open shows (default "cloud")
    MINIO_ROOT_USER / MINIO_ROOT_PASSWORD   S3 access key / secret (Modal secret minio-root)

Identity works as in weather_service.py: with $AGENTBUS_PDS it enrolls as $AGENTBUS_HANDLE
(default "files"), writes its provider record (collection org.latha.agentbus.files, rkey self)
naming $AGENTBUS_ENDPOINT, and only answers callers presenting a service JWT addressed to its DID."""

import mimetypes
import os
import sys
import threading
from urllib.parse import urlsplit

from agentbus import rookery, xrpc
from minio import Minio

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


@files.method("list")
def list_files(path=""):
    if not isinstance(path, str) or len(path) > 1024:
        raise ValueError("path must be a string of at most 1024 characters")
    out = []
    for obj in s3.list_objects(bucket, prefix=path or None, recursive=True):
        if len(out) == MAX_FILES:
            break
        out.append({"name": obj.object_name, "uri": f"s3://{bucket}/{obj.object_name}", "size": obj.size,
                    "mtime": int(obj.last_modified.timestamp()),
                    "mime": mimetypes.guess_type(obj.object_name)[0] or "application/octet-stream"})
    return {"provider": label, "files": out}


port = int(os.environ.get("PORT", "8000"))
did = None
if os.environ.get("AGENTBUS_PDS"):
    endpoint = env("AGENTBUS_ENDPOINT")
    ident = rookery.Identity.from_env("files")
    did = ident.did

    def publish():
        # Off the startup path: a cold start answers its first call without waiting on the PDS.
        ident.put_record(files.nsid, "self", files.record(endpoint))
        print(f"files: {ident.handle} ({did}) provides {files.nsid} at {endpoint}", flush=True)

    threading.Thread(target=publish, name="publish-record", daemon=True).start()
else:
    print("files: no AGENTBUS_PDS, serving without identity: open to anyone, not discoverable",
          flush=True)

trust = {d for d in os.environ.get("AGENTBUS_TRUST", "").split(",") if d} or None
print(f"files: listing s3 bucket {bucket} on {s3_url.netloc}, listening on :{port}", flush=True)
files.serve(port, did=did, allow=trust)
