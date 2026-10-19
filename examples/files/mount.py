"""The bus's file providers as a normal folder: a FUSE mount with one subfolder per provider.

    python mount.py [MOUNTPOINT]          mount (default ~/agentbus) and serve until Ctrl-C
    python mount.py --unmount [MOUNTPOINT]

Every files.list provider on the hub, local procs and the remote ones the hub found through the
relay alike, shows up as ``MOUNTPOINT/<provider>/``. Reading a file calls files.read on the provider
that listed it; saving one calls files.write when it's closed, and rm, mkdir, rmdir and mv call
files.delete, .mkdir, .rmdir and .rename. A provider that doesn't offer files.write is read-only
(EROFS). Files are at most 1 MiB (EFBIG past that), as files.read and files.write allow.

Listings are cached for TTL seconds, so a change made elsewhere shows up within that.
Needs libfuse3 (Arch: the fuse3 package), which mfusepy loads, and its fusermount3."""

import argparse
import ctypes.util
import errno
import os
import posixpath
import shutil
import stat
import subprocess
import sys
import threading
import time

if ctypes.util.find_library("fuse3"):  # mfusepy picks FUSE 2 first when both are installed
    os.environ.setdefault("FUSE_LIBRARY_NAME", "fuse3")

import agentbus
import mfusepy
from mfusepy import FuseOSError

from common import (DELETE, FUNCTION, MAX_READ, MKDIR, READ, RENAME, RMDIR, WRITE, content_bytes,
                    write_args)

TTL = 10  # seconds a provider's listing is trusted
FIND_EVERY = 5  # seconds between looks for providers joining or leaving the hub
CALL_TIMEOUT = 90  # past a remote proc's cold start on Modal, as the File -> Open GUI allows


def log(msg):
    print(f"mount: {msg}", file=sys.stderr, flush=True)


def os_error(e):
    """The errno for a failed bus call. Providers say what went wrong only in words (CallError's
    message), so this reads the phrases the file procs use."""
    if isinstance(e, FuseOSError):
        return e
    if isinstance(e, agentbus.NoProvider):
        return FuseOSError(errno.ENOENT)  # the provider left the hub
    if isinstance(e, TimeoutError):
        return FuseOSError(errno.ETIMEDOUT)
    text = str(e)
    for phrase, code in (("not empty", errno.ENOTEMPTY), ("already exists", errno.EEXIST),
                         ("is a folder", errno.EISDIR), ("into itself", errno.EINVAL),
                         ("returns at most", errno.EFBIG), ("no file", errno.ENOENT),
                         ("no folder", errno.ENOENT), ("not a relative path", errno.EINVAL)):
        if phrase in text:
            return FuseOSError(code)
    log(f"call failed: {text}")
    return FuseOSError(errno.EIO)


class Provider:
    """One files.list provider: its hub owner, whether it manages files, and its cached listing."""

    def __init__(self, owner, label, writable, answer):
        self.owner, self.label, self.writable = owner, label, writable
        self.lock = threading.Lock()  # one listing refresh at a time
        self.set_listing(answer)

    def set_listing(self, answer):
        self.files = {f["name"]: f for f in answer["files"]}
        self.dirs = set(answer.get("dirs", []))
        for name in self.files:
            parts = name.split("/")[:-1]
            self.dirs.update("/".join(parts[:i]) for i in range(1, len(parts) + 1))
        self.fetched = time.monotonic()


class Handle:
    """An open file: its bytes, loaded on first use, and whether they need writing back."""

    def __init__(self, provider, name, data=None, dirty=False):
        self.provider, self.name, self.data, self.dirty = provider, name, data, dirty
        self.lock = threading.Lock()


