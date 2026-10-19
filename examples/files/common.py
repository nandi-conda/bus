"""Shared names for the File -> Open example.

A file proc provides two functions on the hub:

- ``files.list``: args optional ``path`` (a prefix to filter on). Result:
  ``{"provider": label, "files": [{name, uri, size, mtime, mime}]}``. File -> Open asks every
  provider of it, local procs and remote ones the hub found alike, and lists each answer as it
  arrives. A provider with folders also answers ``dirs``: every folder name, empty ones included.
- ``files.read``: args ``file`` (a name as ``files.list`` gave it). Result: ``{name, mime, size,
  encoding, content}``, where ``encoding`` is ``utf-8`` (``content`` is the text) or ``base64``
  (anything that isn't UTF-8 text). The app asks the provider that listed the file.

A provider that manages its files also provides these (a read-only one leaves them out):

- ``files.write``: args ``file``, ``encoding``, ``content`` (as files.read returns them). Creates or
  replaces the file, making its folders. Result: the file's ``files.list`` entry.
- ``files.delete``: args ``file``. Result: ``{name}``.
- ``files.mkdir`` / ``files.rmdir``: args ``dir``. rmdir refuses a folder that isn't empty.
  Result: ``{name}``.
- ``files.rename``: args ``file``, ``to`` (a file or a folder; ``to`` is replaced if it is a file).
  Result: ``{name}``, the new name.

Names are relative paths: ``/``-separated, no empty, ``.`` or ``..`` segments.
"""

import base64
import mimetypes

FUNCTION = "files.list"
READ = "files.read"
WRITE, DELETE, MKDIR, RMDIR, RENAME = "files.write", "files.delete", "files.mkdir", "files.rmdir", "files.rename"
MANAGE = (WRITE, DELETE, MKDIR, RMDIR, RENAME)
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
_NAME = {"type": "string", "minLength": 1, "maxLength": 1024}
_DONE = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}


def _schema(args, result=_DONE):
    import json

    return json.dumps({"args": {"type": "object", "required": sorted(args), "properties": args,
                                "additionalProperties": False}, "result": result})


MANAGE_SCHEMAS = {
    WRITE: _schema({"file": _NAME, "encoding": {"enum": ["utf-8", "base64"]}, "content": {"type": "string"}},
                   {"type": "object", "required": ["name", "uri"]}),
    DELETE: _schema({"file": _NAME}),
    MKDIR: _schema({"dir": _NAME}),
    RMDIR: _schema({"dir": _NAME}),
    RENAME: _schema({"file": _NAME, "to": _NAME}),
}


def mime_of(name):
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def check_name(name):
    """files.read's one argument, validated: a non-empty name."""
    if not isinstance(name, str) or not name or len(name) > MAX_NAME:
        raise ValueError(f"file must be a non-empty string of at most {MAX_NAME} characters")
    return name


def check_path(name):
    """A name a provider may create or change: a relative path with no empty, ``.`` or ``..``
    segments (so it can't reach outside the provider), at most MAX_NAME characters."""
    check_name(name)
    parts = name.split("/")
    if "\0" in name or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"{name!r} is not a relative path of plain names")
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
    """The bytes a files.read answer (or files.write's args) carries."""
    if answer["encoding"] == "base64":
        return base64.b64decode(answer["content"], validate=True)
    if answer["encoding"] != "utf-8":
        raise ValueError("encoding must be utf-8 or base64")
    return answer["content"].encode("utf-8")


def write_args(data):
    """files.write's encoding and content for ``data``, the bytes to write."""
    answer = file_content("", data)
    return {"encoding": answer["encoding"], "content": answer["content"]}
