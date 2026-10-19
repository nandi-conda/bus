"""Procs on freeq: public procs announce their functions in #bots, and a bridge makes them
callable on the local hub.

A public proc is its own freeq bot. It signs in with a did:key (stored the way freeq's bot-kit
stores it, ~/.freeq/bots/<name>/agent.key), runs bot-kit's announce sequence (PROVENANCE, AGENT
REGISTER, AGENT MANIFEST, PRESENCE, HEARTBEAT), joins #bots and says what it provides::

    @+agentbus/provides=weather.current,weather.forecast;+agentbus/host=<id> TAGMSG #bots

It says so again whenever anyone asks (``@+agentbus/discover=1 TAGMSG #bots``) and whenever its
functions change. The same names go in its manifest's ``[capabilities.channels]`` for #bots,
which freeq serves at ``GET /api/v1/agents/manifests/{did}``.

Calls are tag-only DMs to the proc's nick, answered by the proc itself::

    @+agentbus/call=<id>;+agentbus/fn=weather.current;+agentbus/args={"place":"Berlin"} TAGMSG weather
    @+agentbus/reply=<id>;+agentbus/result={"temp_c":11.5} TAGMSG bridge-…
    @+agentbus/reply=<id>;+agentbus/error=ZeroDivisionError: … TAGMSG bridge-…

The bridge (``Bridge``) joins #bots, asks who provides what, and calls ``Provide`` on the local
hub for every function a remote proc announces. A local ``procs.call("weather.current", …)`` then
reaches whichever proc in #bots provides it: each remote proc is its own provider on the hub, so
the hub ranks and fails over between them as it does for local ones. Only
announcements from signed-in DIDs count (the ``account`` tag); ``trust`` narrows that to a set.
"""

import atexit
import base64
import hashlib
import itertools
import json
import os
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_SERVER = "ircs://irc.freeq.at:6697"
DEFAULT_CHANNEL = "#bots"
CALL_TIMEOUT = 20.0
# freeq allows each connection a burst of 10 commands, refilled at 10 per second, and drops
# (not queues) anything past that. Stay under it by waiting instead.
SEND_BURST = 8
SEND_RATE = 8.0
# freeq drops lines over 8192 bytes; keep a margin for the prefix and the other tags.
MAX_TAG_VALUE = 7000

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class FreeqError(Exception):
    """A freeq connection or remote call failed."""


