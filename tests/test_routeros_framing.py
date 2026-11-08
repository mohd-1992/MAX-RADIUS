"""RouterOS framing contracts, isolated from database and application workers."""
import ast
from pathlib import Path
import socket
import struct
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'core/mikrotik_api.py').read_text(encoding='utf8'))
node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'RouterOSApiProtocol')
ns = {'socket': socket, 'struct': struct}
exec(compile(ast.Module(body=[node], type_ignores=[]), 'routeros-framing', 'exec'), ns)
Protocol = ns['RouterOSApiProtocol']


class FragmentedSocket:
    def __init__(self, data, chunk=1):
        self.data = data
        self.chunk = chunk
        self.closed = False

    def recv(self, size):
        count = min(size, self.chunk)
        part, self.data = self.data[:count], self.data[count:]
        return part

    def sendall(self, data):
        pass

    def close(self):
        self.closed = True


class FramingTests(unittest.TestCase):
    def protocol(self, data, chunk=1):
        client = Protocol('192.0.2.1')
        client.sock = FragmentedSocket(data, chunk)
        return client

    def test_all_length_encodings_allow_fragmented_reads(self):
        for wire, length in [(b'\0', 0), (b'\x7f', 127), (b'\x80\x80', 128),
                             (b'\xbf\xff', 16383), (b'\xc0\x40\x00', 16384),
                             (b'\xdf\xff\xff', 2097151),
                             (b'\xe0\x20\x00\x00', 2097152),
                             (b'\xef\xff\xff\xff', 268435455),
                             (b'\xf0\x10\x00\x00\x00', 268435456)]:
            with self.subTest(length=length):
                self.assertEqual(self.protocol(wire)._read_len(), length)

    def test_truncated_length_never_becomes_sentence_end(self):
        for wire in [b'', b'\x80', b'\xc0\x40', b'\xe0\x20\0', b'\xf0\x10\0\0']:
            with self.subTest(wire=wire):
                with self.assertRaises(ConnectionError):
                    self.protocol(wire)._read_len()

    def test_unknown_prefix_rejected(self):
        with self.assertRaises(ValueError):
            self.protocol(b'\xf8')._read_len()

    def test_fragmented_utf8_payload(self):
        word = 'باقة المشترك'.encode()
        self.assertEqual(self.protocol(bytes([len(word)]) + word + b'\0').read_sentence(), ['باقة المشترك'])

    def test_truncated_payload_rejected(self):
        with self.assertRaises(ConnectionError):
            self.protocol(b'\x05ab').read_sentence()

    def test_trap_is_drained_before_done(self):
        words = [b'!trap', b'=message=denied']
        wire = b''.join(bytes([len(w)]) + w for w in words) + b'\0\x05!done\0'
        rows = self.protocol(wire).talk_raw(['/test'])
        self.assertEqual(rows, [('!trap', {'message': 'denied'}), ('!done', {})])

    def test_disconnect_closes_socket(self):
        client = self.protocol(b'\x05ab')
        sock = client.sock
        with self.assertRaises(ConnectionError):
            client.talk_raw(['/test'])
        self.assertTrue(sock.closed)
        self.assertIsNone(client.sock)

    def test_real_socket_fragmented_reply(self):
        reader, writer = socket.socketpair()
        self.addCleanup(reader.close)
        reader.settimeout(2)
        client = Protocol('192.0.2.1')
        client.sock = reader
        payload = b'x' * 16384
        def send():
            try:
                for part in [b'\xc0', b'\x40', b'\0', payload[:7000], payload[7000:], b'\0']:
                    writer.sendall(part)
            finally:
                writer.close()
        thread = threading.Thread(target=send)
        thread.start()
        try:
            self.assertEqual(client.read_sentence(), [payload.decode()])
        finally:
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
