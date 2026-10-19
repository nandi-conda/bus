"""Shared names for the File -> Open example.

A file proc provides the function ``files.list`` on the hub. Args: optional ``path`` (a prefix
to filter on). Result: ``{"provider": label, "files": [{name, uri, size, mtime, mime}]}``.
File -> Open asks every provider of it, local procs and #bots procs the bridge imported alike,
and lists each answer as it arrives.
"""

FUNCTION = "files.list"
SCHEMA = """{
  "args": {"type": "object", "properties": {"path": {"type": "string"}},
           "additionalProperties": false},
  "result": {"type": "object", "required": ["provider", "files"],
             "properties": {"provider": {"type": "string"},
                            "files": {"type": "array", "items": {
                              "type": "object", "required": ["name", "uri"]}}}}
}"""
