"""Transport tests.  Never touch real COM ports or printers: serial tests use
pyserial's ``loop://`` URL, printer tests only enumerate / exercise errors."""
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

from inkcut2 import transports
from inkcut2.devices import ConnectionSettings
from inkcut2.transports import (FileTransport, NetworkTransport, PrinterTransport,
                                 SerialTransport, TransportError, TransportTimeout,
                                 make_transport)


class DiscoveryTests(unittest.TestCase):
    def test_list_serial_ports(self):
        ports = transports.list_serial_ports()
        self.assertIsInstance(ports, list)
        for item in ports:
            self.assertEqual(len(item), 2)
            self.assertIsInstance(item[0], str)
            self.assertIsInstance(item[1], str)

    def test_list_printers(self):
        printers = transports.list_printers()
        self.assertIsInstance(printers, list)
        for p in printers:
            self.assertIsInstance(p, str)
        if sys.platform == "win32":
            self.assertTrue(all(p for p in printers))

    def test_find_cutter_printer(self):
        self.assertEqual(transports.find_cutter_printer(["HP LaserJet", "GCC Expert II-24"]),
                         "GCC Expert II-24")
        self.assertEqual(transports.find_cutter_printer(["HP LaserJet"]), "")


class ChunkTests(unittest.TestCase):
    def test_split_prefers_delimiters(self):
        data = b"IN;PU0,0;PD400,0,400,400,0,400,0,0;PU0,0;IN;"
        chunks = list(transports._split_chunks(data, 30))
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(len(c) <= 30 for c in chunks))
        self.assertTrue(all(c.endswith(b";") for c in chunks[:-1]), chunks)
        # a command longer than the chunk size is split anyway
        chunks = list(transports._split_chunks(data, 16))
        self.assertEqual(b"".join(chunks), data)
        self.assertTrue(all(len(c) <= 16 for c in chunks))

    def test_split_without_delimiters(self):
        data = bytes(range(256)) * 3
        chunks = list(transports._split_chunks(data.replace(b";", b"x").replace(b"\n", b"x")
                                               .replace(b"\x03", b"x"), 100))
        self.assertEqual(sum(map(len, chunks)), len(data))
        self.assertTrue(all(len(c) <= 100 for c in chunks))


class MakeTransportTests(unittest.TestCase):
    def test_kinds(self):
        self.assertIsInstance(make_transport(ConnectionSettings(kind="serial")), SerialTransport)
        self.assertIsInstance(make_transport(ConnectionSettings(kind="printer")), PrinterTransport)
        self.assertIsInstance(make_transport(ConnectionSettings(kind="file")), FileTransport)
        self.assertIsInstance(make_transport(ConnectionSettings(kind="network")), NetworkTransport)
        with self.assertRaises(TransportError):
            make_transport(types_ns(kind="carrier-pigeon"))


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class FileTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_bytes_with_progress(self):
        path = os.path.join(self.tmp, "a", "out.plt")
        data = b"IN;" + b"PD1,1;" * 500 + b"IN;"
        seen = []
        t = make_transport(ConnectionSettings(kind="file", file_path=path, chunk_size=100))
        with t:
            t.write(data, progress=lambda s, n: seen.append((s, n)))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), data)
        self.assertEqual(seen[0], (0, len(data)))
        self.assertEqual(seen[-1], (len(data), len(data)))
        self.assertTrue(all(b[0] >= a[0] for a, b in zip(seen, seen[1:])))

    def test_time_placeholder(self):
        t = FileTransport(ConnectionSettings(kind="file",
                                             file_path=os.path.join(self.tmp, "job-{time}.plt")))
        t.open()
        t.write(b"IN;")
        t.close()
        self.assertNotIn("{time}", t.path)
        self.assertTrue(os.path.exists(t.path))

    def test_cancel(self):
        path = os.path.join(self.tmp, "c.plt")
        ev = threading.Event()
        ev.set()
        t = FileTransport(ConnectionSettings(kind="file", file_path=path, chunk_size=4))
        t.cancel_data = b";PU;"
        with t:
            t.write(b"IN;PD1,1;PD2,2;", cancel_event=ev)
        self.assertTrue(t.cancelled)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b";PU;")

    def test_open_close_creates_no_file(self):
        path = os.path.join(self.tmp, "sub", "probe.plt")
        t = FileTransport(ConnectionSettings(kind="file", file_path=path))
        t.open()
        t.close()
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        self.assertFalse(os.path.exists(path))

    def test_errors(self):
        with self.assertRaises(TransportError):
            FileTransport(ConnectionSettings(kind="file", file_path="")).open()
        with self.assertRaises(TransportError):
            FileTransport(ConnectionSettings(kind="file", file_path=self.tmp)).open()