class BusFiles(mfusepy.Operations):
    use_ns = True

    def __init__(self, procs):
        self.procs = procs
        self.state = threading.Lock()  # guards providers and handles (``lock`` is a FUSE op)
        self.providers = {}  # label -> Provider
        self.handles = {}  # fh -> Handle
        self.next_fh = 1
        self.uid, self.gid = os.getuid(), os.getgid()
        self.started = time.time_ns()
        threading.Thread(target=self.find, name="find-providers", daemon=True).start()

    # ── Providers ───────────────────────────────────────────────────────────

    def call(self, provider, function, **args):
        try:
            return self.procs.call(function, timeout=CALL_TIMEOUT, provider=provider.owner, **args)
        except Exception as e:
            raise os_error(e) from None

    def find(self):
        """Follow the hub: add each provider as it appears (asking it for its listing, which also
        names it) and drop the ones that leave."""
        asking = set()
        while True:
            try:
                owners = {o for o, _ in self.procs.providers(FUNCTION)}
                writers = {o for o, _ in self.procs.providers(WRITE)}
            except agentbus.BusError as e:
                log(f"can't ask the hub for providers: {e}")
                owners, writers = None, set()
            if owners is not None:
                with self.state:
                    for label, p in list(self.providers.items()):
                        if p.owner not in owners:
                            log(f"{label} left")
                            del self.providers[label]
                        else:
                            p.writable = p.owner in writers
                    known = {p.owner for p in self.providers.values()} | asking
                for owner in owners - known:
                    asking.add(owner)
                    threading.Thread(target=self.add, args=(owner, owner in writers, asking),
                                     daemon=True).start()
            time.sleep(FIND_EVERY)

    def add(self, owner, writable, asking):
        try:
            answer = self.procs.call(FUNCTION, timeout=CALL_TIMEOUT, provider=owner)
        except Exception as e:
            log(f"{owner} didn't list its files: {e}")
            time.sleep(30)  # then find() asks it again
            asking.discard(owner)
            return
        base = answer["provider"].replace("/", "-") or owner
        with self.state:
            label, n = base, 1
            while label in self.providers:
                n += 1
                label = f"{base}-{n}"
            self.providers[label] = Provider(owner, label, writable, answer)
        asking.discard(owner)
        log(f"{label}: {len(answer['files'])} files{'' if writable else ' (read-only)'}")

    def listing(self, provider, fresh=False):
        with provider.lock:
            if fresh or time.monotonic() - provider.fetched > TTL:
                provider.set_listing(self.call(provider, FUNCTION))
        return provider

    def locate(self, path):
        """(provider, name within it) for a path; (None, "") is the root, (p, "") a provider."""
        parts = path.strip("/").split("/", 1)
        if not parts[0]:
            return None, ""
        with self.state:
            provider = self.providers.get(parts[0])
        if provider is None:
            raise FuseOSError(errno.ENOENT)
        return provider, parts[1] if len(parts) > 1 else ""

    def writable(self, path):
        provider, name = self.locate(path)
        if provider is None or not name:
            raise FuseOSError(errno.EPERM)  # the root and the provider folders are the hub's
        if not provider.writable:
            raise FuseOSError(errno.EROFS)
        return provider, name

    # ── Attributes and listings ─────────────────────────────────────────────

    def dir_attrs(self, mtime_ns=None):
        t = mtime_ns or self.started
        return {"st_mode": stat.S_IFDIR | 0o755, "st_nlink": 2, "st_uid": self.uid,
                "st_gid": self.gid, "st_size": 0, "st_atime": t, "st_mtime": t, "st_ctime": t}

    def file_attrs(self, size, mtime):
        t = int(mtime * 1e9) if mtime else self.started
        return {"st_mode": stat.S_IFREG | 0o644, "st_nlink": 1, "st_uid": self.uid,
                "st_gid": self.gid, "st_size": size, "st_blocks": (size + 511) // 512,
                "st_atime": t, "st_mtime": t, "st_ctime": t}

    def open_handle(self, provider, name):
        with self.state:
            return next((h for h in self.handles.values()
                         if h.provider is provider and h.name == name and h.dirty), None)

    def getattr(self, path, fh=None):
        provider, name = self.locate(path)
        if not name:
            return self.dir_attrs()
        handle = self.open_handle(provider, name)
        if handle:  # being written: its size is what's in hand, not the last listing's
            return self.file_attrs(len(handle.data), time.time())
        self.listing(provider)
        f = provider.files.get(name)
        if f:
            return self.file_attrs(f.get("size") or 0, f.get("mtime"))
        if name in provider.dirs:
            return self.dir_attrs()
        raise FuseOSError(errno.ENOENT)

    def readdir(self, path, fh):
        provider, name = self.locate(path)
        if provider is None:
            with self.state:
                return [".", "..", *sorted(self.providers)]
        self.listing(provider)
        if name and name not in provider.dirs:
            raise FuseOSError(errno.ENOTDIR if name in provider.files else errno.ENOENT)
        prefix = name + "/" if name else ""
        children = {n[len(prefix):] for n in (*provider.files, *provider.dirs)
                    if n.startswith(prefix) and "/" not in n[len(prefix):]}
        return [".", "..", *sorted(children - {""})]

    def statfs(self, path):
        return {"f_bsize": 4096, "f_frsize": 4096, "f_blocks": 1 << 24, "f_bfree": 1 << 23,
                "f_bavail": 1 << 23, "f_files": 1 << 20, "f_ffree": 1 << 19, "f_namemax": 255}

    # ── File contents ───────────────────────────────────────────────────────

    def new_handle(self, handle):
        with self.state:
            fh, self.next_fh = self.next_fh, self.next_fh + 1
            self.handles[fh] = handle
        return fh

    def handle(self, fh):
        with self.state:
            handle = self.handles.get(fh)
        if handle is None:
            raise FuseOSError(errno.EBADF)
        return handle

    def load(self, handle):
        """The handle's bytes, fetched with files.read the first time. Call with handle.lock."""
        if handle.data is None:
            f = handle.provider.files.get(handle.name)
            if f and (f.get("size") or 0) > MAX_READ:
                raise FuseOSError(errno.EFBIG)
            handle.data = bytearray(content_bytes(self.call(handle.provider, READ, file=handle.name)))
        return handle.data

    def push(self, handle):
        """Write a changed handle back with files.write. Call with handle.lock."""
        if not handle.dirty:
            return
        f = self.call(handle.provider, WRITE, file=handle.name, **write_args(bytes(handle.data)))
        handle.provider.files[handle.name] = f
        handle.dirty = False

    def open(self, path, flags):
        provider, name = self.locate(path)
        if not name:
            raise FuseOSError(errno.EISDIR)
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC):
            self.writable(path)
        self.listing(provider)
        if name not in provider.files:
            raise FuseOSError(errno.EISDIR if name in provider.dirs else errno.ENOENT)
        handle = Handle(provider, name)
        if flags & os.O_TRUNC:
            handle.data, handle.dirty = bytearray(), True
        return self.new_handle(handle)

    def create(self, path, mode, fi=None):
        provider, name = self.writable(path)
        handle = Handle(provider, name, bytearray(), dirty=True)
        with handle.lock:
            self.push(handle)  # exists on the provider from now on, as a new file should
        for i in range(1, len(name.split("/"))):
            provider.dirs.add("/".join(name.split("/")[:i]))
        return self.new_handle(handle)

    def read(self, path, size, offset, fh):
        handle = self.handle(fh)
        with handle.lock:
            return bytes(self.load(handle)[offset:offset + size])

    def write(self, path, data, offset, fh):
        handle = self.handle(fh)
        if not handle.provider.writable:
            raise FuseOSError(errno.EROFS)
        with handle.lock:
            buf = self.load(handle)
            if offset + len(data) > MAX_READ:
                handle.data, handle.dirty = None, False  # never save a cut-short file
                raise FuseOSError(errno.EFBIG)
            if offset > len(buf):
                buf.extend(bytes(offset - len(buf)))
            buf[offset:offset + len(data)] = data
            handle.dirty = True
        return len(data)

    def truncate(self, path, length, fh=None):
        if length > MAX_READ:
            raise FuseOSError(errno.EFBIG)
        provider, name = self.writable(path)
        handle = self.handle(fh) if fh is not None else Handle(provider, name)
        with handle.lock:
            buf = handle.data = self.load(handle) if length else bytearray()
            del buf[length:]
            buf.extend(bytes(length - len(buf)))
            handle.dirty = True
            if fh is None:
                self.push(handle)
        return 0

    def flush(self, path, fh):
        handle = self.handle(fh)
        with handle.lock:
            self.push(handle)
        return 0

    def fsync(self, path, datasync, fh):
        return self.flush(path, fh)

    def release(self, path, fh):
        try:
            return self.flush(path, fh)
        finally:
            with self.state:
                self.handles.pop(fh, None)

    # ── Names ───────────────────────────────────────────────────────────────

    def unlink(self, path):
        provider, name = self.writable(path)
        self.call(provider, DELETE, file=name)
        provider.files.pop(name, None)
        return 0

    def mkdir(self, path, mode):
        provider, name = self.writable(path)
        self.call(provider, MKDIR, dir=name)
        provider.dirs.add(name)
        return 0

    def rmdir(self, path):
        provider, name = self.writable(path)
        self.call(provider, RMDIR, dir=name)
        provider.dirs.discard(name)
        return 0

    def rename(self, old, new):
        provider, name = self.writable(old)
        to_provider, to = self.writable(new)
        if to_provider is not provider:
            raise FuseOSError(errno.EXDEV)  # mv falls back to copy + delete across providers
        self.call(provider, RENAME, file=name, to=to)
        self.listing(provider, fresh=True)
        return 0

    # Providers keep no modes, owners or times of their own; accept what tools set (cp -p, editors).
    def chmod(self, path, mode):
        self.getattr(path)
        return 0

    def chown(self, path, uid, gid):
        self.getattr(path)
        return 0

    def utimens(self, path, times=None):
        self.getattr(path)
        return 0


def unmount(mountpoint):
    tool = shutil.which("fusermount3") or shutil.which("fusermount")
    if not tool:
        sys.exit("mount: no fusermount3 on PATH (Arch: pacman -S fuse3)")
    sys.exit(subprocess.run([tool, "-u", mountpoint]).returncode)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mountpoint", nargs="?", default=os.path.expanduser("~/agentbus"))
    parser.add_argument("--unmount", action="store_true", help="unmount it instead")
    opts = parser.parse_args()
    mountpoint = os.path.abspath(opts.mountpoint)
    if opts.unmount:
        unmount(mountpoint)
    os.makedirs(mountpoint, exist_ok=True)
    procs = agentbus.Procs()
    procs.functions()  # fail now, with the hub's own advice, if there's no hub to ask
    log(f"mounting the bus's file providers on {mountpoint} (Ctrl-C or --unmount to stop)")
    mfusepy.FUSE(BusFiles(procs), mountpoint, foreground=True, fsname="agentbus", subtype="agentbus")


if __name__ == "__main__":
    main()
