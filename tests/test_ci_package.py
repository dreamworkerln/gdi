"""Contract checks for one-file CI result delivery; no remote services required."""

import copy
from concurrent.futures import ThreadPoolExecutor
import stat
import threading
import zipfile
from unittest.mock import patch

from gdi.ci_package import build_package, package_checksum, read_package
from gdi.ci import CiClient
from gdi.exchange import decode, digest, encode, file_digest
from gdi.git import GdiError
from tests.test_ci import CiTestCase


class CiPackageTests(CiTestCase):
    def test_concurrent_clients_can_populate_the_same_empty_package_cache(self):
        _, req, _, _, _ = self.package()
        clients = [CiClient(self.ea, 'drive'), CiClient(self.ea, 'drive')]
        barrier = threading.Barrier(2)
        download = self.store.download
        def synchronized(path, target):
            download(path, target)
            if '/result-' in path:
                barrier.wait(timeout=5)
        with patch.object(self.store, 'download', side_effect=synchronized):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda client: client.status(req['job_id']), clients))
        self.assertTrue(all(result['verified'] and result['state'] == 'PASS' for result in results))

    def test_client_downloads_one_package_and_repairs_local_files_without_redownload(self):
        worker, req, _, _, _ = self.package()
        client = CiClient(self.ea, 'drive')
        self.store.downloads.clear()
        result = client.status(req['job_id'])
        self.assertEqual((result['state'], result['verified']), ('PASS', True))
        self.assertEqual(len(self.store.downloads), 1)
        self.assertTrue(self.store.downloads[0].endswith('.zip'))
        self.assertFalse(any('/log-chunks/' in path or '/events/' in path for path in self.store.data))
        local = client.root / 'results' / req['job_id'] / 'build.log'
        local.write_bytes(b'corrupted cache')
        self.store.downloads.clear()
        self.assertTrue(client.status(req['job_id'])['verified'])
        self.assertEqual(local.read_bytes(), (worker.spool(req['job_id']) / 'build.log').read_bytes())
        self.assertEqual(self.store.downloads, [])

    def package(self):
        worker = self.worker()
        client, req = self.submit(worker)
        worker.tick()
        spool = worker.spool(req['job_id'])
        raw = (spool / 'request.json').read_bytes() if (spool / 'request.json').exists() else encode(req)
        result = decode((spool / 'result.json').read_bytes())
        path = build_package(spool, req, digest(raw))
        return worker, req, spool, result, path

    def rewrite(self, path, transform):
        with zipfile.ZipFile(path) as source:
            entries = [(info, source.read(info)) for info in source.infolist()]
        target = self.root / 'changed.zip'
        with zipfile.ZipFile(target, 'w') as archive:
            for info, data in transform(entries):
                archive.writestr(info, data)
        renamed = target.with_name('result-' + file_digest(target) + '.zip')
        target.replace(renamed)
        return renamed

    def test_single_file_has_complete_result_and_reproducible_bytes(self):
        _, req, spool, result, path = self.package()
        expected = {'package.json', 'result.json'} | {item['path'] for item in result['artifacts']}
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            self.assertFalse(any('log-chunks' in name for name in archive.namelist()))
        raw = path.read_bytes()
        path.unlink()
        rebuilt = build_package(spool, req, result['request_sha256'])
        self.assertEqual(rebuilt.read_bytes(), raw)
        target = self.root / 'received'
        self.assertEqual(read_package(rebuilt, req, result['request_sha256'], destination=target), result)
        for spec in result['artifacts']:
            self.assertEqual((target / spec['path']).read_bytes(), (spool / spec['path']).read_bytes())

    def test_changed_archive_bytes_and_foreign_identity_are_rejected(self):
        _, req, _, result, path = self.package()
        original = path.read_bytes()
        path.write_bytes(original + b'changed')
        with self.assertRaisesRegex(GdiError, 'checksum'):
            read_package(path, req, result['request_sha256'])
        path.write_bytes(original)
        foreign = {**req, 'head': 'f' * 40}
        with self.assertRaisesRegex(GdiError, 'identity'):
            read_package(path, foreign, result['request_sha256'])

    def test_missing_extra_duplicate_and_symlink_entries_are_rejected(self):
        _, req, _, result, path = self.package()
        mutations = [
            lambda entries: [(info, data) for info, data in entries if info.filename != 'build.log'],
            lambda entries: entries + [(zipfile.ZipInfo('../outside'), b'bad')],
            lambda entries: entries + [entries[0]],
        ]
        for mutation in mutations:
            changed = self.rewrite(path, mutation)
            with self.assertRaises(GdiError):
                read_package(changed, req, result['request_sha256'], destination=self.root / 'unpack')
        def symlink(entries):
            changed = []
            for info, data in entries:
                info = copy.copy(info)
                if info.filename == 'build.log':
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                changed.append((info, data))
            return changed
        changed = self.rewrite(path, symlink)
        with self.assertRaisesRegex(GdiError, 'entry'):
            read_package(changed, req, result['request_sha256'])
        self.assertFalse((self.root / 'outside').exists())

    def test_artifact_hash_is_checked_even_when_zip_crc_is_valid(self):
        _, req, _, result, path = self.package()
        changed = self.rewrite(path, lambda entries: [
            (info, bytes(byte ^ 1 for byte in data) if info.filename == 'build.log' else data)
            for info, data in entries])
        with self.assertRaisesRegex(GdiError, 'checksum/size'):
            read_package(changed, req, result['request_sha256'])

    def test_pass_without_blocking_stages_is_rejected_inside_package(self):
        _, req, _, result, path = self.package()
        def remove_stages(entries):
            rewritten = []
            for info, data in entries:
                if info.filename == 'result.json':
                    value = decode(data)
                    value['stages'] = []
                    data = encode(value)
                rewritten.append((info, data))
            return rewritten
        changed = self.rewrite(path, remove_stages)
        with self.assertRaisesRegex(GdiError, 'blocking'):
            read_package(changed, req, result['request_sha256'])

    def test_finalized_spool_corruption_cannot_be_repackaged(self):
        _, req, spool, result, _ = self.package()
        (spool / 'build.log').write_bytes(b'corrupt durable log')
        with self.assertRaisesRegex(GdiError, 'checksum/size'):
            build_package(spool, req, result['request_sha256'])

    def test_empty_and_binary_log_are_preserved_exactly(self):
        _, req, spool, result, path = self.package()
        for data in (b'', b'\x00\xff\r\n\x80binary\x00'):
            path.unlink()
            (spool / 'build.log').write_bytes(data)
            for spec in result['artifacts']:
                if spec['path'] == 'build.log':
                    spec.update(bytes=len(data), sha256=digest(data))
            (spool / 'result.json').write_bytes(encode(result))
            path = build_package(spool, req, result['request_sha256'])
            target = self.root / ('empty' if not data else 'binary')
            read_package(path, req, result['request_sha256'], destination=target)
            self.assertEqual((target / 'build.log').read_bytes(), data)

    def test_package_name_must_be_exact_content_address(self):
        for name in ('result.zip', 'result-' + 'A' * 64 + '.zip', '../result-' + 'a' * 64 + '.zip'):
            with self.assertRaises(GdiError):
                package_checksum(name)