def _b58btc(data):
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _b64u(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def host_id():
    """Identifies this host's bus, so a bridge never imports procs from its own bus."""
    try:
        with open("/etc/machine-id") as f:
            machine = f.read().strip()
    except OSError:
        machine = socket.gethostname()
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    return hashlib.sha256(f"{machine}\0{bus}".encode()).hexdigest()[:16]


class Identity:
    """A bot's did:key, from a 32-byte ed25519 seed (bot-kit's on-disk format)."""

    def __init__(self, seed):
        if len(seed) != 32:
            raise ValueError(f"an ed25519 seed is 32 bytes, not {len(seed)}")
        self.key = Ed25519PrivateKey.from_private_bytes(seed)
        pub = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.multibase = "z" + _b58btc(b"\xed\x01" + pub)
        self.did = "did:key:" + self.multibase

    @classmethod
    def load(cls, name, root=None):
        """Load ~/.freeq/bots/<name>/agent.key, creating it (mode 0600) on first use."""
        root = root or os.environ.get("AGENTBUS_FREEQ_HOME") or os.path.expanduser("~/.freeq/bots")
        path = os.path.join(root, name, "agent.key")
        try:
            with open(path, "rb") as f:
                return cls(f.read())
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        seed = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(seed)
        return cls(seed)

    def sign(self, data):
        return self.key.sign(data)


# ── IRC wire ────────────────────────────────────────────────────────────────

_ESCAPES = {";": "\\:", " ": "\\s", "\\": "\\\\", "\r": "\\r", "\n": "\\n"}
_UNESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


def escape_tag(value):
    return "".join(_ESCAPES.get(c, c) for c in value)


def unescape_tag(value):
    out, chars = [], iter(value)
    for c in chars:
        if c == "\\":
            nxt = next(chars, "")
            out.append(_UNESCAPES.get(nxt, nxt))
        else:
            out.append(c)
    return "".join(out)


def parse_line(line):
    """-> (tags, source nick, command, params)."""
    tags = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for item in raw[1:].split(";"):
            key, _, value = item.partition("=")
            tags[key] = unescape_tag(value)
    source = ""
    if line.startswith(":"):
        prefix, _, line = line.partition(" ")
        source = prefix[1:].split("!", 1)[0]
    trailing = None
    if " :" in line:
        line, _, trailing = line.partition(" :")
    parts = line.split()
    command, params = (parts[0].upper() if parts else ""), parts[1:]
    if trailing is not None:
        params.append(trailing)
    return tags, source, command, params


def format_tags(tags):
    return "@" + ";".join(f"{k}={escape_tag(v)}" for k, v in tags.items())


def _toml_str(s):
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def manifest_toml(name, description, owner_did, functions, channel):
    lines = [
        "[agent]",
        f"display_name = {_toml_str(name)}",
        'actor_class = "agent"',
    ]
    if description:
        lines.append(f"description = {_toml_str(description)}")
    lines += [
        "",
        "[provenance]",
        'origin_type = "custom"',
        f"creator_did = {_toml_str(owner_did)}",
        f"revocation_authority = {_toml_str(owner_did)}",
        "",
        "[capabilities.channels]",
        f"{_toml_str(channel)} = [{', '.join(_toml_str(f) for f in sorted(functions))}]",
        "",
    ]
    return "\n".join(lines)


class Bot:
    """One freeq connection, kept alive on a background thread, reconnecting with backoff.

    ``on_ready()`` runs after each successful sign-in and JOIN; ``on_message(tags, nick,
    command, params)`` runs for every line from the server, on the reader thread."""

    def __init__(self, name, server=None, channel=DEFAULT_CHANNEL, owner_did=None,
                 description=None, identity=None):
        self.name = name
        self.server = server or os.environ.get("AGENTBUS_FREEQ_SERVER") or DEFAULT_SERVER
        self.channel = channel
        self.identity = identity or Identity.load(name)
        self.owner_did = owner_did or os.environ.get("AGENTBUS_OWNER_DID")
        self.description = description
        self.nick = name
        self.on_ready = lambda: None
        self.on_message = lambda tags, nick, command, params: None
        self.ready = threading.Event()
        self._sock = None
        self._send_lock = threading.Lock()
        self._tokens, self._refilled = float(SEND_BURST), time.monotonic()
        self._registered = False
        self._stop = threading.Event()
        self._thread = None
        self._manifest_functions = set()

    # Connection lifecycle

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name=f"freeq-{self.name}", daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            # PART first: freeq keeps a signed-in nick in its channels for 30 s after QUIT
            # (in case it reconnects), but a PART takes effect at once.
            if self.ready.is_set():
                self.send(f"PART {self.channel} :stopping")
            self.send("QUIT :stopping")
        except FreeqError:
            pass
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(5)

    def wait_ready(self, timeout=30):
        if not self.ready.wait(timeout):
            raise FreeqError(f"{self.name} did not sign in to {self.server} within {timeout}s")

    def _connect(self):
        url = urlsplit(self.server)
        ports = {"irc": 6667, "ircs": 6697, "ws": 80, "wss": 443}
        if url.scheme not in ports:
            raise FreeqError(f"server must be irc://, ircs://, ws:// or wss://, not {self.server!r}")
        port = url.port or ports[url.scheme]
        if url.scheme.startswith("ws"):
            sock = _open_tunnel(url.hostname, port, url.scheme == "wss")
            sock = _WebSocket(sock, url.hostname, url.path or "/irc")
        else:
            sock = socket.create_connection((url.hostname, port), timeout=15)
            if url.scheme == "ircs":
                sock = _tls_context().wrap_socket(sock, server_hostname=url.hostname)
        sock.settimeout(None)
        return sock

    def _run(self):
        backoff = 1
        while not self._stop.is_set():
            try:
                self._sock = self._connect()
                self._session()
                backoff = 1
            except (OSError, FreeqError) as e:
                if self._stop.is_set():
                    break
                print(f"freeq: {self.name}: {e}; reconnecting in {backoff}s", flush=True)
            finally:
                self.ready.clear()
                if self._sock:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _lines(self):
        buf = b""
        while True:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise FreeqError("server closed the connection")
            buf += chunk
            while b"\r\n" in buf:
                raw, buf = buf.split(b"\r\n", 1)
                yield raw.decode("utf-8", "replace")

    def _session(self):
        self.nick = self.name
        self._registered = False
        self.send("CAP LS 302")
        self.send("CAP REQ :sasl message-tags account-tag")
        self.send("AUTHENTICATE ATPROTO-CHALLENGE")
        heartbeat = None
        debug = os.environ.get("AGENTBUS_FREEQ_DEBUG")
        for line in self._lines():
            if debug:
                print(f"freeq: {self.nick} <- {line}", file=sys.stderr, flush=True)
            tags, source, command, params = parse_line(line)
            if command == "PING":
                self.send("PONG :" + (params[-1] if params else ""))
            elif command == "AUTHENTICATE" and params and params[0] != "+":
                # Sign the exact bytes the server sent.
                sig = _b64u(self.identity.sign(_unb64u(params[0])))
                self.send("AUTHENTICATE " + _b64u(json.dumps({"did": self.identity.did, "signature": sig}).encode()))
            elif command == "903":
                self.send(f"NICK {self.nick}")
                self.send(f"USER {self.name} 0 * :{self.description or self.name}")
                self.send("CAP END")
            elif command in ("904", "905", "906"):
                raise FreeqError(f"SASL sign-in refused: {params[-1] if params else command}")
            elif command == "433":  # nick in use
                self.nick = f"{self.name}-{os.urandom(2).hex()}"
                self.send(f"NICK {self.nick}")
            elif command == "001":
                self._registered = True
                self.nick = params[0] if params else self.nick
                self._announce()
                self.send(f"JOIN {self.channel}")
                heartbeat = threading.Thread(target=self._heartbeat, daemon=True)
                heartbeat.start()
            elif command == "JOIN" and source == self.nick and params and params[0].lower() == self.channel.lower():
                self.ready.set()
                self.on_ready()
            elif command == "NICK" and source == self.nick and params:
                self.nick = params[0]
            else:
                self.on_message(tags, source, command, params)

    def _announce(self):
        """bot-kit's sequence: PROVENANCE → AGENT REGISTER → AGENT MANIFEST → PRESENCE."""
        owner = self.owner_did or self.identity.did
        if self.owner_did:
            cert = {
                "type": "FreeqBotDelegation/v1",
                "bot_did": self.identity.did,
                "bot_public_key": self.identity.multibase,
                "creator_did": self.owner_did,
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "revocation_authority": self.owner_did,
                "signature": None,
            }
            self.send("PROVENANCE :" + _b64u(json.dumps(cert).encode()))
        self.send("AGENT REGISTER :class=agent")
        toml = manifest_toml(self.name, self.description, owner, self._manifest_functions, self.channel)
        self.send("AGENT MANIFEST " + base64.b64encode(toml.encode()).decode())
        self.send("PRESENCE :state=idle")

    def _heartbeat(self, every=30):
        sock = self._sock
        while not self._stop.is_set() and self._sock is sock:
            try:
                self.send(f"HEARTBEAT :state=idle;ttl={every * 2}")
            except FreeqError:
                return
            self._stop.wait(every)

    def set_functions(self, functions):
        """Re-send the manifest when the provided functions change."""
        self._manifest_functions = set(functions)
        if self.ready.is_set():
            owner = self.owner_did or self.identity.did
            toml = manifest_toml(self.name, self.description, owner, self._manifest_functions, self.channel)
            self.send("AGENT MANIFEST " + base64.b64encode(toml.encode()).decode())

    # Sending

    def send(self, line):
        if "\r" in line or "\n" in line:
            raise ValueError("an IRC line cannot contain CR or LF")
        sock = self._sock
        if sock is None:
            raise FreeqError(f"{self.name} is not connected")
        with self._send_lock:
            self._pace(line)
            try:
                sock.sendall((line + "\r\n").encode())
            except OSError as e:
                raise FreeqError(f"send failed: {e}") from e

    def _pace(self, line):
        if line.split(" ", 1)[0] in ("PING", "PONG", "JOIN") or not self._registered:
            return  # exempt, or still registering, which freeq doesn't count
        now = time.monotonic()
        self._tokens = min(SEND_BURST, self._tokens + (now - self._refilled) * SEND_RATE)
        self._refilled = now
        if self._tokens < 1:
            time.sleep((1 - self._tokens) / SEND_RATE)
            self._tokens, self._refilled = 1.0, time.monotonic()
        self._tokens -= 1

    def tagmsg(self, target, tags):
        for key, value in tags.items():
            if len(value.encode()) > MAX_TAG_VALUE:
                raise FreeqError(f"{key} is {len(value.encode())} bytes; freeq tags carry at most {MAX_TAG_VALUE}")
        self.send(f"{format_tags(tags)} TAGMSG {target}")


def _open_tunnel(host, port, tls):
    """A TCP (and TLS) connection to host:port, through $HTTPS_PROXY with CONNECT when one is
    set and the host isn't in $NO_PROXY: sandboxes often allow only that route out."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    no_proxy = [h.strip().lstrip(".") for h in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if h.strip()]
    if proxy and not any(host == h or host.endswith("." + h) for h in no_proxy):
        p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
        sock = socket.create_connection((p.hostname, p.port or 80), timeout=15)
        auth = ""
        if p.username:
            auth = "Proxy-Authorization: Basic " + base64.b64encode(f"{p.username}:{p.password or ''}".encode()).decode() + "\r\n"
        sock.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise FreeqError("proxy closed the connection during CONNECT")
            head += chunk
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 200" not in status:
            raise FreeqError(f"proxy refused CONNECT {host}:{port}: {status}")
    else:
        sock = socket.create_connection((host, port), timeout=15)
    if tls:
        sock = _tls_context().wrap_socket(sock, server_hostname=host)
    return sock


def _tls_context():
    # Honor a CA bundle the environment names, as a TLS-inspecting proxy needs.
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)


class _WebSocket:
    """Just enough of RFC 6455 for freeq's /irc gateway, which carries one IRC line per text
    frame. Looks like a socket to Bot: ``sendall`` takes CRLF-terminated lines, ``recv`` yields
    them back CRLF-terminated."""

    def __init__(self, sock, host, path):
        self.sock = sock
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                      "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise FreeqError("server closed the connection during the WebSocket upgrade")
            head += chunk
        head, self._buf = head.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status:
            raise FreeqError(f"WebSocket upgrade refused: {status}")
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if f"sec-websocket-accept: {accept}".lower().encode() not in head.lower():
            raise FreeqError("WebSocket upgrade answered with the wrong accept key")
        self._lock = threading.Lock()

    def settimeout(self, t):
        self.sock.settimeout(t)

    def close(self):
        try:
            self._frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()

    def _frame(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            head += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._lock:
            self.sock.sendall(head + mask + body)

    def sendall(self, data):
        for line in data.split(b"\r\n"):
            if line:
                self._frame(0x1, line)

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self, _size):
        """The next text frame, as one CRLF-terminated line ("" once the server closes)."""
        message = b""
        try:
            while True:
                b0, b1 = self._read(2)
                n = b1 & 0x7F
                if n == 126:
                    n = int.from_bytes(self._read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self._read(8), "big")
                mask = self._read(4) if b1 & 0x80 else None
                payload = self._read(n)
                if mask:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                opcode = b0 & 0x0F
                if opcode == 0x8:
                    return b""
                if opcode == 0x9:
                    self._frame(0xA, payload)
                    continue
                if opcode in (0x0, 0x1, 0x2):
                    message += payload
                    if b0 & 0x80:
                        return message if message.endswith(b"\r\n") else message + b"\r\n"
        except OSError:
            return b""


def _json(value):
    return json.dumps(value, separators=(",", ":"))


# ── Public procs ────────────────────────────────────────────────────────────


class PublicProc:
    """Serves a proc's functions to #bots. ``handlers`` maps function name to
    ``handler(args, info) -> dict``; calls from freeq get ``info=None``."""

    def __init__(self, name, handlers, trust=None, **bot_args):
        self.handlers = handlers
        self.trust = set(trust) if trust else None
        self.host = host_id()
        self.bot = Bot(name, **bot_args)
        self.bot.on_ready = self.announce
        self.bot.on_message = self._on_message

    def start(self):
        self.bot.set_functions(self.handlers)
        self.bot.start()
        # Leave #bots on the way out. Without it, freeq holds a signed-in nick's seat for 30 s
        # and bridges keep routing calls to it until then.
        atexit.register(self.stop)
        return self

    def stop(self):
        self.bot.stop()

    def changed(self):
        """Call after adding or removing a handler."""
        self.bot.set_functions(self.handlers)
        if self.bot.ready.is_set():
            self.announce()

    def announce(self):
        self.bot.tagmsg(self.bot.channel, {
            "+agentbus/provides": ",".join(sorted(self.handlers)),
            "+agentbus/host": self.host,
        })

    def _on_message(self, tags, nick, command, params):
        if command != "TAGMSG" or not params:
            return
        if "+agentbus/discover" in tags and params[0].lower() == self.bot.channel.lower():
            self.announce()
        elif "+agentbus/call" in tags and params[0] == self.bot.nick:
            threading.Thread(target=self._answer, args=(tags, nick), daemon=True).start()

    def _answer(self, tags, nick):
        call_id, name = tags["+agentbus/call"], tags.get("+agentbus/fn", "")
        caller = tags.get("account")
        reply = {"+agentbus/reply": call_id}
        try:
            if not caller:
                raise PermissionError("calls need a signed-in caller")
            if self.trust is not None and caller not in self.trust:
                raise PermissionError(f"{caller} is not trusted")
            handler = self.handlers.get(name)
            if handler is None:
                raise LookupError(f"no function {name!r} here")
            args = json.loads(tags.get("+agentbus/args") or "{}")
            if not isinstance(args, dict):
                raise TypeError("args must be a JSON object")
            result = handler(args, None)
            if not isinstance(result, dict):
                raise TypeError(f"handler returned {type(result).__name__}, not a dict")
            reply["+agentbus/result"] = _json(result)
        except Exception as e:
            reply["+agentbus/error"] = f"{type(e).__name__}: {e}"
        try:
            self.bot.tagmsg(nick, reply)
        except FreeqError as e:
            # Too large, or the connection dropped: say so instead of leaving the caller waiting.
            if "+agentbus/result" in reply:
                try:
                    self.bot.tagmsg(nick, {"+agentbus/reply": call_id, "+agentbus/error": f"FreeqError: {e}"})
                except FreeqError:
                    pass


# ── Bridge ──────────────────────────────────────────────────────────────────


class Bridge:
    """Makes functions announced in #bots callable on the local hub.

    Each trusted remote proc in #bots becomes its own provider on the local hub (``hub``),
    forwarding each call to it over freeq (see ``agentbus.mirror``)."""

    def __init__(self, hub=None, name=None, trust=None, call_timeout=CALL_TIMEOUT, **bot_args):
        from . import HUB_NAME
        from .mirror import Mirror

        self.host = host_id()
        self.trust = set(trust) if trust else None
        self.call_timeout = call_timeout
        self.bot = Bot(name or f"bridge-{self.host[:6]}", **bot_args)
        self.bot.on_ready = self._on_ready
        self.bot.on_message = self._on_message
        self.mirror = Mirror(hub or HUB_NAME, lambda remote, fn, args: self.call(remote.label, fn, args))
        self.hub = self.mirror.hub
        self._pending = {}  # call id -> [threading.Event, reply tags, nick]
        self._ids = itertools.count()

    def start(self):
        self.mirror.start()
        self.bot.start()
        return self

    def stop(self):
        self.bot.stop()
        self.mirror.stop()

    def functions(self):
        """The functions this bridge currently provides on the local hub."""
        return self.mirror.functions()

    def providers(self, name):
        """[(nick, did)] of the remote procs providing ``name`` on the local hub."""
        return self.mirror.providers(name)

    def owners(self):
        """{local unique bus name: (nick, did)}, to label what ``Procs.providers`` returns."""
        return self.mirror.owners()

    def wait_synced(self, timeout=10):
        """Block until pending announcements have reached the hub (for tests and scripts)."""
        return self.mirror.wait_synced(timeout)

    def _on_ready(self):
        with self.mirror.lock:
            self.mirror.remotes.clear()
        self.mirror.changed()
        self.bot.tagmsg(self.bot.channel, {"+agentbus/discover": "1"})

    def _on_message(self, tags, nick, command, params):
        if command == "TAGMSG" and params:
            if "+agentbus/reply" in tags and params[0] == self.bot.nick:
                waiter = self._pending.get(tags["+agentbus/reply"])
                if waiter and waiter[2] == nick:
                    waiter[1] = tags
                    waiter[0].set()
            elif "+agentbus/provides" in tags and params[0].lower() == self.bot.channel.lower():
                self._learn(tags, nick)
        elif command in ("PART", "KICK", "QUIT"):
            self.mirror.drop(params[1] if command == "KICK" and len(params) > 1 else nick)
        elif command == "NICK" and params:
            with self.mirror.lock:
                remote = self.mirror.remotes.pop(nick, None)
                if remote:
                    remote.key = remote.label = params[0]
                    self.mirror.remotes[params[0]] = remote

    def _learn(self, tags, nick):
        did = tags.get("account")
        if not did or tags.get("+agentbus/host") == self.host:
            return  # guests can't be held to anything; our own bus already has its procs
        if self.trust is not None and did not in self.trust:
            return
        self.mirror.put(nick, nick, did, {f for f in tags["+agentbus/provides"].split(",") if f})

    def call(self, nick, name, args):
        """Call ``name`` on one remote proc and wait for its reply."""
        call_id = f"{self.host[:6]}-{next(self._ids)}"
        waiter = [threading.Event(), None, nick]
        self._pending[call_id] = waiter
        try:
            self.bot.tagmsg(nick, {
                "+agentbus/call": call_id,
                "+agentbus/fn": name,
                "+agentbus/args": _json(args),
            })
            if not waiter[0].wait(self.call_timeout):
                raise TimeoutError(f"{nick} did not answer {name} within {self.call_timeout}s")
            reply = waiter[1]
        finally:
            self._pending.pop(call_id, None)
        if "+agentbus/error" in reply:
            raise FreeqError(reply["+agentbus/error"])
        result = json.loads(reply.get("+agentbus/result") or "null")
        if not isinstance(result, dict):
            raise FreeqError(f"{nick} returned {type(result).__name__}, not a dict")
        return result
