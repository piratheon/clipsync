#!/usr/bin/env python3
"""Sync plain-text clipboards between Android and a computer over ADB.

Run `clipsync.py --help` for commands. Phone backends: `cmd`, `service`,
then the optional Clipper helper app. PC support: Linux and macOS.
Clipboard contents are not logged; only their lengths are reported.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Optional, Sequence

LOG = logging.getLogger("clipsync")

DEFAULT_INTERVAL = 1.0
MIN_INTERVAL = 0.2
DEFAULT_MAX_BYTES = 64 * 1024
ADB_TIMEOUT = 10.0
PC_TIMEOUT = 5.0
MAX_BACKOFF = 30.0
# Keep shell commands below Linux argument limits.
MAX_SHELL_COMMAND_BYTES = 120_000

CLIPPER_PACKAGE = "ca.zgrs.clipper"
CLIPPER_SERVICE = f"{CLIPPER_PACKAGE}/.ClipboardService"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class ClipsyncError(Exception):
    """Base class for expected, user-reportable failures."""


class AdbError(ClipsyncError):
    """adb is missing, the device is unreachable, or a device command failed."""


class BackendUnsupported(AdbError):
    """A phone clipboard backend cannot work on this device as configured."""


class ClipboardError(ClipsyncError):
    """The PC clipboard could not be read or written."""


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


class Adb:
    """Run ADB commands with timeouts and device selection."""

    def __init__(
        self,
        executable: str = "adb",
        serial: Optional[str] = None,
        timeout: float = ADB_TIMEOUT,
    ) -> None:
        resolved = shutil.which(executable)
        if resolved is None:
            raise AdbError(f"adb executable not found: {executable!r} (install android-tools)")
        self.executable = resolved
        self._requested = serial or os.environ.get("ANDROID_SERIAL") or None
        self._active: Optional[str] = None
        self._timeout = timeout

    @property
    def requested(self) -> Optional[str]:
        """The serial explicitly requested via --serial or $ANDROID_SERIAL, if any."""
        return self._requested

    def _exec(self, args: Sequence[str]) -> "subprocess.CompletedProcess[bytes]":
        try:
            return subprocess.run(
                [self.executable, *args],
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"adb timed out after {self._timeout:.0f}s") from exc
        except OSError as exc:
            raise AdbError(f"failed to execute adb: {exc}") from exc

    def serial(self) -> str:
        """Return the target device serial, validating that it is ready."""
        if self._active:
            return self._active

        result = self._exec(["devices"])
        if result.returncode != 0:
            raise AdbError(_decode(result.stderr).strip() or "`adb devices` failed")

        states: dict[str, str] = {}
        for line in _decode(result.stdout).splitlines():
            line = line.strip()
            if not line or line.startswith(("*", "List of")):
                continue
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]

        if self._requested:
            state = states.get(self._requested)
            if state is None:
                raise AdbError(f"device {self._requested!r} is not connected")
            if state != "device":
                raise AdbError(f"device {self._requested!r} is in state {state!r}")
            self._active = self._requested
            return self._active

        ready = [serial for serial, state in states.items() if state == "device"]
        if not ready:
            detail = ", ".join(f"{s} ({st})" for s, st in states.items())
            raise AdbError("no ready device" + (f": {detail}" if detail else ""))
        if len(ready) > 1:
            raise AdbError(
                "multiple devices connected, choose one with --serial: " + ", ".join(sorted(ready))
            )
        self._active = ready[0]
        return self._active

    def shell_raw(self, command: str) -> tuple[int, bytes, bytes]:
        """Run `command` on the device; return (exit status, stdout, stderr)."""
        result = self._exec(["-s", self.serial(), "shell", command])
        return result.returncode, result.stdout, result.stderr

    def shell(self, command: str) -> bytes:
        """Run `command` on the device and return stdout. Raises on non-zero exit."""
        status, stdout, stderr = self.shell_raw(command)
        if status != 0:
            # Retry device discovery after an ADB error.
            self._active = None
            detail = _decode(stderr).strip() or _decode(stdout).strip()
            detail = detail.splitlines()[0][:160] if detail else ""
            raise AdbError(detail or f"device command exited with status {status}")
        return stdout

    def reset(self) -> None:
        self._active = None


def _quoted_command(prefix: str, text: str) -> str:
    if "\x00" in text:
        raise ClipboardError("text contains NUL bytes and cannot be sent to the device")
    command = prefix + shlex.quote(text)
    if len(command.encode("utf-8")) > MAX_SHELL_COMMAND_BYTES:
        raise ClipboardError("text is too large to pass through the adb shell")
    return command


class CmdBackend:
    """`cmd clipboard get-text` / `set-text` (runs as the shell user)."""

    name = "cmd"
    _ERROR_MARKERS = (
        "no shell command",
        "unknown command",
        "can't find service",
        "cannot find service",
    )

    def __init__(self, adb: Adb) -> None:
        self._adb = adb

    def _unsupported(self) -> BackendUnsupported:
        return BackendUnsupported("`cmd clipboard` is not available on this Android build")

    def _run(self, command: str) -> str:
        status, raw_out, raw_err = self._adb.shell_raw(command)
        out, err = _decode(raw_out), _decode(raw_err)
        # Some Android builds report unsupported commands but return success.
        if any(marker in err.lower() for marker in self._ERROR_MARKERS) or (
            out.lower().lstrip().startswith(self._ERROR_MARKERS)
        ):
            raise self._unsupported()
        if status != 0:
            self._adb.reset()
            detail = (err.strip() or out.strip()).splitlines()[:1]
            raise AdbError(detail[0][:160] if detail else f"device command exited with status {status}")
        return out

    def probe(self) -> None:
        self.get()

    def get(self) -> str:
        out = self._run("cmd clipboard get-text")
        # Remove the newline added by the command.
        if out.endswith("\r\n"):
            return out[:-2]
        if out.endswith("\n"):
            return out[:-1]
        return out

    def set(self, text: str) -> None:
        self._run(_quoted_command("cmd clipboard set-text ", text))


def _parcel_words(output: str) -> list[int]:
    """Parse 32-bit words from `service call` output."""
    words: list[int] = []
    for line in output.splitlines():
        line = re.sub(r"^\s*Result: Parcel\(", "", line)
        line = re.sub(r"^\s*0x[0-9a-fA-F]+:", "", line)
        match = re.match(r"\s*((?:[0-9a-fA-F]{8}(?:\s+|$))+)", line)
        if match:
            words.extend(int(token, 16) for token in match.group(1).split())
    return words


def _pack_words(data: bytes) -> list[int]:
    """Pack bytes into signed little-endian 32-bit words."""
    padded = data + b"\x00" * (-len(data) % 4)
    return [w - (1 << 32) if w >= 1 << 31 else w for (w,) in struct.iter_unpack("<I", padded)]


def _find_item_text(words: Sequence[int]) -> Optional[str]:
    """Find the first readable plain-text item in a ClipData reply."""
    count = len(words)
    for j in range(2, count - 4):
        if words[j] != 0 or not 1 <= words[j + 1] <= 64 or words[j + 2] not in (0, 1):
            continue
        length = words[j + 3]
        raw = b"".join(struct.pack("<I", w) for w in words[j + 4 :])
        if length <= 0 or len(raw) <= length or raw[length] != 0:
            continue
        try:
            return raw[:length].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _clip_words(text: str, label: bytes, timestamp_ms: int) -> list[str]:
    """Build `service call` arguments for a plain-text clip."""
    text_bytes = text.encode("utf-8")
    ints: list[int] = [
        1,  # non-null ClipData
        1, len(label), *_pack_words(label + b"\x00"),  # label
        1, len("text/plain"), *_pack_words("text/plain".encode("utf-16-le") + b"\x00\x00"),
        -1,  # null extras
    ]
    args = [f"i32 {value}" for value in ints]
    args.append(f"i64 {timestamp_ms}")
    tail: list[int] = [
        0,  # no styled text
        3,  # classification status
        0,
        0,  # no icon
        1,  # one item
        1, len(text_bytes), *_pack_words(text_bytes + b"\x00"),  # item text
        -1,  # null HTML text
        0, 0, 0, 0, 0,  # no intent or URI
    ]
    args.extend(f"i32 {value}" for value in tail)
    return args


@dataclass(frozen=True)
class _SetVariant:
    """One way of asking the clipboard service to store a clip."""

    name: str
    code: int  # 1 = setPrimaryClip, 2 = setPrimaryClipAsPackage
    tag: str  # attribution tag; "" sends an empty string
    root: bool = False


_SET_VARIANTS = (
    _SetVariant("shell", 1, "clipsync"),
    _SetVariant("shell, empty attribution tag", 1, ""),
    _SetVariant("shell, as-package call", 2, "clipsync"),
    _SetVariant("shell, as-package call, empty tag", 2, ""),
    _SetVariant("root (su)", 1, "clipsync", root=True),
)


class ServiceBackend:
    """Use clipboard binder calls; transaction IDs were verified on Android 16."""

    name = "service"
    # Text size is limited by the command-line argument length.
    max_text_bytes = 24 * 1024

    _TX_SET = 1
    _TX_GET = 4
    _TRAILER = "s16 com.android.shell s16 clipsync i32 0 i32 0"
    _LABEL = b"clipsync"

    def __init__(self, adb: Adb) -> None:
        self._adb = adb
        self._variant: Optional[_SetVariant] = None  # write method proven to work

    def _call(self, code: int, args: str, root: bool = False) -> list[int]:
        command = f"service call clipboard {code} {args}"
        if root:
            command = f"su -c '{command}'"
        if len(command.encode("utf-8")) > MAX_SHELL_COMMAND_BYTES:
            raise ClipboardError("text is too large to pass through the adb shell")
        status, stdout, stderr = self._adb.shell_raw(command)
        if status != 0:
            self._adb.reset()
            detail = (_decode(stderr).strip() or _decode(stdout).strip()).splitlines()[:1]
            raise AdbError(detail[0][:160] if detail else f"`service call` exited with status {status}")
        words = _parcel_words(_decode(stdout))
        if not words:
            raise BackendUnsupported("the clipboard service did not return a parcel")
        if words[0] != 0:
            raise BackendUnsupported(f"the clipboard service raised an exception (0x{words[0]:08x})")
        return words

    def probe(self) -> None:
        self._read(strict=True)

    def _read(self, strict: bool) -> str:
        words = self._call(self._TX_GET, self._TRAILER)
        if len(words) < 2:
            raise BackendUnsupported("unexpected reply size: transaction numbers differ on this Android version")
        if words[1] == 0:
            return ""
        text = _find_item_text(words)
        if text is None:
            if strict:
                raise BackendUnsupported("could not find the clip text: unrecognised parcel layout")
            LOG.debug("the phone clipboard holds no readable text item")
            return ""
        return text

    def get(self) -> str:
        return self._read(strict=False)

    def _write(self, text: str, variant: _SetVariant) -> None:
        args = " ".join(_clip_words(text, self._LABEL, int(time.time() * 1000)))
        tag = f"s16 {variant.tag}" if variant.tag else "s16 ''"
        trailer = f"s16 com.android.shell {tag} i32 0 i32 0"
        if variant.code == 2:
            trailer += " s16 com.android.shell"  # sourcePackage of setPrimaryClipAsPackage
        words = self._call(variant.code, f"{args} {trailer}", root=variant.root)
        if len(words) != 1:
            raise AdbError("unexpected reply to the write")

    def set(self, text: str) -> None:
        if "\x00" in text:
            raise ClipboardError("text contains NUL bytes and cannot be sent to the device")

        if self._variant is not None:
            self._write(text, self._variant)
            return

        # Verify the write and remember the first variant that works.
        outcomes: list[str] = []
        for variant in _SET_VARIANTS:
            try:
                self._write(text, variant)
                words = self._call(self._TX_GET, self._TRAILER)
            except ClipboardError:
                raise
            except AdbError as exc:
                outcomes.append(f"{variant.name}: {exc}")
                continue
            present = len(words) >= 2 and words[1] != 0
            stored = _find_item_text(words) if present else None
            if stored == text:
                self._variant = variant
                LOG.info("phone clipboard write method: %s", variant.name)
                return
            if not present:
                outcomes.append(f"{variant.name}: accepted, but the clipboard stayed empty")
            elif stored is None:
                # Include the reply to help diagnose an unknown parcel layout.
                dump = " ".join(f"{w:08x}" for w in words[:48])
                outcomes.append(
                    f"{variant.name}: a clip appeared but its text could not be parsed "
                    f"(reply words: {dump})"
                )
            else:
                outcomes.append(f"{variant.name}: accepted, but the clipboard was unchanged")
        raise BackendUnsupported(
            "Android accepted the write but never stored it (" + "; ".join(outcomes) + ")"
        )


class ClipperBackend:
    """Use the optional Clipper helper app through broadcasts."""

    name = "clipper"
    _RESULT = re.compile(r'Broadcast completed: result=(-?\d+)(?:, data="(.*)")?\Z', re.DOTALL)
    _RESULT_OK = -1

    def __init__(self, adb: Adb) -> None:
        self._adb = adb

    def probe(self) -> None:
        packages = _decode(self._adb.shell(f"pm list packages {CLIPPER_PACKAGE}")).split()
        if f"package:{CLIPPER_PACKAGE}" not in packages:
            raise BackendUnsupported(
                f"Clipper app ({CLIPPER_PACKAGE}) is not installed on the phone"
            )
        self.get()

    def _broadcast(self, command: str) -> "re.Match[str]":
        for attempt in (1, 2):
            out = _decode(self._adb.shell(command)).strip()
            match = self._RESULT.search(out)
            if match is None:
                raise AdbError("unexpected response from `am broadcast`")
            if int(match.group(1)) == self._RESULT_OK:
                return match
            if attempt == 1:
                # Start the service if no receiver handled the broadcast.
                try:
                    self._adb.shell(f"am startservice -n {CLIPPER_SERVICE}")
                except AdbError as exc:
                    LOG.debug("could not start the Clipper service: %s", exc)
        raise BackendUnsupported(
            "the Clipper service is not running: open the Clipper app once on the phone "
            "and allow it to run in the background"
        )

    def get(self) -> str:
        match = self._broadcast("am broadcast -a clipper.get")
        return match.group(2) or ""

    def set(self, text: str) -> None:
        self._broadcast(_quoted_command("am broadcast -a clipper.set -e text ", text))


_BACKENDS = {"cmd": CmdBackend, "service": ServiceBackend, "clipper": ClipperBackend}


class PhoneClipboard:
    """Phone clipboard that picks a working backend on first use."""

    def __init__(self, adb: Adb, choice: str = "auto") -> None:
        self._adb = adb
        self._choice = choice
        self._backend: Optional[CmdBackend | ServiceBackend | ClipperBackend] = None

    @property
    def backend_name(self) -> str:
        return self._resolve().name

    def limit_bytes(self, default: int) -> int:
        """Largest text the active backend can write, capped by `default`."""
        limit = getattr(self._resolve(), "max_text_bytes", None)
        return min(default, limit) if limit else default

    def _resolve(self) -> "CmdBackend | ServiceBackend | ClipperBackend":
        if self._backend is not None:
            return self._backend
        order = list(_BACKENDS) if self._choice == "auto" else [self._choice]
        reasons: list[str] = []
        for name in order:
            backend = _BACKENDS[name](self._adb)
            try:
                backend.probe()
            except BackendUnsupported as exc:
                reasons.append(f"{name}: {exc}")
                continue
            LOG.info("phone clipboard backend: %s", name)
            self._backend = backend
            return backend
        raise BackendUnsupported(
            "no usable phone clipboard backend (" + "; ".join(reasons) + "). "
            "Run `clipsync.py check` for details"
        )

    def get(self) -> str:
        return self._resolve().get()

    def set(self, text: str) -> None:
        self._resolve().set(text)


@dataclass(frozen=True)
class _PcBackend:
    name: str
    read_cmd: tuple[str, ...]
    write_cmd: tuple[str, ...]


def _detect_pc_backend() -> _PcBackend:
    system = platform.system()
    if system == "Darwin":
        if shutil.which("pbpaste") and shutil.which("pbcopy"):
            return _PcBackend("macOS pasteboard", ("pbpaste",), ("pbcopy",))
        raise ClipboardError("pbcopy/pbpaste not found")

    if system == "Linux":
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste") and shutil.which("wl-copy"):
            return _PcBackend(
                "wl-clipboard",
                ("wl-paste", "--no-newline", "--type", "text"),
                ("wl-copy",),
            )
        if os.environ.get("DISPLAY"):
            if shutil.which("xclip"):
                return _PcBackend(
                    "xclip",
                    ("xclip", "-selection", "clipboard", "-out"),
                    ("xclip", "-selection", "clipboard", "-in"),
                )
            if shutil.which("xsel"):
                return _PcBackend(
                    "xsel",
                    ("xsel", "--clipboard", "--output"),
                    ("xsel", "--clipboard", "--input"),
                )
        raise ClipboardError(
            "no usable clipboard tool: install wl-clipboard (Wayland) or xclip/xsel (X11), "
            "and make sure WAYLAND_DISPLAY or DISPLAY is set"
        )

    raise ClipboardError(f"unsupported platform: {system}")


class PcClipboard:
    """Access the host clipboard through platform command-line tools."""

    def __init__(self) -> None:
        self._backend = _detect_pc_backend()

    @property
    def name(self) -> str:
        return self._backend.name

    def read(self) -> str:
        try:
            result = subprocess.run(
                self._backend.read_cmd,
                capture_output=True,
                timeout=PC_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClipboardError("timed out reading the PC clipboard") from exc
        except OSError as exc:
            raise ClipboardError(f"failed to run {self._backend.read_cmd[0]}: {exc}") from exc

        if result.returncode != 0:
            # An empty or non-text clipboard is treated as empty.
            LOG.debug("pc clipboard read exited %d: %s", result.returncode, _decode(result.stderr).strip())
            return ""
        return _decode(result.stdout)

    def write(self, text: str) -> None:
        # Clipboard tools may fork and keep their output pipes open.
        try:
            result = subprocess.run(
                self._backend.write_cmd,
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=PC_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ClipboardError("timed out writing the PC clipboard") from exc
        except OSError as exc:
            raise ClipboardError(f"failed to run {self._backend.write_cmd[0]}: {exc}") from exc
        if result.returncode != 0:
            raise ClipboardError(f"{self._backend.write_cmd[0]} exited with status {result.returncode}")


def validate_payload(text: str, max_bytes: int) -> Optional[str]:
    """Validate text before sending it to the other clipboard."""
    if not text:
        return "clipboard is empty"
    if "\x00" in text:
        return "contains NUL bytes"
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        return f"{size} bytes exceeds the {max_bytes} byte limit"
    return None


class Syncer:
    def __init__(self, pc: PcClipboard, phone: PhoneClipboard, adb: Adb, max_bytes: int) -> None:
        self._pc = pc
        self._phone = phone
        self._adb = adb
        self._max_bytes = max_bytes

    def run(self, stop: threading.Event, interval: float) -> None:
        # Separate baselines prevent normalisation from causing echo loops.
        baseline: Optional[tuple[str, str]] = None
        delay = interval
        outage_reported = False

        while not stop.is_set():
            try:
                pc_text = self._pc.read()
                phone_text = self._phone.get()
                if baseline is None:
                    baseline = (pc_text, phone_text)
                    LOG.info("watching clipboards (pc: %s)", self._pc.name)
                else:
                    baseline = self._reconcile(baseline, pc_text, phone_text)
                delay = interval
                outage_reported = False
            except ClipsyncError as exc:
                if not outage_reported:
                    LOG.warning("sync paused: %s", exc)
                    outage_reported = True
                else:
                    LOG.debug("still failing: %s", exc)
                baseline = None  # re-baseline after recovery; never replay stale state
                self._adb.reset()
                delay = min(max(delay, interval) * 2, MAX_BACKOFF)
            stop.wait(delay)

    def _reconcile(
        self, baseline: tuple[str, str], pc_text: str, phone_text: str
    ) -> tuple[str, str]:
        base_pc, base_phone = baseline
        pc_changed = pc_text != base_pc
        phone_changed = phone_text != base_phone
        if not (pc_changed or phone_changed):
            return baseline

        pc_ok = pc_changed and self._acceptable(
            pc_text, "pc", self._phone.limit_bytes(self._max_bytes)
        )
        phone_ok = phone_changed and self._acceptable(phone_text, "phone", self._max_bytes)

        if pc_ok and phone_ok:
            LOG.warning("both clipboards changed within one interval; the PC value wins")

        if pc_ok:
            self._phone.set(pc_text)
            phone_text = self._phone.get()
            LOG.info("pc -> phone (%d chars)", len(pc_text))
        elif phone_ok:
            self._pc.write(phone_text)
            pc_text = self._pc.read()
            LOG.info("phone -> pc (%d chars)", len(phone_text))
        return pc_text, phone_text

    def _acceptable(self, text: str, origin: str, limit: int) -> bool:
        reason = validate_payload(text, limit)
        if reason is None:
            return True
        # Empty clipboards are routine; invalid non-empty values are warnings.
        level = logging.DEBUG if not text else logging.WARNING
        LOG.log(level, "not syncing %s clipboard: %s", origin, reason)
        return False


def run_check(adb: Adb, show_raw: bool, roundtrip: bool) -> int:
    print(f"adb:      {adb.executable}")
    print(f"device:   {adb.serial()}")

    def prop(name: str) -> str:
        return _decode(adb.shell(f"getprop {name}")).strip() or "unknown"

    print(
        f"android:  {prop('ro.build.version.release')} "
        f"(sdk {prop('ro.build.version.sdk')}), "
        f"{prop('ro.product.manufacturer')} {prop('ro.product.model')}"
    )

    try:
        pc = PcClipboard()
        print(f"pc:       {pc.name}, readable ({len(pc.read())} chars)")
    except ClipboardError as exc:
        print(f"pc:       UNAVAILABLE - {exc}")

    working: list[CmdBackend | ClipperBackend] = []
    for name, factory in _BACKENDS.items():
        backend = factory(adb)
        try:
            backend.probe()
            print(f"phone:    {name}: responds")
            working.append(backend)
        except ClipsyncError as exc:
            print(f"phone:    {name}: not usable - {exc}")
    usable = len(working)

    if roundtrip:
        print("round trip (overwrites the phone clipboard with a test token):")
        for backend in working:
            token = f"clipsync-{secrets.token_hex(6)}"
            try:
                backend.set(token)
                got = backend.get()
            except ClipsyncError as exc:
                print(f"  {backend.name}: FAILED - {exc}")
                continue
            if got == token:
                print(f"  {backend.name}: write and read both work")
            elif not got:
                print(f"  {backend.name}: write accepted but read returned nothing "
                      "(the shell is probably not allowed to read the clipboard)")
            else:
                print(f"  {backend.name}: read returned different text ({len(got)} chars)")

    if show_raw:
        status, stdout, stderr = adb.shell_raw("cmd clipboard get-text")
        print("raw `cmd clipboard get-text` (may reveal clipboard contents):")
        print(f"  exit={status} stdout={_decode(stdout)[:200]!r} stderr={_decode(stderr)[:200]!r}")

    if usable == 0:
        print(
            "\nNo phone clipboard backend works. Copy some text on the phone and run the "
            "check again; if it still fails, send me this output."
        )
        return EXIT_ERROR
    return EXIT_OK


def acquire_single_instance_lock(key: str) -> Optional[IO[str]]:
    """Prevent duplicate sync daemons for the same device key."""
    try:
        import fcntl
    except ImportError:  # non-POSIX platform
        return None

    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key) or "default"
    base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
    path = base / f"clipsync-{os.getuid()}-{safe_key}.lock"
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise ClipsyncError(f"another clipsync daemon is already running (lock: {path})") from exc
    return handle


MAIN_EPILOG = """\
examples:
  clipsync.py check                    diagnose adb, the phone and the PC clipboard
  clipsync.py push                     send the PC clipboard to the phone
  clipsync.py push --text "hello"      send literal text to the phone
  echo hi | clipsync.py push --stdin   send standard input to the phone
  clipsync.py pull                     copy the phone clipboard to the PC
  clipsync.py pull --stdout            print the phone clipboard
  clipsync.py daemon                   two-way sync in the foreground (Ctrl+C stops)
  clipsync.py daemon &                 same, in the background of this shell

