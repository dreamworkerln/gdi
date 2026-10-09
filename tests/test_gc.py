import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from gdi.cache import VerifiedCache
from gdi.exchange import Exchange, bundle_prerequisites, decode, digest, encode
from gdi.git import GdiError
from gdi.transport import Rclone
from tests.test_exchange import ExchangeTestCase


class GcTests(ExchangeTestCase):
    def chain(self):
        return self.ea.publications(self.store, self.identity, "refs/heads/main")

    def populate(self):
        self.ea.push("drive")
        for number in range(1, 6):
            self.commit(self.a, f"update {number}")
            self.ea.push("drive", checkpoint_every=2)
        return self.chain()

    def gc(self, **kwargs):
        return self.eb.gc("drive", report=lambda line: None, **kwargs)

    def apply_gc(self, **kwargs):
        return self.gc(apply=True, quiescent=True, **kwargs)

    def test_dry_run_lists_exact_sizes_and_preserves_everything(self):
        chain = self.populate()
        before = copy.deepcopy(self.store.data)
        downloads = list(self.store.downloads)
        status = self.b.snapshot()
        report = []
        plan = self.eb.gc("drive", report=report.append)
        self.assertEqual([item.path for item in plan.candidates], sorted(
            "bundles/" + data["bundle_sha256"] + ".bundle" for _, data in chain[:2]))
        self.assertEqual(plan.reclaim_bytes, sum(len(before[item.path]) for item in plan.candidates))
        self.assertEqual(len(plan.branches[0].checkpoints), 2)
        self.assertEqual(len(plan.branches[0].retained), 4)
        self.assertEqual(self.store.data, before)
        self.assertEqual(self.store.downloads, downloads)
        self.assertEqual(self.store.deletions, [])
        self.assertEqual(self.b.snapshot(), status)
        self.assertFalse(VerifiedCache(self.b, self.identity).path.exists())
        self.assertIn("Dry run", report[-1])

    def test_apply_checks_from_scratch_and_new_client_restores_all_history(self):
        chain = self.populate()
        before = copy.deepcopy(self.store.data)
        cache = VerifiedCache(self.a, self.identity)
        cache_refs = cache.git.text("show-ref")
        status = self.a.snapshot()
        self.store.downloads.clear()
        report = []
        plan = self.ea.gc("drive", apply=True, quiescent=True, report=report.append)
        self.assertEqual(set(self.store.deletions), {item.path for item in plan.candidates})
        self.assertEqual(set(self.store.downloads), {
            "bundles/" + data["bundle_sha256"] + ".bundle" for _, data in chain[2:]})
        self.assertEqual(self.store.data, {path: raw for path, raw in before.items()
                                         if path not in self.store.deletions})
        self.assertEqual(cache.git.text("show-ref"), cache_refs)
        self.assertEqual(self.a.snapshot(), status)
        self.assertTrue(any(line.startswith("Verified refs/heads/main:") for line in report))
        self.assertFalse(VerifiedCache(self.b, self.identity).path.exists())
        self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), chain[-1][1]["head"])
        self.assertTrue(self.b.ancestor(self.first, self.b.oid("HEAD")))
        self.assertEqual(self.b.text("show", self.first + ":file.txt"), "first")
        self.assertEqual(self.apply_gc().candidates, ())

    def test_retention_setting_and_fewer_checkpoints(self):
        self.ea.push("drive")
        self.assertEqual(self.gc().candidates, ())
        self.populate()
        self.assertEqual(len(self.gc(keep_checkpoints=1).candidates), 4)
        self.assertEqual(len(self.gc(keep_checkpoints=2).candidates), 2)
        self.assertEqual(self.gc(keep_checkpoints=10).candidates, ())

    def test_apply_requires_quiescent_confirmation_and_valid_policy(self):
        with patch.object(self.eb, "connect", side_effect=AssertionError("must not access remote")):
            with self.assertRaisesRegex(GdiError, "requires --quiescent"):
                self.gc(apply=True)
            for value in (0, -1, True, "2"):
                with self.subTest(value=value), self.assertRaisesRegex(GdiError, "positive integer"):
                    self.gc(keep_checkpoints=value)

    def test_all_branches_are_kept_and_verified_including_unicode_ref(self):
        main = self.populate()
        self.a.call("switch", "-c", "feature/другая", self.first)
        feature_head = self.commit(self.a, "feature")
        _, publication, _ = self.ea.push("drive")
        feature = self.ea.publications(self.store, self.identity, "refs/heads/feature/другая")
        feature_bundle = "bundles/" + feature[0][1]["bundle_sha256"] + ".bundle"
        self.a.call("switch", "main")
        report = []
        plan = self.eb.gc("drive", apply=True, quiescent=True, report=report.append)
        self.assertEqual({branch.ref for branch in plan.branches}, {"refs/heads/main", "refs/heads/feature/другая"})
        self.assertIn(feature_bundle, self.store.data)
        self.assertTrue(any(publication in line and line.startswith("Verified") for line in report))
        self.assertEqual(self.eb.fetch("drive", "feature/другая")[0], feature_head)
        self.assertEqual(self.eb.pull("drive")[0], main[-1][1]["head"])

    def test_corrupt_retained_bundle_is_not_hidden_by_populated_cache(self):
        chain = self.populate()
        self.eb.fetch("drive")
        for index in (2, -1):
            path = "bundles/" + chain[index][1]["bundle_sha256"] + ".bundle"
            with self.subTest(path=path):
                raw = self.store.data[path]
                self.store.data[path] = raw[:-1] + bytes([raw[-1] ^ 1])
                try:
                    with self.assertRaisesRegex(GdiError, "SHA256"):
                        self.apply_gc()
                    self.assertEqual(self.store.deletions, [])
                finally:
                    self.store.data[path] = raw

    def test_bad_other_branch_prevents_every_deletion(self):
        self.populate()
        self.a.call("switch", "-c", "feature/broken")
        _, publication, _ = self.ea.push("drive")
        path = "updates/" + digest(b"refs/heads/feature/broken") + "/" + publication + ".json"
        self.store.data[path] = self.store.data[path].replace(b'"version":2', b'"version":9')
        with self.assertRaises(GdiError):
            self.apply_gc()
        self.assertEqual(self.store.deletions, [])

    def test_orphan_bundles_are_untouched(self):
        self.populate()
        orphan = "bundles/" + digest(b"unfinished upload") + ".bundle"
        self.store.data[orphan] = b"unfinished upload"
        plan = self.apply_gc()
        self.assertEqual([item.path for item in plan.orphans], [orphan])
        self.assertEqual(self.store.data[orphan], b"unfinished upload")

    def test_unknown_paths_abort_without_deleting(self):
        self.populate()
        for path in ("bundles/README.txt", "bundles/nested/a.bundle", "updates/unknown.json"):
            with self.subTest(path=path):
                self.store.data[path] = b"unknown"
                try:
                    with self.assertRaisesRegex(GdiError, "unexpected"):
                        self.apply_gc()
                    self.assertEqual(self.store.deletions, [])
                finally:
                    del self.store.data[path]

    def test_metadata_under_wrong_branch_directory_is_rejected(self):
        self.populate()
        path = next(path for path in self.store.data if path.startswith("updates/"))
        self.store.data["updates/" + "0" * 64 + "/" + path.rsplit("/", 1)[1]] = self.store.data.pop(path)
        with self.assertRaisesRegex(GdiError, "directory does not match"):
            self.apply_gc()
        self.assertEqual(self.store.deletions, [])

    def test_conflicting_chain_is_rejected(self):
        chain = self.populate()
        data = copy.deepcopy(chain[0][1])
        data["nonce"] = "0" * 32
        raw = encode(data)
        path = "updates/" + digest(b"refs/heads/main") + "/" + digest(raw) + ".json"
        self.store.data[path] = raw
        with self.assertRaisesRegex(GdiError, "conflicting"):
            self.apply_gc()
        self.assertEqual(self.store.deletions, [])

    def test_missing_retained_bundle_is_rejected_but_missing_obsolete_bundle_is_ok(self):
        chain = self.populate()
        needed = "bundles/" + chain[-1][1]["bundle_sha256"] + ".bundle"
        raw = self.store.data.pop(needed)
        with self.assertRaisesRegex(GdiError, "retained bundle is missing"):
            self.apply_gc()
        self.assertEqual(self.store.deletions, [])
        self.store.data[needed] = raw
        obsolete = "bundles/" + chain[0][1]["bundle_sha256"] + ".bundle"
        del self.store.data[obsolete]
        plan = self.apply_gc()
        self.assertEqual(len(plan.candidates), 1)
        self.assertEqual(self.eb.pull("drive")[0], chain[-1][1]["head"])

    def test_non_immediate_base_keeps_older_dependency_chain(self):
        chain = self.populate()
        bundle = self.root / "older-base.bundle"
        self.a.call("bundle", "create", str(bundle), "refs/heads/main", "^" + chain[1][1]["head"])
        raw = bundle.read_bytes()
        bundle_path = "bundles/" + digest(raw) + ".bundle"
        self.store.data[bundle_path] = raw
        path = "updates/" + digest(b"refs/heads/main") + "/" + chain[-1][0] + ".json"
        self.replace_publication(path, base_publication=chain[1][0], base_head=chain[1][1]["head"],
                                 bundle_sha256=digest(raw), bundle_bytes=len(raw),
                                 prerequisites=bundle_prerequisites(bundle))
        plan = self.apply_gc(keep_checkpoints=1)
        for _, data in chain[:2]:
            self.assertIn("bundles/" + data["bundle_sha256"] + ".bundle", self.store.data)
        self.assertEqual(len(plan.branches[0].retained), 4)
        self.assertEqual(self.eb.pull("drive")[0], chain[-1][1]["head"])

    def test_remote_advance_during_verification_aborts_before_deletion(self):
        self.populate()
        def advance():
            self.store.after_download = lambda: None
            self.commit(self.a, "concurrent update")
            self.ea.push("drive")
        self.store.after_download = advance
        with self.assertRaisesRegex(GdiError, "remote changed during GC"):
            self.apply_gc()
        self.assertEqual(self.store.deletions, [])

    def test_repository_replacement_during_verification_aborts(self):
        self.populate()
        def replace():
            value = decode(self.store.data["repository.json"])
            value["repository_id"] = "0" * 32
            self.store.data["repository.json"] = encode(value)
        self.store.after_download = replace
        with self.assertRaisesRegex(GdiError, "repository ID mismatch"):
            self.apply_gc()
        self.assertEqual(self.store.deletions, [])

    def test_partial_delete_failure_can_be_retried(self):
        chain = self.populate()
        delete = self.store.delete_bundle
        def fail_second(path):
            if self.store.deletions:
                raise GdiError("lost connection")
            delete(path)
        with patch.object(self.store, "delete_bundle", side_effect=fail_second):
            with self.assertRaisesRegex(GdiError, "partially cleaned"):
                self.apply_gc()
        self.assertEqual(len(self.store.deletions), 1)
        plan = self.apply_gc()
        self.assertEqual(len(plan.candidates), 1)
        self.assertEqual(len(self.store.deletions), 2)
        self.assertEqual(self.eb.pull("drive")[0], chain[-1][1]["head"])

    def test_lost_delete_acknowledgement_can_be_retried(self):
        self.populate()
        delete = self.store.delete_bundle
        def lost_ack(path):
            delete(path)
            raise GdiError("lost acknowledgement")
        with patch.object(self.store, "delete_bundle", side_effect=lost_ack):
            with self.assertRaisesRegex(GdiError, "could not confirm deletion"):
                self.apply_gc()
        self.assertEqual(len(self.store.deletions), 1)
        self.assertEqual(len(self.apply_gc().candidates), 1)

    def test_backend_not_reflecting_deletion_is_reported(self):
        self.populate()
        with patch.object(self.store, "delete_bundle", return_value=None):
            with self.assertRaisesRegex(GdiError, "deletion was not reflected"):
                self.apply_gc()
        self.assertEqual(len(self.apply_gc().candidates), 2)

    def test_observed_remote_change_during_deletion_is_reported(self):
        self.populate()
        delete = self.store.delete_bundle
        def change_remote(path):
            delete(path)
            self.store.data["bundles/" + digest(b"late orphan") + ".bundle"] = b"late orphan"
        with patch.object(self.store, "delete_bundle", side_effect=change_remote):
            with self.assertRaisesRegex(GdiError, "remote changed"):
                self.apply_gc()
        self.assertEqual(self.apply_gc().candidates, ())

    def test_empty_remote_is_a_noop(self):
        self.assertEqual(self.apply_gc().candidates, ())
        self.assertEqual(self.store.deletions, [])

    def test_rclone_deletion_is_restricted_to_one_bundle(self):
        transport = Rclone("remote:project")
        with patch.object(transport, "call") as call:
            for path in ("", "bundles", "bundles/../repository.json", "repository.json", "updates/x.json"):
                with self.subTest(path=path), self.assertRaises(GdiError):
                    transport.delete_bundle(path)
            call.assert_not_called()
            path = "bundles/" + "a" * 64 + ".bundle"
            transport.delete_bundle(path)
            call.assert_called_once_with("deletefile", "remote:project/" + path)


