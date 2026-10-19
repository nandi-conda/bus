"""Shared names for the File -> Open example.

The GUI broadcasts GetFiles(s request) on the session bus; every provider that hears it
broadcasts SendFiles(s request, s provider, as files) back. Nobody needs to know who else
is on the bus: the GUI lists whatever arrives for its request id.
"""

PATH = "/org/example/Files"
IFACE = "org.example.Files1"
