"""Shared names for the File -> Open example.

A file proc provides two functions on the hub:

- ``files.list``: args optional ``path`` (a prefix to filter on). Result:
  ``{"provider": label, "files": [{name, uri, size, mtime, mime}]}``. File -> Open asks every
  provider of it, local procs and remote ones the hub found alike, and lists each answer as it
  arrives.
- ``files.read``: args ``file`` (a name as ``files.list`` gave it). Result: ``{name, mime, size,
  encoding, content}``, where ``encoding`` is ``utf-8`` (``content`` is the text) or ``base64``
  (anything that isn't UTF-8 text). The app asks the provider that listed the file.
"""

import base64
import mimetypes

FUNCTION = "files.list"
READ = "files.read"
MAX_READ = 1 << 20  # files.read refuses files larger than this many bytes
MAX_NAME = 1024

SCHEMA = """{
  "args": {"type": "object", "properties": {"path": {"type": "string"}},
           "additionalProperties": false},
  "result": {"type": "object", "required": ["provider", "files"],
             "properties": {"provider": {"type": "string"},
                            "files": {"type": "array", "items": {
                              "type": "object", "required": ["name", "uri"]}}}}
}"""
READ_SCHEMA = """{
  "args": {"type": "object", "required": ["file"], "properties": {"file": {"type": "string"}},
           "additionalProperties": false},
  "result": {"type": "object", "required": ["name", "mime", "size", "encoding", "content"],
             "properties": {"name": {"type": "string"}, "mime": {"type": "string"},
                            "size": {"type": "integer"},
                            "encoding": {"enum": ["utf-8", "base64"]},
                            "content": {"type": "string"}}}
}"""


def mime_of(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def check_name(name):
    """files.read's one argument, validated: a non-empty name."""
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        raise ValueError(f"file must be a non-empty string of at most {MAX_NAME} characters")
    return name


def check_size(name, size):
    if size > MAX_READ:
        raise ValueError(f"{name} is {size} bytes; files.read returns at most {MAX_READ}")


def file_content(name, data):
    """The files.read result for ``data``, the bytes of file ``name``."""
    check_size(name, len(data))
    try:
        encoding, content = "utf-8", data.decode("utf-8")
    except UnicodeDecodeError:
        encoding, content = "base64", base64.b64encode(data).decode("ascii")
    return {"name": name, "mime": mime_of(name), "size": len(data), "encoding": encoding,
            "content": content}


def content_bytes(answer):
    """The bytes a files.read answer carries."""
    if answer["encoding"] == "base64":
        return base64.b64decode(answer["content"])
    return answer["content"].encode("utf-8")