first run:
  1. Enable USB debugging on the phone and authorise this PC (adb devices).
  2. Copy some text on the phone, then run `clipsync.py check --roundtrip`.
     At least one phone backend should report that write and read both work.

options such as --serial go after the command: clipsync.py daemon --serial XXXX
"""


def _positive_interval(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from exc
    if number < MIN_INTERVAL:
        raise argparse.ArgumentTypeError(f"interval must be at least {MIN_INTERVAL} seconds")
    return number


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer: {value!r}") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return number


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--adb", default="adb", metavar="PATH", help="adb executable (default: adb)")
    common.add_argument(
        "--serial",
        "-s",
        metavar="SERIAL",
        help="device serial (default: the only connected device, or $ANDROID_SERIAL)",
    )
    common.add_argument(
        "--phone-backend",
        choices=("auto", *_BACKENDS),
        default="auto",
        help="how to reach the phone clipboard (default: auto)",
    )
    common.add_argument(
        "--max-bytes",
        type=_positive_int,
        default=DEFAULT_MAX_BYTES,
        metavar="N",
        help=f"largest payload to sync (default: {DEFAULT_MAX_BYTES})",
    )
    common.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    parser = argparse.ArgumentParser(
        prog="clipsync.py",
        description="Sync clipboard text between an Android phone and this PC over ADB.",
        epilog=MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name: str, summary: str, description: str) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            parents=[common],
            help=summary,
            description=description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

    push = add("push", "send text to the phone clipboard", "Send text to the phone clipboard.")
    source = push.add_mutually_exclusive_group()
    source.add_argument("--text", help="send this text instead of the PC clipboard")
    source.add_argument("--stdin", action="store_true", help="read the text from standard input")

    pull = add("pull", "fetch the phone clipboard", "Fetch the phone clipboard.")
    pull.add_argument(
        "--stdout",
        action="store_true",
        help="print to standard output instead of setting the PC clipboard",
    )

    daemon = add(
        "daemon",
        "keep both clipboards in sync",
        "Poll both clipboards and copy whichever side changed to the other.\n"
        "Stops on Ctrl+C or SIGTERM. Only one daemon per device can run.",
    )
    daemon.add_argument(
        "--interval",
        type=_positive_interval,
        default=DEFAULT_INTERVAL,
        metavar="SECONDS",
        help=f"poll interval (default: {DEFAULT_INTERVAL})",
    )

    check = add("check", "diagnose adb, the phone and the PC clipboard", "Report what works and what does not.")
    check.add_argument(
        "--show-raw",
        action="store_true",
        help="also print the raw `cmd clipboard get-text` response (may reveal clipboard contents)",
    )
    check.add_argument(
        "--roundtrip",
        action="store_true",
        help="write a test token to the phone clipboard and read it back (overwrites the clipboard)",
    )
    return parser


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _run(args: argparse.Namespace) -> int:
    adb = Adb(args.adb, args.serial)

    if args.command == "check":
        return run_check(adb, args.show_raw, args.roundtrip)

    phone = PhoneClipboard(adb, args.phone_backend)

    if args.command == "push":
        if args.text is not None:
            text = args.text
        elif args.stdin:
            text = sys.stdin.read()
        else:
            text = PcClipboard().read()
        reason = validate_payload(text, phone.limit_bytes(args.max_bytes))
        if reason:
            raise ClipsyncError(f"nothing sent: {reason}")
        phone.set(text)
        LOG.info("sent %d chars to the phone", len(text))
        return EXIT_OK

    if args.command == "pull":
        text = phone.get()
        if not text:
            raise ClipsyncError(
                f"the phone clipboard read returned nothing (backend: {phone.backend_name}). "
                "Either it is empty or Android is not letting adb read it; "
                "run `clipsync.py check --roundtrip` to find out"
            )
        if args.stdout:
            sys.stdout.write(text)
            sys.stdout.flush()
        else:
            PcClipboard().write(text)
            LOG.info("received %d chars from the phone", len(text))
        return EXIT_OK

    if args.command == "daemon":
        pc = PcClipboard()
        lock = acquire_single_instance_lock(adb.requested or "auto")
        stop = threading.Event()

        def _request_stop(signum: int, _frame: object) -> None:
            LOG.info("received %s, shutting down", signal.Signals(signum).name)
            stop.set()

        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        try:
            Syncer(pc, phone, adb, args.max_bytes).run(stop, args.interval)
        finally:
            if lock is not None:
                lock.close()
        return EXIT_OK

    raise ClipsyncError(f"unknown command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE
    _configure_logging(args.verbose)
    try:
        return _run(args)
    except ClipsyncError as exc:
        LOG.error("%s", exc)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
