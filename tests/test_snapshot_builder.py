import contextlib
import copy
import io
from pathlib import Path
from unittest.mock import patch

from gdi import agent
from gdi.cli import main
from gdi.publication import decode, encode
from gdi.snapshot import Snapshot
from gdi.snapshot_builder import ADAPTER, SnapshotBuildError, build, summary
from tests.snapshot_captures import capture_snapshot
from tests.test_exchange import ExchangeTestCase


class SnapshotBuilderTests(ExchangeTestCase):
    def capture(self, name='capture', **kwargs):
        return capture_snapshot(self.root / name, self.store, **kwargs)

    def rewrite(self, path, update):
        value = decode(path.read_bytes())
        update(value)
        path.write_bytes(encode(value))

    def first_response(self, capture, key='listings'):
        value = decode(capture.read_bytes())
        return capture.parent / value[key][0]['response']

    def assert_build_failure_preserves_inputs(self, capture, code=None):
        before = {p.relative_to(capture.parent): p.read_bytes()
                  for p in capture.parent.rglob('*') if p.is_file()}
        output = capture.parent / 'assembled.json'
        with self.assertRaises(SnapshotBuildError) as error:
            build(capture, self.identity, output)
        if code:
            self.assertEqual(error.exception.code, code)
        self.assertFalse(output.exists())
        after = {p.relative_to(capture.parent): p.read_bytes()
                 for p in capture.parent.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        return error.exception

    def test_empty_repository_assembled_without_rclone_or_connector_calls(self):
        capture = self.capture()
        with patch('socket.socket', side_effect=AssertionError('unexpected network')):
            result = build(capture, self.identity, capture.parent / 'assembled.json')
        snapshot = Snapshot(result['snapshot'], self.identity)
        self.assertEqual(snapshot.chain, [])
        self.assertEqual(result['adapter'], ADAPTER)
        self.assertEqual(result['validation']['manifests_validated'], 0)
        self.assertFalse(result['validation']['drive_freshness_verified'])

    def test_existing_branch_full_and_delta_clone_from_assembled_snapshot(self):
        self.ea.push('drive')
        self.commit(self.a, 'second'); self.ea.push('drive')
        capture = self.capture()
        result = build(capture, self.identity, capture.parent / 'assembled.json')
        self.assertEqual(result['validation'], {'manifests_validated': 2, 'bundles_required': 2,
                                               'required_bundles_present': 2,
                                               'bundle_contents_verified': False,
                                               'drive_freshness_verified': False})
        restored = agent.clone(result['snapshot'], self.identity, self.root / 'restored')
        self.assertEqual(restored['head'], self.a.oid('HEAD'))
        self.assertEqual(restored['validation']['bundles_verified'], 2)
        self.assertTrue(restored['validation']['git_objects_verified'])
        self.assertEqual((self.root / 'restored/file.txt').read_text(), 'second\n')
        # Normal snapshot still accepts the unchanged v1 schema.
        document = decode(Path(result['snapshot']).read_bytes())
        self.assertEqual(document['snapshot_version'], 1)
        self.assertNotIn('adapter', document)

    def test_metadata_only_snapshot_reports_required_bundles_as_not_downloaded(self):
        manifest = self.published()
        capture = self.capture(downloads=['repository.json', manifest])
        result = build(capture, self.identity, capture.parent / 'assembled.json')
        self.assertEqual(result['validation']['bundles_required'], 1)
        self.assertEqual(result['validation']['required_bundles_present'], 0)
        self.assertFalse(result['validation']['bundle_contents_verified'])

    def test_unknown_adapter_capture_or_response_formats_stop(self):
        for name, mutate in [('adapter', lambda d: d.update(adapter='future-connector-v9')),
                             ('version', lambda d: d.update(capture_version=2))]:
            capture = self.capture(name)
            self.rewrite(capture, mutate)
            self.assert_build_failure_preserves_inputs(capture)
        capture = self.capture('changed-response')
        response = self.first_response(capture)
        self.rewrite(response, lambda d: d['structuredContent'].update(files=None, results=[]))
        self.assert_build_failure_preserves_inputs(capture, 'UNSUPPORTED_RESPONSE')

    def test_truncated_paginated_or_at_limit_responses_are_refused(self):
        for flag, value in [('next_page_token', 'more'), ('nextPageToken', 'more'),
                            ('has_more', True), ('truncated', True), ('incompleteSearch', True)]:
            capture = self.capture(flag)
            self.rewrite(self.first_response(capture), lambda d: d['structuredContent'].update({flag: value}))
            self.assert_build_failure_preserves_inputs(capture, 'INCOMPLETE_LISTING')
        capture = self.capture('limit')
        doc = decode(capture.read_bytes())
        count = len(decode(self.first_response(capture).read_bytes())['structuredContent']['files'])
        doc['listings'][0]['request']['top_k'] = count
        capture.write_bytes(encode(doc))
        self.assert_build_failure_preserves_inputs(capture, 'INCOMPLETE_LISTING')

    def test_missing_listing_or_manifest_is_refused(self):
        self.ea.push('drive')
        for name, key in [('listing', 'listings'), ('manifest', 'downloads')]:
            capture = self.capture(name)
            doc = decode(capture.read_bytes())
            if key == 'listings':
                doc[key] = doc[key][:1]
            else:
                doc[key] = [item for item in doc[key] if not item['local_path'].startswith('downloads/branches/')]
            capture.write_bytes(encode(doc))
            self.assert_build_failure_preserves_inputs(capture)

    def test_duplicate_ids_names_and_unrelated_parents_are_refused(self):
        for name in ('duplicate-id', 'duplicate-name', 'parent'):
            capture = self.capture(name)
            path = self.first_response(capture)
            doc = decode(path.read_bytes())
            files = doc['structuredContent']['files']
            if name == 'duplicate-id':
                files.append(copy.deepcopy(files[0]))
            elif name == 'duplicate-name':
                files[1]['title'] = files[0]['title']
            else:
                files[0]['parent_ids'] = ['unrelated-folder']
            path.write_bytes(encode(doc))
            self.assert_build_failure_preserves_inputs(capture)

    def test_id_url_typo_and_download_metadata_mismatch_are_refused(self):
        capture = self.capture('bad-id')
        self.rewrite(self.first_response(capture),
                     lambda d: d['structuredContent']['files'][0].update(id='mistyped-id'))
        self.assert_build_failure_preserves_inputs(capture, 'INVALID_IDENTITY')
        for field, value in [('id', 'wrong-id'), ('file_name', 'wrong.json'), ('file_size_bytes', 123456), ('mime_type', 'text/plain')]:
            capture = self.capture('bad-download-' + field)
            self.rewrite(self.first_response(capture, 'downloads'), lambda d: d['structuredContent'].update({field: value}))
            self.assert_build_failure_preserves_inputs(capture)

    def test_missing_size_bool_or_negative_sizes_do_not_become_zero(self):
        for number, value in enumerate([None, True, -1, '-1', '', '1.5']):
            capture = self.capture('size-' + str(number))
            self.rewrite(self.first_response(capture),
                         lambda d: next(row for row in d['structuredContent']['files']
                                        if row['file_or_folder'] == 'file').update(size=value))
            self.assert_build_failure_preserves_inputs(capture, 'UNSUPPORTED_RESPONSE')

    def test_unknown_listing_envelope_and_changed_pagination_types_fail_closed(self):
        for name, fields in [('envelope', {'possibly_partial': True}),
                             ('token-type', {'nextPageToken': 0}),
                             ('flag-type', {'has_more': 'false'})]:
            capture = self.capture(name)
            self.rewrite(self.first_response(capture), lambda d: d['structuredContent'].update(fields))
            self.assert_build_failure_preserves_inputs(capture, 'UNSUPPORTED_RESPONSE')

    def test_tool_error_is_not_exposed_or_interpreted_as_empty(self):
        capture = self.capture()
        self.first_response(capture).write_bytes(encode({'isError': True, 'structuredContent': {'files': []},
                                                        'private': 'https://secret.invalid/bearer'}))
        error = self.assert_build_failure_preserves_inputs(capture, 'CONNECTOR_ERROR')
        self.assertNotIn('secret', str(error))

    def test_unchanged_structured_data_is_supported_and_extra_fields_are_ignored(self):
        capture = self.capture()
        for item in decode(capture.read_bytes())['listings']:
            path = capture.parent / item['response']
            data = decode(path.read_bytes())['structuredContent']
            for row in data['files']:
                row['new_optional_field'] = 'future value'
            path.write_bytes(encode(data))
        build(capture, self.identity, capture.parent / 'assembled.json')

    def test_output_and_inputs_cannot_be_overwritten_and_symlinks_are_refused(self):
        capture = self.capture()
        output = capture.parent / 'assembled.json'
        output.write_bytes(b'preserve me')
        with self.assertRaises(SnapshotBuildError):
            build(capture, self.identity, output)
        self.assertEqual(output.read_bytes(), b'preserve me')
        output.unlink()
        with self.assertRaises(SnapshotBuildError):
            build(capture, self.identity, self.root / 'elsewhere.json')
        original = self.first_response(capture)
        moved = original.with_suffix('.original')
        original.rename(moved)
        original.symlink_to(moved.name)
        self.assert_build_failure_preserves_inputs(capture, 'INVALID_INPUT')

    def test_failure_after_validation_cannot_truncate_existing_snapshot(self):
        capture = self.capture()
        output = capture.parent / 'assembled.json'
        def concurrent_creator(source, destination):
            Path(destination).write_bytes(b'concurrent file')
            raise FileExistsError('already exists')
        with patch('gdi.snapshot_builder.os.link', side_effect=concurrent_creator):
            with self.assertRaises(SnapshotBuildError):
                build(capture, self.identity, output)
        self.assertEqual(output.read_bytes(), b'concurrent file')
        self.assertFalse(list(capture.parent.glob('.gdi-snapshot-*.json')))

    def test_wrong_repository_identity_preserves_capture_and_no_output(self):
        capture = self.capture()
        with self.assertRaises(SnapshotBuildError):
            build(capture, 'f' * 32, capture.parent / 'assembled.json')
        self.assertFalse((capture.parent / 'assembled.json').exists())

    def test_cli_success_and_actionable_structured_failure(self):
        capture = self.capture()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = main(['agent', 'snapshot-build', '--capture', str(capture), '--repository-id', self.identity,
                         '--output', str(capture.parent / 'assembled.json')])
        self.assertEqual(code, 0)
        self.assertEqual(decode(stream.getvalue())['validation']['manifests_validated'], 0)
        self.rewrite(capture, lambda d: d.update(adapter='changed-provider'))
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(io.StringIO()):
            code = main(['agent', 'snapshot-build', '--capture', str(capture), '--repository-id', self.identity,
                         '--output', str(capture.parent / 'failed.json')])
        self.assertEqual(code, 1)
        error = decode(stream.getvalue())
        self.assertEqual(error['kind'], 'SNAPSHOT_BUILD_ERROR')
        self.assertEqual(error['code'], 'UNSUPPORTED_ADAPTER')
        self.assertTrue(error['inputs_preserved'])
        self.assertFalse(error['snapshot_written'])