class SerialLoopTests(unittest.TestCase):
    def conn(self, **kw):
        c = dict(kind="serial", port="loop://", baudrate=115200, flow="rtscts", chunk_size=64,
                 timeout_s=5)
        c.update(kw)
        return ConnectionSettings(**c)

    def test_round_trip(self):
        data = (b"IN;VS30;FS80;" + b"".join(b"PD%d,%d;" % (i, i) for i in range(800)) + b"IN;")
        t = SerialTransport(self.conn(stopbits=2, parity="E", bytesize=7, chunk_delay_ms=1))
        t.open()
        received = bytearray()

        def reader():
            while len(received) < len(data):
                b = t.ser.read(4096)
                if b:
                    received.extend(b)

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        progress = []
        try:
            t.write(data, progress=lambda s, n: progress.append(s))
        finally:
            th.join(10)
            t.close()
        self.assertEqual(bytes(received), data)
        self.assertEqual(progress[-1], len(data))
        self.assertFalse(t.cancelled)
        self.assertIsNone(t.ser)

    def test_settings_applied(self):
        t = SerialTransport(self.conn(stopbits=1.5, parity="O", bytesize=7, flow="xonxoff"))
        t.open()
        try:
            self.assertEqual(t.ser.stopbits, 1.5)
            self.assertEqual(t.ser.parity, "O")
            self.assertEqual(t.ser.bytesize, 7)
            self.assertTrue(t.ser.xonxoff)
            self.assertFalse(t.ser.rtscts)
        finally:
            t.close()

    def test_cancel_when_device_blocks(self):
        # Nobody reads the loopback, so its 4 KB buffer fills and stays full,
        # like a cutter holding CTS low.  Cancel must return promptly.
        t = SerialTransport(self.conn(timeout_s=30))
        t.cancel_data = b";PU;"
        t.open()
        ev = threading.Event()
        timer = threading.Timer(0.4, ev.set)
        timer.start()
        t0 = time.monotonic()
        try:
            t.write(b"PD1,1;" * 5000, cancel_event=ev)
            elapsed = time.monotonic() - t0
            self.assertTrue(t.cancelled)
            self.assertLess(elapsed, 5)
            # the queued data was discarded and the cancel sequence written
            self.assertEqual(t.ser.read(100), b";PU;")
        finally:
            timer.cancel()
            t.close()

    def test_cancel_before_start(self):
        t = SerialTransport(self.conn())
        t.open()
        ev = threading.Event()
        ev.set()
        try:
            t.write(b"IN;PD1,1;", cancel_event=ev)
            self.assertTrue(t.cancelled)
            self.assertEqual(t.ser.in_waiting, 0)
        finally:
            t.close()

    def test_stall_raises_timeout(self):
        t = SerialTransport(self.conn(timeout_s=0.5))
        t.open()
        t0 = time.monotonic()
        try:
            with self.assertRaises(TransportTimeout) as cm:
                t.write(b"PD1,1;" * 2000)
            self.assertIn("stopped accepting data", str(cm.exception))
            self.assertLess(time.monotonic() - t0, 10)
        finally:
            t.close()

    def test_open_errors_are_readable(self):
        with self.assertRaises(TransportError) as cm:
            SerialTransport(self.conn(port="")).open()
        self.assertIn("No serial port", str(cm.exception))
        with self.assertRaises(TransportError) as cm:
            # a URL scheme that does not exist -> never touches real hardware
            SerialTransport(self.conn(port="nosuchscheme://x")).open()
        self.assertTrue(str(cm.exception))


class PrinterTests(unittest.TestCase):
    def test_no_printer_name(self):
        with self.assertRaises(TransportError):
            PrinterTransport(ConnectionSettings(kind="printer", printer_name="")).open()

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_unknown_printer(self):
        t = PrinterTransport(ConnectionSettings(kind="printer",
                                                printer_name="InkCut2 nonexistent printer 123"))
        with self.assertRaises(TransportError) as cm:
            t.open()
        self.assertIn("Could not open printer", str(cm.exception))

    @unittest.skipUnless(sys.platform == "win32" and
                         "Microsoft Print to PDF" in transports.list_printers(),
                         "needs the built-in 'Microsoft Print to PDF' printer")
    def test_open_close_spools_nothing(self):
        # open()+close() only opens/closes a printer handle; no document is
        # started, so nothing is printed.
        t = PrinterTransport(ConnectionSettings(kind="printer",
                                                printer_name="Microsoft Print to PDF"))
        t.open()
        self.assertIsNotNone(t.handle)
        self.assertEqual(t.job_id, 0)
        t.close()
        self.assertIsNone(t.handle)


class NetworkTests(unittest.TestCase):
    def test_round_trip(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        got = bytearray()

        def serve():
            c, _ = srv.accept()
            with c:
                while True:
                    b = c.recv(65536)
                    if not b:
                        break
                    got.extend(b)

        th = threading.Thread(target=serve, daemon=True)
        th.start()
        data = b"IN;" + b"PD5,5;" * 1000
        t = make_transport(ConnectionSettings(kind="network", host="127.0.0.1", net_port=port,
                                              chunk_size=256, timeout_s=5))
        with t:
            t.write(data)
        th.join(5)
        srv.close()
        self.assertEqual(bytes(got), data)

    def test_connect_refused(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()   # nothing listens here now
        with self.assertRaises(TransportError):
            NetworkTransport(ConnectionSettings(kind="network", host="127.0.0.1", net_port=port,
                                                timeout_s=2)).open()


if __name__ == "__main__":
    unittest.main()
