"""Synthetic archives only; no Telegram credentials, customer or application startup."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import tarfile
import tracemalloc
import unittest
from unittest.mock import patch

from services import telegram_backup_parts as parts
from services.telegram_document_transport import UploadFailure, upload_document
import httpx


class BackupPartsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root/'original-backup.tar.gz'
        self.path.write_bytes(bytes(range(256))*33)
        self.join = {}
        exec(compile(parts.JOIN_TOOL_SOURCE, 'standalone-join-tool', 'exec'), self.join)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self):
        data = parts.plan_parts(self.path, part_bytes=2048)
        content = self.path.read_bytes()
        offset = 0
        for part in data['parts']:
            (self.root/part['filename']).write_bytes(content[offset:offset+part['size_bytes']])
            offset += part['size_bytes']
        self.path.unlink()
        return data

    def assemble(self, manifest):
        target = self.root/'backup.parts.json'
        target.write_text(json.dumps(manifest), encoding='utf-8')
        return self.join['join_backup'](target)

    def assert_invalid(self, manifest):
        with self.assertRaises((ValueError, OSError)):
            self.assemble(manifest)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.root.glob('.joining-*')), [])

    def test_roundtrip_and_preserve_parts(self):
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        m = self.manifest()
        output = self.assemble(m)
        self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), digest)
        self.assertTrue(all((self.root/p['filename']).exists() for p in m['parts']))

    def test_archive_format_preserved(self):
        with tarfile.open(self.path, 'w:gz') as archive:
            entry = tarfile.TarInfo('synthetic-fixture.txt'); entry.size = 8192
            archive.addfile(entry, io.BytesIO(b'x'*8192))
        m = self.manifest()
        output = self.assemble(m)
        with tarfile.open(output, 'r:gz') as archive:
            self.assertEqual(archive.extractfile('synthetic-fixture.txt').read(), b'x'*8192)

    def test_bundle_busy_does_not_wait_or_send(self):
        parts._BUNDLE_SLOT.acquire()
        try:
            with self.assertRaises(UploadFailure) as caught:
                parts.send_backup_document('fixture', 'fixture', self.path, '')
            self.assertEqual(caught.exception.details['code'], 'upload_busy')
        finally:
            parts._BUNDLE_SLOT.release()

    def test_safe_names(self):
        self.assertEqual(self.join['safe_name']('نسخة-20261017.tar.gz'), 'نسخة-20261017.tar.gz')
        for name in ['../escape', 'a/b', 'a\\b', 'x:y', 'a\x00b', 'a\rb', 'a\nb', '', '..']:
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                self.join['safe_name'](name)

    def test_missing_part(self):
        m = self.manifest(); (self.root/m['parts'][1]['filename']).unlink()
        self.assert_invalid(m)

    def test_corrupted_part(self):
        m = self.manifest(); p = self.root/m['parts'][1]['filename']
        p.write_bytes(b'x'*p.stat().st_size)
        self.assert_invalid(m)

    def test_truncated_part(self):
        m = self.manifest(); (self.root/m['parts'][1]['filename']).write_bytes(b'x')
        self.assert_invalid(m)

    def test_duplicate_part(self):
        m = self.manifest(); m['parts'][1]['filename'] = m['parts'][0]['filename']
        self.assert_invalid(m)

    def test_order(self):
        m = self.manifest(); m['parts'].reverse(); self.assert_invalid(m)

    def test_wrong_whole_hash(self):
        m = self.manifest(); m['sha256'] = '0'*64; self.assert_invalid(m)

    def test_wrong_total(self):
        m = self.manifest(); m['size_bytes'] += 1; self.assert_invalid(m)

    def test_traversal(self):
        m = self.manifest(); m['filename'] = '../escape'; self.assert_invalid(m)

    def test_no_overwrite(self):
        m = self.manifest(); self.path.write_bytes(b'existing')
        with self.assertRaises(FileExistsError): self.assemble(m)
        self.assertEqual(self.path.read_bytes(), b'existing')

    def test_symlink_part(self):
        m = self.manifest(); p = self.root/m['parts'][0]['filename']; saved = self.root/'saved'
        p.rename(saved)
        try: p.symlink_to(saved)
        except OSError: self.skipTest('Host does not permit symlinks')
        self.assert_invalid(m)

    def test_virtual_part_bounds_and_length(self):
        with parts.PartReader(self.path, 1024, 2000) as view:
            self.assertEqual(view.seek(0, 2), 2000)
            self.assertEqual(view.read(), b'')
            view.seek(0); self.assertEqual(view.read(), self.path.read_bytes()[1024:3024])
            with self.assertRaises(ValueError): view.seek(2001)

    def test_virtual_part_real_httpx_multipart(self):
        seen = []
        def reply(request):
            body = request.read(); seen.append(body)
            self.assertEqual(int(request.headers['content-length']), len(body))
            return httpx.Response(200, json={'ok': True})
        with parts.PartReader(self.path, 1024, 2000) as view:
            upload_document('fixture', 'fixture', self.path, '', _file=view,
                            _filename='part001', _transport=httpx.MockTransport(reply))
        self.assertIn(self.path.read_bytes()[1024:3024], seen[0])
        self.assertLess(len(seen[0]), 3000)

    def test_count_empty_and_size_guards(self):
        for value in [0, -1, True, parts.PART_BYTES+1]:
            with self.assertRaises(ValueError): parts.plan_parts(self.path, part_bytes=value)
        with self.assertRaises(ValueError): parts.plan_parts(self.path, part_bytes=1)
        self.path.write_bytes(b'')
        with self.assertRaises(ValueError): parts.plan_parts(self.path)

    def test_small_backup_one_document(self):
        seen = []
        def send(*args, **kwargs): seen.append(kwargs); return {'ok': True}, {}
        parts.send_backup_document('fixture', 'fixture', self.path, '', _send=send)
        self.assertEqual(seen, [{'total_timeout': 600.0}])

    def test_45mb_single_document_boundary_and_upload_budget(self):
        for size, expected_calls in [(45_000_000, 1), (45_000_001, 4)]:
            with self.path.open('wb') as stream:
                stream.truncate(size)
            seen = []
            def send(*args, **kwargs):
                seen.append(kwargs)
                return {'ok': True, 'result': {'message_id': len(seen)}}, {}
            parts.send_backup_document('fixture', 'fixture', self.path, '', _send=send)
            # Split case sends manifest, two parts and the join utility.
            self.assertEqual(len(seen), expected_calls)
            if expected_calls == 1:
                self.assertAlmostEqual(seen[0]['total_timeout'], size/32768.0+120.0)
                self.assertNotIn('_file', seen[0])

    def test_existing_5gib_archive_ceiling_preserved(self):
        with self.path.open('wb') as stream:
            stream.truncate(5*1024*1024*1024+1)
        with self.assertRaises(ValueError):
            parts.plan_parts(self.path)

    def test_partial_uncertain_stops_without_replay_and_slot_released(self):
        seen = []
        def send(*args, **kw):
            seen.append(kw['_filename'])
            if len(seen) == 3: raise UploadFailure('read_timeout', 'response', {}, True)
            return {'ok': True, 'result': {'message_id': len(seen)}}, {}
        with self.assertRaises(UploadFailure) as caught:
            parts.send_backup_document('fixture', 'fixture', self.path, '', _part_bytes=2048, _send=send)
        self.assertEqual(len(seen), 3)
        self.assertEqual(len(caught.exception.details['confirmed_documents']), 2)
        self.assertFalse(caught.exception.details['bundle_complete'])
        self.assertTrue(caught.exception.details['outcome_uncertain'])
        self.assertTrue(parts._BUNDLE_SLOT.acquire(False)); parts._BUNDLE_SLOT.release()

    def test_reject_part_not_success(self):
        def send(*args, **kw): return {'ok': False, 'error_code': 403}, {}
        with self.assertRaises(UploadFailure) as caught:
            parts.send_backup_document('fixture', 'fixture', self.path, '', _part_bytes=2048, _send=send)
        self.assertFalse(caught.exception.details['bundle_complete'])

    def test_changed_archive_stops(self):
        def send(*args, **kw):
            self.path.write_bytes(b'changed')
            return {'ok': True}, {}
        with self.assertRaises(UploadFailure) as caught:
            parts.send_backup_document('fixture', 'fixture', self.path, '', _part_bytes=2048, _send=send)
        self.assertEqual(caught.exception.details['code'], 'backup_changed')

    def test_global_deadline(self):
        clock = iter([0, 7201])
        with self.assertRaises(UploadFailure) as caught:
            parts.send_backup_document('fixture', 'fixture', self.path, '', _part_bytes=2048,
                                       _clock=lambda: next(clock))
        self.assertEqual(caught.exception.details['code'], 'bundle_deadline')

    def test_large_101mib_end_to_end_bounded_memory(self):
        # No full archive buffering: transfer all virtual parts to a local sink,
        # then run the exact standalone verifier. Multipart is tested separately.
        chunk = bytes(range(256))*256
        with self.path.open('wb') as out:
            for _ in range(101*16): out.write(chunk)
            out.write(b'end')
        destination = self.root/'received'; destination.mkdir()
        calls = []
        def send(token, chat, path, caption, **kw):
            stream = kw['_file']; name = kw['_filename']; calls.append(name)
            with (destination/name).open('wb') as out:
                for data in iter(lambda: stream.read(65536), b''): out.write(data)
            return {'ok': True, 'result': {'message_id': len(calls)}}, {'attempts': 1}
        tracemalloc.start()
        response, progress = parts.send_backup_document('fixture', 'fixture', self.path, '', _send=send)
        mpath = destination/calls[0]
        joined = self.join['join_backup'](mpath)
        _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        self.assertEqual(response['_bundle']['part_count'], 3)
        self.assertEqual(len(calls), 5)
        self.assertTrue(progress['bundle_complete'])
        self.assertEqual(joined.stat().st_size, 101*1024*1024+3)
        with joined.open('rb') as stream:
            self.assertEqual(hashlib.file_digest(stream, 'sha256').hexdigest(), response['_bundle']['sha256'])
        self.assertLess(peak, 4*1024*1024)


if __name__ == '__main__': unittest.main()