@unittest.skipUnless(shutil.which("rclone"), "rclone required for local backend integration")
class LocalGcTests(ExchangeTestCase):
    def test_real_cli_gc_then_bootstrap_from_clean_cache(self):
        remote = self.root / "transport"
        env = {**os.environ, "RCLONE_CONFIG_GDITEST_TYPE": "local", "RCLONE_CONFIG": "/dev/null",
               "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1"}
        def cli(path, *args, exit_code=0):
            result = subprocess.run([sys.executable, "-m", "gdi", *args], cwd=path, env=env,
                                    text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, exit_code, result.stdout + result.stderr)
            return result.stdout + result.stderr
        cli(self.a.path, "remote", "add", "localdrive", "gditest:" + str(remote), "--init")
        cli(self.a.path, "push", "localdrive")
        for number in range(1, 6):
            self.commit(self.a, f"real update {number}")
            cli(self.a.path, "push", "localdrive", "--checkpoint-every", "2")
        manifests = {str(path.relative_to(remote)): path.read_bytes() for path in (remote / "updates").rglob("*.json")}
        before = list((remote / "bundles").glob("*.bundle"))
        self.assertEqual(len(before), 6)
        self.assertIn("Candidates: 2 bundles", cli(self.a.path, "gc", "localdrive"))
        self.assertEqual(len(list((remote / "bundles").glob("*.bundle"))), 6)
        self.assertIn("requires --quiescent", cli(self.a.path, "gc", "localdrive", "--apply", exit_code=1))
        result = cli(self.a.path, "gc", "localdrive", "--apply", "--quiescent")
        self.assertIn("Verified refs/heads/main:", result)
        self.assertIn("GC complete: deleted 2 bundles", result)
        self.assertEqual(len(list((remote / "bundles").glob("*.bundle"))), 4)
        self.assertEqual(manifests, {str(path.relative_to(remote)): path.read_bytes()
                                    for path in (remote / "updates").rglob("*.json")})
        identity = Exchange(self.a).remote("localdrive")["repository_id"]
        cli(self.b.path, "remote", "add", "localdrive", "gditest:" + str(remote), "--repository-id", identity)
        cli(self.b.path, "pull", "localdrive")
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))
        self.assertIn("Nothing to delete", cli(self.a.path, "gc", "localdrive", "--apply", "--quiescent"))
