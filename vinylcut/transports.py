"""Transports: serial (pyserial), Windows raw printer spooler (ctypes/winspool),
file and TCP socket.

All transports share one blocking API used from a GUI worker thread::

    t = make_transport(conn)
    t.open()
    try:
        t.write(data, progress=cb, cancel_event=ev)   # progress(sent, total)
    finally:
        t.close()

Errors are raised as :class:`TransportError` (or a subclass) whose ``str()`` is
a human readable sentence suitable for a GUI dialog.  When ``cancel_event`` is
set, ``write`` stops at the next chunk boundary, sends ``cancel_data`` (if set,
e.g. ``protocols.cancel_sequence(device)``), sets ``self.cancelled = True`` and
returns normally (it does not raise).
"""
from __future__ import annotations

import os
import socket
import sys
import time

try:                                    # pyserial ships with Inkscape 1.4
    import serial
    import serial.tools.list_ports
except Exception:                       # pragma: no cover - only without pyserial
    serial = None

from .devices import ConnectionSettings, FLOW_CONTROLS, PARITIES, BAUD_RATES  # noqa: F401 (re-export)


class TransportError(Exception):
    """Human readable transport failure."""


class TransportTimeout(TransportError):
    """The device stopped accepting data."""


class TransportNotAvailable(TransportError):
    """The transport cannot be used on this system."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def _natural_key(s):
    import re
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def list_serial_ports() -> list:
    """[(device, description)] e.g. [("COM4", "USB Serial Port (COM4)")]."""
    if serial is None:
        return []
    try:
        ports = serial.tools.list_ports.comports()
    except Exception:
        return []
    out = []
    for p in ports:
        dev = getattr(p, "device", None) or str(p)
        desc = getattr(p, "description", "") or ""
        if desc in ("n/a", dev):
            desc = ""
        hwid = getattr(p, "hwid", "") or ""
        if hwid and hwid != "n/a" and "VID:PID" in hwid:
            desc = ("%s [%s]" % (desc, hwid)).strip()
        out.append((dev, desc))
    out.sort(key=lambda t: _natural_key(t[0]))
    return out


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _winspool = ctypes.WinDLL("winspool.drv", use_last_error=True)

    PRINTER_ENUM_LOCAL = 0x00000002
    PRINTER_ENUM_CONNECTIONS = 0x00000004

    class PRINTER_INFO_4W(ctypes.Structure):
        _fields_ = [("pPrinterName", wintypes.LPWSTR),
                    ("pServerName", wintypes.LPWSTR),
                    ("Attributes", wintypes.DWORD)]

    class DOC_INFO_1W(ctypes.Structure):
        _fields_ = [("pDocName", wintypes.LPWSTR),
                    ("pOutputFile", wintypes.LPWSTR),
                    ("pDatatype", wintypes.LPWSTR)]

    _EnumPrintersW = _winspool.EnumPrintersW
    _EnumPrintersW.argtypes = [wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    _EnumPrintersW.restype = wintypes.BOOL

    _OpenPrinterW = _winspool.OpenPrinterW
    _OpenPrinterW.argtypes = [wintypes.LPWSTR, ctypes.POINTER(wintypes.HANDLE), ctypes.c_void_p]
    _OpenPrinterW.restype = wintypes.BOOL

    _ClosePrinter = _winspool.ClosePrinter
    _ClosePrinter.argtypes = [wintypes.HANDLE]
    _ClosePrinter.restype = wintypes.BOOL

    _StartDocPrinterW = _winspool.StartDocPrinterW
    _StartDocPrinterW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(DOC_INFO_1W)]
    _StartDocPrinterW.restype = wintypes.DWORD

    _EndDocPrinter = _winspool.EndDocPrinter
    _EndDocPrinter.argtypes = [wintypes.HANDLE]
    _EndDocPrinter.restype = wintypes.BOOL

    _StartPagePrinter = _winspool.StartPagePrinter
    _StartPagePrinter.argtypes = [wintypes.HANDLE]
    _StartPagePrinter.restype = wintypes.BOOL

    _EndPagePrinter = _winspool.EndPagePrinter
    _EndPagePrinter.argtypes = [wintypes.HANDLE]
    _EndPagePrinter.restype = wintypes.BOOL

    _WritePrinter = _winspool.WritePrinter
    _WritePrinter.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                              ctypes.POINTER(wintypes.DWORD)]
    _WritePrinter.restype = wintypes.BOOL

    _AbortPrinter = _winspool.AbortPrinter
    _AbortPrinter.argtypes = [wintypes.HANDLE]
    _AbortPrinter.restype = wintypes.BOOL

    def _winerr(prefix):
        code = ctypes.get_last_error()
        msg = ctypes.FormatError(code).strip() if code else "unknown error"
        return "%s (Windows error %d: %s)" % (prefix, code, msg)


def list_printers() -> list:
    """Names of installed Windows printers (local + connections); [] elsewhere."""
    if sys.platform != "win32":
        return []
    flags = PRINTER_ENUM_LOCAL | PRINTER_ENUM_CONNECTIONS
    needed = wintypes.DWORD(0)
    returned = wintypes.DWORD(0)
    try:
        _EnumPrintersW(flags, None, 4, None, 0, ctypes.byref(needed), ctypes.byref(returned))
        if needed.value == 0:
            return []
        buf = ctypes.create_string_buffer(needed.value)
        ok = _EnumPrintersW(flags, None, 4, buf, needed, ctypes.byref(needed),
                            ctypes.byref(returned))
        if not ok:
            return []
        arr = ctypes.cast(buf, ctypes.POINTER(PRINTER_INFO_4W))
        names = []
        for i in range(returned.value):
            n = arr[i].pPrinterName
            if n and n not in names:
                names.append(n)
        return names
    except Exception:
        return []


def find_cutter_printer(names=None, patterns=("gcc", "expert", "cutter", "plotter")) -> str:
    """Best guess at a cutter among installed printers ("" if none)."""
    names = list_printers() if names is None else names
    for pat in patterns:
        for n in names:
            if pat in n.lower():
                return n
    return ""


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------
_DELIMS = (b";", b"\n", b"\x03")


def _split_chunks(data: bytes, size: int):
    """Split into chunks of <= size bytes, preferring to end on a command
    delimiter (';', LF or ETX) so a cancel never leaves half a command."""
    size = max(1, int(size))
    i = 0
    n = len(data)
    while i < n:
        end = min(n, i + size)
        if end < n:
            best = -1
            for d in _DELIMS:
                k = data.rfind(d, i, end)
                if k > best:
                    best = k
            if best >= i + size // 4:      # don't make chunks tiny
                end = best + 1
        yield data[i:end]
        i = end


class Transport:
    """Base transport.  Subclasses implement ``open``, ``_write_chunk``, ``close``."""

    kind = "base"

    def __init__(self, conn: ConnectionSettings | None = None):
        self.conn = conn or ConnectionSettings()
        self.cancelled = False
        self.cancel_data = b""          # sent after a cancel (e.g. b";PU;")
        self.bytes_sent = 0

    # -- API ------------------------------------------------------------
    def open(self) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def write(self, data: bytes, progress=None, cancel_event=None) -> None:
        if isinstance(data, str):
            data = data.encode("ascii", errors="replace")
        data = bytes(data)
        total = len(data)
        self.cancelled = False
        sent = 0
        delay = max(0, int(self.conn.chunk_delay_ms or 0)) / 1000.0
        if progress:
            _safe_progress(progress, 0, total)
        for chunk in _split_chunks(data, self.conn.chunk_size or 512):
            if cancel_event is not None and cancel_event.is_set():
                self._handle_cancel()
                return
            self._write_chunk(chunk, cancel_event)
            if self.cancelled:
                return
            sent += len(chunk)
            self.bytes_sent += len(chunk)
            if progress:
                _safe_progress(progress, sent, total)
            if delay and sent < total:
                if _sleep(delay, cancel_event):
                    self._handle_cancel()
                    return
        self._drain(cancel_event)
        if self.cancelled:
            return

    def describe(self) -> str:
        return self.conn.describe()

    # -- hooks ------------------------------------------------------------
    def _write_chunk(self, chunk: bytes, cancel_event) -> None:
        raise NotImplementedError

    def _drain(self, cancel_event) -> None:
        pass

    def _handle_cancel(self) -> None:
        self.cancelled = True
        if self.cancel_data:
            try:
                self._write_chunk(self.cancel_data, None)
            except Exception:
                pass

    # -- context manager --------------------------------------------------
    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _safe_progress(cb, sent, total):
    try:
        cb(sent, total)
    except Exception:
        pass


def _sleep(seconds, cancel_event) -> bool:
    """Sleep, returning True early if cancel_event gets set."""
    if cancel_event is None:
        time.sleep(seconds)
        return False
    return bool(cancel_event.wait(seconds))


# ---------------------------------------------------------------------------
# Serial
# ---------------------------------------------------------------------------
_PARITY_MAP = {"N": "N", "E": "E", "O": "O", "M": "M", "S": "S"}
_FLOW_NAMES = dict(FLOW_CONTROLS)


class SerialTransport(Transport):
    """pyserial transport.  ``conn.port`` may be "COM4" or any pyserial URL
    (``loop://``, ``socket://host:port``, ``rfc2217://...``)."""

    kind = "serial"
    poll_interval = 0.02

    def __init__(self, conn=None):
        super().__init__(conn)
        self.ser = None

    def open(self) -> None:
        if serial is None:
            raise TransportNotAvailable("pyserial is not available in this Python; "
                                        "serial ports cannot be used.")
        c = self.conn
        port = (c.port or "").strip()
        if not port:
            raise TransportError("No serial port selected. Choose the cutter's COM port "
                                 "under Connection.")
        try:
            ser = serial.serial_for_url(port, do_not_open=True)
            ser.baudrate = int(c.baudrate)
            ser.bytesize = int(c.bytesize)
            ser.parity = _PARITY_MAP.get(str(c.parity).upper()[:1], "N")
            sb = float(c.stopbits)
            ser.stopbits = 1.5 if sb == 1.5 else int(sb)
            flow = (c.flow or "none").lower()
            ser.xonxoff = flow == "xonxoff"
            ser.rtscts = flow == "rtscts"
            ser.dsrdtr = flow == "dsrdtr"
            ser.timeout = 0.1
            ser.write_timeout = max(1.0, float(c.timeout_s or 120.0))
            ser.open()
        except ValueError as e:
            raise TransportError("Invalid serial settings for %s: %s" % (port, e))
        except serial.SerialException as e:
            raise TransportError(self._open_error_message(port, e))
        except OSError as e:
            raise TransportError(self._open_error_message(port, e))
        self.ser = ser

    @staticmethod
    def _open_error_message(port, e):
        s = str(e)
        low = s.lower()
        if "filenotfound" in low or "cannot find" in low or "no such file" in low:
            avail = ", ".join(d for d, _ in list_serial_ports()) or "none"
            return ("Serial port %s was not found. Is the cutter switched on and connected? "
                    "Available ports: %s." % (port, avail))
        if "access is denied" in low or "permissionerror" in low or "permission denied" in low:
            return ("Serial port %s is in use by another program (close GreatCut, InkCut or "
                    "other cutter software) or access was denied." % port)
        return "Could not open serial port %s: %s" % (port, s)

    def _flow_hint(self):
        return _FLOW_NAMES.get(self.conn.flow, self.conn.flow)

    def _timeout_error(self):
        return TransportTimeout(
            "The cutter stopped accepting data on %s for %.0f s. Check that it is switched "
            "on, has media loaded and is ONLINE, the cable, and that flow control (%s) and "
            "baud rate (%d) match the cutter's settings."
            % (self.conn.port, float(self.conn.timeout_s or 0), self._flow_hint(),
               int(self.conn.baudrate)))

    def _out_waiting(self):
        try:
            return int(self.ser.out_waiting)
        except Exception:
            return 0

    def _wait_output_below(self, limit, cancel_event) -> bool:
        """Wait until the OS output queue holds <= limit bytes.  Returns False if
        cancelled.  Raises TransportTimeout if the queue makes no progress for
        conn.timeout_s seconds."""
        timeout = float(self.conn.timeout_s or 120.0)
        last = self._out_waiting()
        last_change = time.monotonic()
        while last > limit:
            if cancel_event is not None and cancel_event.is_set():
                return False
            time.sleep(self.poll_interval)
            cur = self._out_waiting()
            if cur != last:
                last = cur
                last_change = time.monotonic()
            elif time.monotonic() - last_change > timeout:
                raise self._timeout_error()
        return True

    def _write_chunk(self, chunk, cancel_event) -> None:
        if self.ser is None:
            raise TransportError("Serial port is not open.")
        # keep the driver queue short so cancel stays responsive
        if not self._wait_output_below(max(0, int(self.conn.chunk_size or 512)), cancel_event):
            self._handle_cancel()
            return
        view = memoryview(chunk)
        while len(view):
            if cancel_event is not None and cancel_event.is_set():
                self._handle_cancel()
                return
            try:
                n = self.ser.write(view.tobytes())
            except serial.SerialTimeoutException:
                raise self._timeout_error()
            except serial.SerialException as e:
                raise TransportError("Lost connection to %s while sending: %s"
                                     % (self.conn.port, e))
            except Exception as e:      # e.g. queue.Full from loop://
                if type(e).__name__ == "Full":
                    raise self._timeout_error()
                raise TransportError("Error writing to %s: %s" % (self.conn.port, e))
            if n is None:
                n = len(view)
            if n <= 0:
                raise self._timeout_error()
            view = view[n:]

    def _handle_cancel(self) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        if self.ser is None:
            return
        try:
            self.ser.reset_output_buffer()      # drop what is still queued in the PC
        except Exception:
            pass
        if self.cancel_data:
            try:
                self.ser.write(self.cancel_data)
            except Exception:
                pass

    def _drain(self, cancel_event) -> None:
        if self.ser is None:
            return
        if not self._wait_output_below(0, cancel_event):
            self._handle_cancel()

    def close(self) -> None:
        # No flush() here: write() already drained (with timeout/cancel); a
        # blocking flush could hang forever on a stalled handshake.
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def read(self, size=1) -> bytes:
        """Read up to ``size`` bytes (timeout 0.1 s); b'' if nothing."""
        if self.ser is None:
            return b""
        try:
            return self.ser.read(size)
        except Exception:
            return b""


# ---------------------------------------------------------------------------
# Windows printer (raw)
# ---------------------------------------------------------------------------
class PrinterTransport(Transport):
    """Send RAW bytes through the Windows spooler (GCC USB driver installs the
    cutter as a printer).  The whole job is spooled, then released to the
    device on close (EndDocPrinter).  Cancel aborts the spool job."""

    kind = "printer"

    def __init__(self, conn=None, doc_name="VinylCut job"):
        super().__init__(conn)
        self.doc_name = doc_name
        self.handle = None
        self.job_id = 0
        self._page = False

    def open(self) -> None:
        if sys.platform != "win32":
            raise TransportNotAvailable("Printer output is only supported on Windows.")
        name = (self.conn.printer_name or "").strip()
        if not name:
            raise TransportError("No printer selected. Choose the cutter's printer "
                                 "(installed by the GCC USB driver) under Connection.")
        h = wintypes.HANDLE()
        if not _OpenPrinterW(name, ctypes.byref(h), None):
            err = _winerr("Could not open printer '%s'" % name)
            avail = ", ".join(list_printers()) or "none"
            raise TransportError("%s. Installed printers: %s." % (err, avail))
        self.handle = h
        # The RAW document is started lazily on the first write, so that
        # open()+close() (a connection test) does not spool an empty job.

    def _start_doc(self) -> None:
        h = self.handle
        name = self.conn.printer_name
        di = DOC_INFO_1W(self.doc_name, None, "RAW")
        job = _StartDocPrinterW(h, 1, ctypes.byref(di))
        if not job:
            raise TransportError(_winerr("The printer '%s' refused the RAW print job" % name))
        self.job_id = job
        if not _StartPagePrinter(h):
            err = _winerr("Could not start a page on printer '%s'" % name)
            _AbortPrinter(h)
            self.job_id = 0
            raise TransportError(err)
        self._page = True

    def write(self, data, progress=None, cancel_event=None) -> None:
        if self.handle is None:
            raise TransportError("Printer is not open.")
        if not self.job_id and not (cancel_event is not None and cancel_event.is_set()):
            self._start_doc()
        super().write(data, progress=progress, cancel_event=cancel_event)

    def _write_chunk(self, chunk, cancel_event) -> None:
        if self.handle is None or not self.job_id:
            raise TransportError("Printer is not open.")
        buf = ctypes.create_string_buffer(bytes(chunk), len(chunk))
        offset = 0
        while offset < len(chunk):
            written = wintypes.DWORD(0)
            ok = _WritePrinter(self.handle, ctypes.byref(buf, offset), len(chunk) - offset,
                               ctypes.byref(written))
            if not ok:
                raise TransportError(_winerr("Writing to printer '%s' failed"
                                             % self.conn.printer_name))
            if written.value == 0:
                raise TransportTimeout("Printer '%s' accepted no data." % self.conn.printer_name)
            offset += written.value

    def _handle_cancel(self) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        if self.handle is not None:
            if self.job_id:
                try:
                    _AbortPrinter(self.handle)      # deletes the spooled job
                except Exception:
                    pass
                # the spooler may already have passed part of the job to the
                # cutter: send the (tiny) cancel sequence as its own job
                if self.cancel_data:
                    try:
                        self.job_id = 0
                        self._start_doc()
                        self._write_chunk(self.cancel_data, None)
                        _EndPagePrinter(self.handle)
                        _EndDocPrinter(self.handle)
                    except Exception:
                        pass
            try:
                _ClosePrinter(self.handle)
            except Exception:
                pass
            self.handle = None
            self.job_id = 0
            self._page = False

    def close(self) -> None:
        if self.handle is None:
            return
        h = self.handle
        self.handle = None
        err = None
        try:
            if self.job_id:
                if self._page and not _EndPagePrinter(h):
                    err = _winerr("EndPagePrinter failed")
                if not _EndDocPrinter(h):
                    err = err or _winerr("EndDocPrinter failed")
        finally:
            _ClosePrinter(h)
            self._page = False
            self.job_id = 0
        if err:
            raise TransportError("Could not finish the print job on '%s': %s"
                                 % (self.conn.printer_name, err))


# ---------------------------------------------------------------------------
# File
# ---------------------------------------------------------------------------
class FileTransport(Transport):
    """Write bytes to a file.  "{time}" in the path becomes YYYYmmdd-HHMMSS;
    ``~`` and %ENV% variables are expanded."""

    kind = "file"

    def __init__(self, conn=None):
        super().__init__(conn)
        self.fh = None
        self.path = ""
        self._opened = False

    def resolved_path(self) -> str:
        p = (self.conn.file_path or "").strip()
        if not p:
            return ""
        p = p.replace("{time}", time.strftime("%Y%m%d-%H%M%S"))
        return os.path.expandvars(os.path.expanduser(p))

    def open(self) -> None:
        """Validate the path and create its folder.  The file itself is created
        on the first write, so open()+close() (a connection test) leaves no
        empty file behind."""
        p = self.resolved_path()
        if not p:
            raise TransportError("No output file chosen.")
        if os.path.isdir(p):
            raise TransportError("'%s' is a folder; choose a file name." % p)
        d = os.path.dirname(os.path.abspath(p))
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            raise TransportError("Cannot create folder '%s': %s" % (d, e.strerror or e))
        if not os.access(d, os.W_OK):
            raise TransportError("Folder '%s' is not writable." % d)
        self.path = p
        self._opened = True

    def _ensure_file(self):
        if self.fh is None:
            if not getattr(self, "_opened", False):
                raise TransportError("Output file is not open.")
            try:
                self.fh = open(self.path, "wb")
            except OSError as e:
                raise TransportError("Cannot write to '%s': %s" % (self.path, e.strerror or e))

    def write(self, data, progress=None, cancel_event=None) -> None:
        self._ensure_file()
        super().write(data, progress=progress, cancel_event=cancel_event)

    def _write_chunk(self, chunk, cancel_event) -> None:
        self._ensure_file()
        try:
            self.fh.write(chunk)
        except OSError as e:
            raise TransportError("Writing '%s' failed: %s" % (self.path, e.strerror or e))

    def close(self) -> None:
        self._opened = False
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError as e:
                raise TransportError("Closing '%s' failed: %s" % (self.path, e))
            finally:
                self.fh = None


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
class NetworkTransport(Transport):
    """Raw TCP (e.g. a cutter behind a network print server, port 9100)."""

    kind = "network"

    def __init__(self, conn=None):
        super().__init__(conn)
        self.sock = None

    def open(self) -> None:
        host = (self.conn.host or "").strip()
        port = int(self.conn.net_port or 9100)
        if not host:
            raise TransportError("No network host entered.")
        try:
            self.sock = socket.create_connection((host, port),
                                                 timeout=min(15.0, float(self.conn.timeout_s or 15)))
            self.sock.settimeout(float(self.conn.timeout_s or 120.0))
        except socket.timeout:
            raise TransportTimeout("Connecting to %s:%d timed out." % (host, port))
        except OSError as e:
            raise TransportError("Could not connect to %s:%d: %s" % (host, port, e.strerror or e))

    def _write_chunk(self, chunk, cancel_event) -> None:
        if self.sock is None:
            raise TransportError("Network connection is not open.")
        try:
            self.sock.sendall(chunk)
        except socket.timeout:
            raise TransportTimeout("%s:%d stopped accepting data." % (self.conn.host,
                                                                      self.conn.net_port))
        except OSError as e:
            raise TransportError("Connection to %s:%d failed: %s"
                                 % (self.conn.host, self.conn.net_port, e.strerror or e))

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_WR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


# ---------------------------------------------------------------------------
TRANSPORTS = {"serial": SerialTransport, "printer": PrinterTransport,
              "file": FileTransport, "network": NetworkTransport}


def make_transport(conn: ConnectionSettings) -> Transport:
    kind = (getattr(conn, "kind", "") or "").lower()
    try:
        cls = TRANSPORTS[kind]
    except KeyError:
        raise TransportError("Unknown connection type '%s' (expected one of: %s)."
                             % (kind, ", ".join(TRANSPORTS)))
    return cls(conn)
