"""The administrator account's gateway (files/gateway.py) as a process: forwards to 127.0.0.1."""
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unittest

GATEWAY = Path(__file__).resolve().parents[1] / 'files/gateway.py'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Echo(socketserver.BaseRequestHandler):
    """Replies with the peer address, then echoes everything until the client half-closes."""

    def handle(self):
        self.request.sendall(f'peer={self.client_address[0]}\n'.encode())
        while data := self.request.recv(65536):
            self.request.sendall(data)
        self.request.sendall(b'<eof>')


class GatewayChecks(unittest.TestCase):
    def setUp(self):
        self.target = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
        threading.Thread(target=self.target.serve_forever, daemon=True).start()
        self.port = free_port()
        self.process = subprocess.Popen([sys.executable, '-I', str(GATEWAY)], stdout=subprocess.PIPE, text=True,
                                        env={**os.environ, 'GATEWAY_PORT': str(self.port), 'TARGET_PORT': str(self.target.server_address[1])})
        self.assertIn('forwarding', self.process.stdout.readline())

    def tearDown(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(10)
        self.process.stdout.close()
        self.target.shutdown()
        self.target.server_close()

    def exchange(self, payload):
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as s:
            s.sendall(payload)
            s.shutdown(socket.SHUT_WR)
            received = b''
            while data := s.recv(65536):
                received += data
        return received

    def test_forwards_to_loopback_and_keeps_half_closed_connections(self):
        payload = os.urandom(300_000)
        received = self.exchange(payload)
        header, _, rest = received.partition(b'\n')
        self.assertEqual(header, b'peer=127.0.0.1')
        self.assertEqual(rest, payload + b'<eof>')

    def test_concurrent_connections(self):
        results = []
        threads = [threading.Thread(target=lambda i=i: results.append(self.exchange(bytes([i]) * 1000))) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(len(results), 20)

    def test_sigterm_with_an_open_connection_exits_after_the_grace_period(self):
        with socket.create_connection(('127.0.0.1', self.port), timeout=10) as s:
            self.assertTrue(s.recv(100).startswith(b'peer='))  # forwarded and still open
            started = time.monotonic()
            self.process.terminate()
            self.assertEqual(self.process.wait(15), 0)
            self.assertLess(time.monotonic() - started, 10)
            s.settimeout(5)
            self.assertEqual(s.recv(100), b'')  # closed by the gateway

    def test_unreachable_target_closes_the_connection(self):
        self.target.shutdown()
        self.target.server_close()
        time.sleep(0.1)
        self.assertEqual(self.exchange(b'x'), b'')
        self.assertIsNone(self.process.poll())


if __name__ == '__main__':
    unittest.main()
