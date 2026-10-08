import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from gdi.cli import main
from gdi.cache import VerifiedCache
from gdi.exchange import Exchange, bundle_prerequisites, decode, digest, encode
from gdi.git import GdiError, Git
from gdi.transport import Rclone, validate_url


class MemoryTransport:
    def __init__(self):
        self.data = {}
        self.directories = {""}
        self.uploads = []
        self.downloads = []
        self.deletions = []
        self.fail = None
        self.after_download = lambda: None

    def mkdir(self, path):
        self.directories.add(path)
        self.directories.update("/".join(path.split("/")[:i]) for i in range(1, len(path.split("/"))))

    def list(self, path, *, recursive=False):
        prefix = path + "/" if path else ""
        result = []
        for item in sorted(set(self.data) | self.directories):
            if item == path or not item.startswith(prefix):
                continue
            relative = item[len(prefix):]
            if recursive or "/" not in relative:
                result.append({"Path": relative, "IsDir": item in self.directories,
                               "Size": len(self.data[item]) if item in self.data else -1})
        return result

    def read(self, path):
        if self.fail == "read":
            raise GdiError("network unavailable")
        if path not in self.data:
            raise GdiError("remote file not found: " + path)
        return self.data[path]

    def upload(self, source, path):
        if self.fail and (self.fail == "upload" or path.startswith(self.fail)):
            raise GdiError("network unavailable during upload")
        if path in self.data and self.data[path] != Path(source).read_bytes():
            raise GdiError("immutable file differs")
        self.uploads.append(path)
        self.data[path] = Path(source).read_bytes()

    def download(self, path, target):
        if self.fail in ("download", path):
            raise GdiError("network unavailable during download")
        Path(target).write_bytes(self.read(path))
        self.downloads.append(path)
        self.after_download()

    def update_advisory(self, source, path):
        if self.fail and (self.fail == "upload" or path.startswith(self.fail)):
            raise GdiError("network unavailable during advisory upload")
        self.data[path] = Path(source).read_bytes()

    def delete_queue(self, path):
        if self.fail in ("delete", path):
            raise GdiError("network unavailable during queue cleanup")
        self.data.pop(path, None)
        self.deletions.append(path)

    def delete_bundle(self, path):
        if self.fail in ("delete", path):
            raise GdiError("network unavailable during delete")
        del self.data[path]
        self.deletions.append(path)


class ExchangeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gdi-test-")
        self.root = Path(self.tmp.name)
        self.store = MemoryTransport()
        self.a = self.repo("a")
        self.first = self.commit(self.a, "first")
        self.b = self.repo("b", clone=self.a)
        self.ea = Exchange(self.a, lambda url: self.store)
        self.eb = Exchange(self.b, lambda url: self.store)
        self.identity = self.ea.add("drive", "memory:project", initialize=True)
        self.eb.add("drive", "memory:project", expected_id=self.identity)

    def tearDown(self):
        self.tmp.cleanup()

    def repo(self, name, *, clone=None, object_format="sha1"):
        path = self.root / name
        if clone:
            clone.call("clone", "--no-local", str(clone.path), str(path))
        else:
            path.mkdir()
            Git(path).call("init", "--quiet", "-b", "main", "--object-format=" + object_format)
        return Git.discover(path)

    def commit(self, git, text):
        (git.path / "file.txt").write_text(text + "\n")
        git.call("add", "file.txt")
        git.call("-c", "user.name=Gdi Test", "-c", "user.email=gdi@example.invalid", "commit", "-qm", text)
        return git.oid("HEAD")

    def published(self):
        self.ea.push("drive")
        return next(path for path in self.store.data if path.startswith("updates/"))

    def replace_publication(self, path, **fields):
        value = decode(self.store.data.pop(path))
        value.update(fields)
        raw = encode(value)
        new_path = path.rsplit("/", 1)[0] + "/" + digest(raw) + ".json"
        self.store.data[new_path] = raw
        return new_path


class ExchangeTests(ExchangeTestCase):
    def test_roundtrip_and_publication_order(self):
        second = self.commit(self.a, "second")
        head, _, created = self.ea.push("drive")
        self.assertTrue(created)
        self.assertEqual(head, second)
        self.assertTrue(self.store.uploads[-2].startswith("bundles/"))
        self.assertTrue(self.store.uploads[-1].startswith("updates/"))
        self.eb.fetch("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)
        self.assertEqual((self.b.path / "file.txt").read_text(), "first\n")
        self.assertEqual(self.b.oid("refs/remotes/drive/main"), second)
        self.assertEqual(self.eb.pull("drive")[0], second)
        self.assertEqual((self.b.path / "file.txt").read_text(), "second\n")
        self.assertEqual(self.eb.pull("drive")[0], second)

    def test_repeated_push_is_noop_and_chain_advances(self):
        first = self.ea.push("drive")
        count = len(self.store.uploads)
        self.assertEqual(self.ea.push("drive"), (first[0], first[1], False))
        self.assertEqual(len(self.store.uploads), count)
        self.commit(self.a, "second")
        second = self.ea.push("drive")
        manifests = [decode(value) for path, value in self.store.data.items() if path.startswith("updates/")]
        self.assertEqual(len(manifests), 2)
        self.assertIn(first[1], [value["previous"] for value in manifests])
        self.assertNotEqual(first[1], second[1])

    def test_dirty_tree_allows_push_and_fetch_but_refuses_pull(self):
        self.commit(self.a, "second")
        (self.a.path / "file.txt").write_text("uncommitted")
        self.ea.push("drive")
        (self.b.path / "local.txt").write_text("keep")
        self.eb.fetch("drive")
        with self.assertRaisesRegex(GdiError, "dirty"):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)
        self.assertEqual((self.b.path / "local.txt").read_text(), "keep")

    def test_divergent_pull_preserves_branch_and_exposes_fetched_ref(self):
        remote_head = self.commit(self.a, "remote")
        local_head = self.commit(self.b, "local")
        self.ea.push("drive")
        with self.assertRaisesRegex(GdiError, "not a fast-forward"):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), local_head)
        self.assertEqual(self.b.oid("refs/remotes/drive/main"), remote_head)
        self.assertEqual((self.b.path / "file.txt").read_text(), "local\n")

    def test_non_fast_forward_push_refused(self):
        self.commit(self.a, "remote")
        self.ea.push("drive")
        self.commit(self.b, "local")
        count = len(self.store.data)
        with self.assertRaisesRegex(GdiError, "not a fast-forward"):
            self.eb.push("drive")
        self.assertEqual(len(self.store.data), count)

    def test_branches_with_slashes_do_not_collide(self):
        for branch in ("feature/a", "feature_a", "ветка"):
            self.a.call("switch", "-c", branch, self.first)
            head = self.commit(self.a, branch)
            self.ea.push("drive")
            self.eb.fetch("drive", branch)
            self.assertEqual(self.b.oid("refs/remotes/drive/" + branch), head)
        directories = {path.split("/")[1] for path in self.store.data if path.startswith("updates/")}
        self.assertEqual(len(directories), 3)

    def test_wrong_bundle_checksum_does_not_import_or_change_tracking(self):
        self.published()
        path = next(path for path in self.store.data if path.startswith("bundles/"))
        self.store.data[path] += b"broken"
        before = self.b.snapshot()
        with self.assertRaisesRegex(GdiError, "SHA256 mismatch"):
            self.eb.fetch("drive")
        self.assertEqual(self.b.snapshot(), before)
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))

    def test_wrong_head_ref_and_repository_id_rejected(self):
        path = self.published()
        original = copy.deepcopy(self.store.data)
        for fields in ({"head": "a" * 40}, {"ref": "refs/heads/other"}, {"repository_id": "0" * 32}):
            with self.subTest(fields=fields):
                self.store.data = copy.deepcopy(original)
                self.replace_publication(path, **fields)
                with self.assertRaises(GdiError):
                    self.eb.pull("drive")
                self.assertEqual(self.b.oid("HEAD"), self.first)
                self.assertIsNone(self.b.oid("refs/remotes/drive/main"))

    def test_bundle_advertising_another_ref_is_rejected(self):
        path = self.published()
        self.a.call("branch", "other")
        bundle = self.root / "other.bundle"
        self.a.call("bundle", "create", str(bundle), "refs/heads/other")
        value = bundle.read_bytes()
        checksum = digest(value)
        self.store.data["bundles/" + checksum + ".bundle"] = value
        self.replace_publication(path, bundle_sha256=checksum, bundle_bytes=len(value))
        with self.assertRaisesRegex(GdiError, "bundle ref or exact HEAD"):
            self.eb.fetch("drive")
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))

    def test_incremental_bundle_disguised_as_full_is_rejected_before_import(self):
        path = self.published()
        second = self.commit(self.a, "second")
        bundle = self.root / "incremental.bundle"
        self.a.call("bundle", "create", str(bundle), "refs/heads/main", "^" + self.first)
        value = bundle.read_bytes()
        checksum = digest(value)
        self.store.data["bundles/" + checksum + ".bundle"] = value
        self.replace_publication(path, head=second, bundle_sha256=checksum, bundle_bytes=len(value))
        with self.assertRaises(GdiError):
            self.eb.fetch("drive")
        # A publication declared as full must be self-contained even if the
        # destination happens to have the actual prerequisite.
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))
        self.assertNotEqual(self.b.call("cat-file", "-e", second, allowed=(0, 1, 128)).returncode, 0)

    def test_branch_change_while_creating_bundle_refuses_publication(self):
        original = self.a.call

        def call(*args, **kwargs):
            if args[:2] == ("bundle", "create"):
                self.commit(self.a, "concurrent")
            return original(*args, **kwargs)

        with patch.object(self.a, "call", side_effect=call), self.assertRaisesRegex(GdiError, "branch changed"):
            self.ea.push("drive")
        self.assertFalse(any(path.startswith("updates/") for path in self.store.data))

    def test_remote_change_before_upload_refuses_publication(self):
        original = self.ea.publications
        calls = []

        def publications(*args):
            value = original(*args)
            calls.append(value)
            return value if len(calls) == 1 else ("f" * 64, {})

        with patch.object(self.ea, "publications", side_effect=publications), self.assertRaisesRegex(GdiError, "remote changed"):
            self.ea.push("drive")
        self.assertFalse(any(path.startswith("bundles/") for path in self.store.data))

    def test_conflict_exposed_during_upload_is_reported(self):
        original = self.store.upload

        def upload(source, path):
            original(source, path)
            if path.startswith("updates/"):
                value = decode(self.store.data[path])
                value["nonce"] = "f" * 32
                raw = encode(value)
                self.store.data[path.rsplit("/", 1)[0] + "/" + digest(raw) + ".json"] = raw

        with patch.object(self.store, "upload", side_effect=upload), self.assertRaisesRegex(GdiError, "concurrent push"):
            self.ea.push("drive")
        self.assertEqual(self.a.oid("HEAD"), self.first)

    def test_repository_identity_is_pinned(self):
        data = decode(self.store.data["repository.json"])
        data["repository_id"] = "0" * 32
        self.store.data["repository.json"] = encode(data)
        with self.assertRaisesRegex(GdiError, "repository ID mismatch"):
            self.eb.fetch("drive")

    def test_metadata_corruption_is_rejected(self):
        path = self.published()
        self.store.data[path] += b" "
        with self.assertRaisesRegex(GdiError, "metadata checksum"):
            self.eb.fetch("drive")

    def test_unfinished_publication_and_retry(self):
        self.store.fail = "updates/"
        with self.assertRaisesRegex(GdiError, "network"):
            self.ea.push("drive")
        self.assertTrue(any(path.startswith("bundles/") for path in self.store.data))
        with self.assertRaisesRegex(GdiError, "no completed publication"):
            self.eb.fetch("drive")
        self.store.fail = None
        self.ea.push("drive")
        self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)

    def test_missing_bundle_and_network_failure_preserve_tracking(self):
        self.published()
        self.eb.fetch("drive")
        prior = self.b.oid("refs/remotes/drive/main")
        self.commit(self.a, "next")
        self.ea.push("drive")
        for fail in ("download", "read"):
            self.store.fail = fail
            with self.assertRaises(GdiError):
                self.eb.pull("drive")
            self.assertEqual(self.b.oid("HEAD"), self.first)
            self.assertEqual(self.b.oid("refs/remotes/drive/main"), prior)
        self.store.fail = None
        self.store.data = {path: value for path, value in self.store.data.items() if not path.startswith("bundles/")}
        with self.assertRaises(GdiError):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)

    def test_competing_publications_are_not_selected_by_time(self):
        path = self.published()
        value = decode(self.store.data[path])
        value["nonce"] = "f" * 32
        raw = encode(value)
        self.store.data[path.rsplit("/", 1)[0] + "/" + digest(raw) + ".json"] = raw
        with self.assertRaisesRegex(GdiError, "concurrent push"):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)

    def test_missing_predecessor_is_not_ignored(self):
        path = self.published()
        self.replace_publication(path, previous="1" * 64)
        with self.assertRaisesRegex(GdiError, "predecessor is missing"):
            self.eb.fetch("drive")

    def test_branch_switch_with_same_head_during_pull_is_detected(self):
        self.commit(self.a, "second")
        self.ea.push("drive")
        self.store.after_download = lambda: self.b.call("switch", "-c", "other")
        with self.assertRaisesRegex(GdiError, "branch or HEAD changed"):
            self.eb.pull("drive")
        self.assertEqual(self.b.branch(), "other")
        self.assertEqual(self.b.oid("HEAD"), self.first)

    def test_dirty_tree_created_during_pull_is_detected(self):
        self.commit(self.a, "second")
        self.ea.push("drive")
        self.store.after_download = lambda: (self.b.path / "local.txt").write_text("concurrent")
        with self.assertRaisesRegex(GdiError, "dirty"):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)

    def test_head_change_during_pull_is_detected(self):
        self.commit(self.a, "second")
        self.ea.push("drive")
        changed = []
        self.store.after_download = lambda: changed.append(self.commit(self.b, "concurrent"))
        with self.assertRaises(GdiError):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), changed[0])

    def test_git_environment_overrides_do_not_redirect_repository(self):
        with patch.dict(os.environ, {"GIT_DIR": str(self.a.path / ".git"), "GIT_WORK_TREE": str(self.a.path)}):
            self.assertEqual(Git.discover(self.b.path).path, self.b.path)

    def test_empty_repository_can_pull_first_commit(self):
        self.ea.push("drive")
        empty = self.repo("empty")
        exchange = Exchange(empty, lambda url: self.store)
        exchange.add("drive", "memory:project", expected_id=self.identity)
        self.assertEqual(exchange.pull("drive")[0], self.first)
        self.assertEqual(empty.oid("HEAD"), self.first)

    def test_multiple_projects_are_independent(self):
        other = MemoryTransport()
        ec = Exchange(self.b, lambda url: other)
        other_id = ec.add("other", "memory:other", initialize=True)
        self.assertNotEqual(other_id, self.identity)
        self.commit(self.b, "other project")
        ec.push("other")
        self.ea.push("drive")
        self.assertNotEqual(other.data, self.store.data)
        with self.assertRaisesRegex(GdiError, "repository ID mismatch"):
            Exchange(self.b, lambda url: other).fetch("drive")

    def test_remote_configuration_and_remove(self):
        self.assertEqual(self.ea.remotes(), [("drive", "memory:project")])
        with self.assertRaisesRegex(GdiError, "already exists"):
            self.ea.add("drive", "memory:project")
        with self.assertRaisesRegex(GdiError, "empty dedicated folder"):
            self.ea.add("another", "memory:project", initialize=True)
        with self.assertRaisesRegex(GdiError, "repository ID mismatch"):
            self.ea.add("another", "memory:project", expected_id="0" * 32)
        self.ea.remove("drive")
        self.assertEqual(self.ea.remotes(), [])
        self.assertIn("repository.json", self.store.data)

    def test_git_remote_name_collision_refused(self):
        self.a.call("remote", "add", "native", "https://example.invalid/repo")
        with self.assertRaisesRegex(GdiError, "tracking namespace"):
            self.ea.add("native", "memory:project")

    def test_shallow_partial_bare_sha256_and_replace_rejected(self):
        self.a.call("clone", "--depth=1", self.a.path.as_uri(), str(self.root / "shallow"))
        with self.assertRaisesRegex(GdiError, "shallow"):
            Git.discover(self.root / "shallow")
        self.a.call("config", "remote.origin.promisor", "true")
        with self.assertRaisesRegex(GdiError, "partial"):
            Git.discover(self.a.path)
        self.a.call("config", "--unset", "remote.origin.promisor")
        self.a.call("init", "--bare", str(self.root / "bare"))
        with self.assertRaisesRegex(GdiError, "bare"):
            Git.discover(self.root / "bare")
        sha = self.root / "sha256"
        sha.mkdir()
        Git(sha).call("init", "--object-format=sha256")
        with self.assertRaisesRegex(GdiError, "SHA-1"):
            Git.discover(sha)
        self.a.call("update-ref", "refs/replace/" + self.first, self.first)
        with self.assertRaisesRegex(GdiError, "replace"):
            Git.discover(self.a.path)

    def test_lfs_and_submodules_rejected(self):
        self.commit(self.a, "version https://git-lfs.github.com/spec/v1")
        with self.assertRaisesRegex(GdiError, "LFS"):
            self.ea.push("drive")
        self.commit(self.a, "normal")
        self.a.call("update-index", "--add", "--cacheinfo", "160000," + self.first + ",sub")
        self.a.call("-c", "user.name=Gdi Test", "-c", "user.email=gdi@example.invalid", "commit", "-qm", "gitlink")
        with self.assertRaisesRegex(GdiError, "submodules"):
            self.ea.push("drive")

    def test_detached_and_unborn_push_errors(self):
        self.a.call("checkout", "--detach")
        with self.assertRaisesRegex(GdiError, "detached"):
            self.ea.push("drive")
        self.ea.push("drive", "main")
        empty = self.repo("unborn")
        with self.assertRaisesRegex(GdiError, "no commit"):
            Exchange(empty, lambda url: self.store).push("drive")

    def test_lock_rejects_second_gdi_operation(self):
        with self.a.lock():
            with self.assertRaisesRegex(GdiError, "another gdi"):
                with self.a.lock():
                    self.fail("second lock acquired")

    def test_unfinished_merge_refuses_pull(self):
        self.published()
        (self.b.path / ".git/MERGE_HEAD").write_text(self.first + "\n")
        with self.assertRaisesRegex(GdiError, "unfinished Git operation"):
            self.eb.pull("drive")

    def series(self, count, *, checkpoint_every=20):
        self.ea.push("drive")
        for number in range(count):
            self.commit(self.a, f"update {number}")
            self.ea.push("drive", checkpoint_every=checkpoint_every)
        return self.ea.publications(self.store, self.identity, "refs/heads/main")

    def test_incremental_normal_mode_transfers_only_new_objects(self):
        (self.a.path / "large.bin").write_bytes(os.urandom(128 * 1024))
        self.a.call("add", "large.bin")
        self.commit(self.a, "large base")
        chain = self.series(1)
        full, delta = chain[0][1], chain[1][1]
        self.assertEqual(full["bundle_kind"], "full")
        self.assertEqual(delta["bundle_kind"], "incremental")
        self.assertEqual(delta["base_publication"], chain[0][0])
        self.assertEqual(delta["base_head"], full["head"])
        self.assertLess(delta["bundle_bytes"], full["bundle_bytes"] // 4)
        self.assertEqual(self.store.downloads, [])
        self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))
        self.assertEqual((self.b.path / "large.bin").read_bytes(), (self.a.path / "large.bin").read_bytes())

    def test_new_client_replays_missed_deltas_in_order(self):
        chain = self.series(3)
        self.eb.pull("drive")
        expected = ["bundles/" + data["bundle_sha256"] + ".bundle" for _, data in chain]
        self.assertEqual(self.store.downloads, expected)
        self.assertEqual(self.b.oid("HEAD"), chain[-1][1]["head"])

    def test_verified_cache_survives_new_exchange_instance_without_downloads(self):
        self.series(2)
        self.eb.fetch("drive")
        self.store.downloads.clear()
        self.store.fail = "download"
        Exchange(self.b, lambda url: self.store).pull("drive")
        self.ea.push("drive")
        self.assertEqual(self.store.downloads, [])
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))

    def test_next_fetch_downloads_only_the_new_delta(self):
        self.series(1)
        self.eb.fetch("drive")
        self.store.downloads.clear()
        self.commit(self.a, "next")
        self.ea.push("drive")
        chain = self.ea.publications(self.store, self.identity, "refs/heads/main")
        self.eb.pull("drive")
        self.assertEqual(self.store.downloads, ["bundles/" + chain[-1][1]["bundle_sha256"] + ".bundle"])

    def test_automatic_checkpoint_bounds_bootstrap_downloads(self):
        chain = self.series(4, checkpoint_every=3)
        self.assertEqual([data["bundle_kind"] for _, data in chain],
                         ["full", "incremental", "incremental", "full", "incremental"])
        old_paths = ["bundles/" + data["bundle_sha256"] + ".bundle" for _, data in chain[:3]]
        for path in old_paths:
            del self.store.data[path]
        self.eb.pull("drive")
        self.assertEqual(self.store.downloads, ["bundles/" + data["bundle_sha256"] + ".bundle"
                                                for _, data in chain[3:]])
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))

    def test_forced_full_checkpoint_at_same_head(self):
        chain = self.series(1)
        head, publication_id, created = self.ea.push("drive", full=True)
        self.assertTrue(created)
        self.assertEqual(head, chain[-1][1]["head"])
        latest = self.ea.publications(self.store, self.identity, "refs/heads/main")[-1]
        self.assertEqual(latest[0], publication_id)
        self.assertEqual(latest[1]["bundle_kind"], "full")
        self.assertEqual(latest[1]["previous"], chain[-1][0])
        self.assertIsNone(latest[1]["base_head"])
        self.assertEqual(latest[1]["prerequisites"], [])
        self.assertFalse(self.ea.push("drive", full=True)[2])

    def test_missing_delta_preserves_refs_and_full_checkpoint_recovers(self):
        chain = self.series(2)
        missing = "bundles/" + chain[1][1]["bundle_sha256"] + ".bundle"
        del self.store.data[missing]
        with self.assertRaisesRegex(GdiError, "not found"):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))
        self.ea.push("drive", full=True)
        self.store.downloads.clear()
        self.eb.pull("drive")
        self.assertEqual(len(self.store.downloads), 1)
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))

    def test_full_checkpoint_recovers_without_private_cache_from_local_history(self):
        chain = self.series(2)
        self.ea.clear_cache("drive")
        self.store.data = {path: value for path, value in self.store.data.items() if not path.startswith("bundles/")}
        self.assertTrue(self.ea.push("drive", full=True)[2])
        self.assertEqual(self.store.downloads, [])
        self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), chain[-1][1]["head"])
        self.assertEqual(len(self.store.downloads), 1)

    def test_interrupted_replay_resumes_without_redownloading_verified_bases(self):
        chain = self.series(2)
        final = "bundles/" + chain[-1][1]["bundle_sha256"] + ".bundle"
        self.store.fail = final
        with self.assertRaisesRegex(GdiError, "network"):
            self.eb.fetch("drive")
        self.assertEqual(len(self.store.downloads), 2)
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))
        self.store.fail = None
        self.store.downloads.clear()
        Exchange(self.b, lambda url: self.store).pull("drive")
        self.assertEqual(self.store.downloads, [final])

    def test_missing_local_cache_is_rebuilt_from_remote(self):
        chain = self.series(2)
        self.eb.fetch("drive")
        self.eb.clear_cache("drive")
        self.store.downloads.clear()
        self.eb.pull("drive")
        self.assertEqual(len(self.store.downloads), len(chain))
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))

    def test_cache_with_missing_objects_is_automatically_rebuilt(self):
        chain = self.series(1)
        self.eb.fetch("drive")
        cache = VerifiedCache(self.b, self.identity)
        shutil.rmtree(cache.path / "objects")
        for name in ("info", "pack"):
            (cache.path / "objects" / name).mkdir(parents=True)
        self.store.downloads.clear()
        self.eb.pull("drive")
        self.assertEqual(len(self.store.downloads), len(chain))
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))

    def test_merge_bundle_supports_multiple_real_prerequisites(self):
        base = self.commit(self.a, "base")
        self.ea.push("drive")
        self.a.call("switch", "-c", "side", self.first)
        (self.a.path / "side.txt").write_text("side\n")
        self.a.call("add", "side.txt")
        self.a.call("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "side")
        self.a.call("switch", "main")
        self.a.call("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "merge", "--no-ff", "side", "-m", "merge")
        self.ea.push("drive")
        chain = self.ea.publications(self.store, self.identity, "refs/heads/main")
        self.assertEqual(chain[-1][1]["prerequisites"], sorted([base, self.first]))
        self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))
        self.assertEqual((self.b.path / "side.txt").read_text(), "side\n")

    def test_incremental_with_incorrect_base_or_prerequisites_is_rejected(self):
        chain = self.series(1)
        tip_id = chain[-1][0]
        path = next(path for path in self.store.data if path.endswith(tip_id + ".json"))
        original = copy.deepcopy(self.store.data)
        for fields in ({"base_head": "f" * 40}, {"base_publication": "f" * 64},
                       {"prerequisites": ["f" * 40]}, {"prerequisites": []}):
            with self.subTest(fields=fields):
                self.store.data = copy.deepcopy(original)
                self.replace_publication(path, **fields)
                self.eb.clear_cache("drive")
                with self.assertRaises(GdiError):
                    self.eb.fetch("drive")
                self.assertIsNone(self.b.oid("refs/remotes/drive/main"))

    def test_corrupted_delta_does_not_import_into_user_repository(self):
        chain = self.series(1)
        head = chain[-1][1]["head"]
        path = "bundles/" + chain[-1][1]["bundle_sha256"] + ".bundle"
        self.store.data[path] += b"corrupt"
        with self.assertRaisesRegex(GdiError, "SHA256 mismatch"):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))
        self.assertNotEqual(self.b.call("cat-file", "-e", head, allowed=(0, 1, 128)).returncode, 0)

    def test_reader_supports_base_earlier_than_previous_publication(self):
        chain = self.series(2)
        path = next(path for path in self.store.data if path.endswith(chain[-1][0] + ".json"))
        bundle = self.root / "earlier-base.bundle"
        self.a.call("bundle", "create", str(bundle), "refs/heads/main", "^" + chain[0][1]["head"])
        raw = bundle.read_bytes()
        checksum = digest(raw)
        self.store.data["bundles/" + checksum + ".bundle"] = raw
        self.replace_publication(path, bundle_sha256=checksum, bundle_bytes=len(raw),
                                 base_publication=chain[0][0], base_head=chain[0][1]["head"],
                                 prerequisites=bundle_prerequisites(bundle))
        self.eb.pull("drive")
        self.assertEqual(len(self.store.downloads), 2)
        self.assertEqual(self.b.oid("HEAD"), self.a.oid("HEAD"))

    def test_actual_prerequisite_outside_declared_base_is_rejected(self):
        chain = self.series(2)
        path = next(path for path in self.store.data if path.endswith(chain[-1][0] + ".json"))
        # Keep the real bundle and its real prerequisite B, but claim that A is
        # enough. Metadata is well-formed, so the Git ancestry check must reject it.
        self.replace_publication(path, base_publication=chain[0][0], base_head=chain[0][1]["head"])
        with self.assertRaises(GdiError):
            self.eb.pull("drive")
        self.assertEqual(self.b.oid("HEAD"), self.first)
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))

    def test_full_checkpoint_cannot_hide_non_fast_forward_history(self):
        chain = self.series(1)
        forged_head = self.commit(self.b, "divergent checkpoint")
        bundle = self.root / "forged-full.bundle"
        self.b.call("bundle", "create", str(bundle), "refs/heads/main")
        raw = bundle.read_bytes()
        checksum = digest(raw)
        self.store.data["bundles/" + checksum + ".bundle"] = raw
        value = dict(chain[-1][1], head=forged_head, bundle_sha256=checksum, bundle_bytes=len(raw),
                     bundle_kind="full", base_publication=None, base_head=None, prerequisites=[],
                     previous=chain[-1][0], nonce="f" * 32)
        manifest = encode(value)
        self.store.data["updates/" + digest(b"refs/heads/main") + "/" + digest(manifest) + ".json"] = manifest
        with self.assertRaises(GdiError):
            self.eb.fetch("drive")
        self.assertEqual(self.b.oid("HEAD"), forged_head)
        self.assertIsNone(self.b.oid("refs/remotes/drive/main"))

    def test_cache_alternate_handles_unicode_spaces_and_quotes_in_repo_path(self):
        self.series(1)
        other = self.repo('копия "with spaces"', clone=self.b)
        exchange = Exchange(other, lambda url: self.store)
        exchange.add("drive", "memory:project", expected_id=self.identity)
        exchange.pull("drive")
        cache = VerifiedCache(other, self.identity)
        self.assertFalse((cache.path / "objects/info/alternates").exists())
        cache.git.call("fsck", "--no-dangling")
        self.assertEqual(other.oid("HEAD"), self.a.oid("HEAD"))

    def test_v1_remote_is_explicitly_rejected(self):
        value = decode(self.store.data["repository.json"])
        value["version"] = 1
        self.store.data["repository.json"] = encode(value)
        with self.assertRaisesRegex(GdiError, "protocol v2"):
            self.eb.fetch("drive")

    def test_checkpoint_interval_one_always_creates_full_bundles(self):
        chain = self.series(2, checkpoint_every=1)
        self.assertTrue(all(data["bundle_kind"] == "full" for _, data in chain))
        for value in (0, -1, True):
            with self.subTest(value=value), self.assertRaisesRegex(GdiError, "positive integer"):
                self.ea.push("drive", checkpoint_every=value)


class ProtocolAndCliTests(unittest.TestCase):
    def test_duplicate_keys_and_invalid_json(self):
        for value in (b'{"version":1,"version":2}', b'[]', b'{', b'\xff'):
            with self.subTest(value=value), self.assertRaises(GdiError):
                decode(value)

    def test_url_validation(self):
        for value in ("--config=x", "gdrive:", "gdrive:../x", "gdrive:x//y", "gdrive:x\ny", ":local:/tmp/a"):
            with self.subTest(value=value), self.assertRaises(GdiError):
                validate_url(value)
        self.assertEqual(validate_url("gdrive:project with spaces"), "gdrive:project with spaces")

    def test_missing_tools_and_invalid_listings(self):
        with patch("gdi.git.subprocess.run", side_effect=FileNotFoundError()), self.assertRaisesRegex(GdiError, "not found"):
            Rclone("remote:path").mkdir("")
        for output in ('{}', '[{"Path":"x","IsDir":false},{"Path":"x","IsDir":false}]', 'broken'):
            with self.subTest(output=output), patch.object(Rclone, "call", return_value=subprocess.CompletedProcess([], 0, output, "")), self.assertRaises(GdiError):
                Rclone("remote:path").list("")

    def test_cli_help_version_and_unsupported_flags(self):
        for arguments in (["-h"], ["--help"], ["--version"], ["-v"], ["pull", "--help"], ["gc", "--help"]):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
                main(arguments)
            self.assertEqual(raised.exception.code, 0)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            main(["push", "drive", "--ci"])
        self.assertEqual(raised.exception.code, 2)

    def test_help_and_version_aliases_work_without_repository_or_transport(self):
        outputs = {}
        for argument in ("-v", "--version", "-h", "--help"):
            output = io.StringIO()
            with patch("gdi.cli.Git.discover", side_effect=AssertionError("must not access Git")), \
                    patch.dict(os.environ, {"TERM": "dumb"}, clear=True), \
                    contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main([argument])
            self.assertEqual(raised.exception.code, 0)
            outputs[argument] = output.getvalue()
            self.assertNotIn("\033[", outputs[argument])
        self.assertEqual(outputs["-v"], outputs["--version"])
        self.assertEqual(outputs["-h"], outputs["--help"])
        version = outputs["-v"]
        self.assertTrue(version.startswith("gdi "))
        self.assertIn("###", version)
        for text in ("usage:", "Quick start", "G D I", "GLOBAL DEFENSE INITIATIVE", "COMMAND & CONQUER"):
            self.assertNotIn(text, version)
        help_text = outputs["-h"]
        self.assertIn("gdi push drive", help_text)
        self.assertIn("gdi gc drive", help_text)
        self.assertIn("Setup: Install.md.", help_text)
        self.assertNotIn("Autonomous CI is planned", help_text)
        self.assertNotIn("###", help_text)

    def test_version_banner_colour_and_plain_text_override(self):
        for environment, color in (({"TERM": "xterm-256color"}, True),
                                   ({"FORCE_COLOR": "1", "TERM": "dumb"}, True),
                                   ({"FORCE_COLOR": "1", "NO_COLOR": "1"}, False)):
            with self.subTest(environment=environment):
                outputs = []
                for argument in ("-v", "--version"):
                    output = io.StringIO()
                    with patch.dict(os.environ, environment, clear=True), \
                            patch.object(output, "isatty", return_value=True), \
                            contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
                        main([argument])
                    self.assertEqual("\033[38;5;178m" in output.getvalue(), color)
                    outputs.append(output.getvalue())
                self.assertEqual(outputs[0], outputs[1])


@unittest.skipUnless(shutil.which("rclone"), "rclone required for local backend integration")
class LocalTransportTests(unittest.TestCase):
    def test_real_cli_roundtrip_without_google_credentials(self):
        with tempfile.TemporaryDirectory(prefix="gdi-local-") as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.mkdir()
            target.mkdir()
            a = Git(source)
            b = Git(target)
            for git in (a, b):
                git.call("init", "-q", "-b", "main")
            (source / "hello.txt").write_text("local backend\n")
            a.call("add", ".")
            a.call("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "first")
            module_root = str(Path(__file__).resolve().parents[1])
            env = {**os.environ, "RCLONE_CONFIG_GDITEST_TYPE": "local", "RCLONE_CONFIG": "/dev/null",
                   "PYTHONPATH": module_root, "PYTHONDONTWRITEBYTECODE": "1"}
            url = "gditest:" + str(root / "transport")

            def cli(path, *args):
                result = subprocess.run([sys.executable, "-m", "gdi", *args], cwd=path,
                                        env=env, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            cli(source, "remote", "add", "drive", url, "--init")
            self.assertIn("Bundle: full", cli(source, "push", "drive"))
            self.assertIn("Already published", cli(source, "push", "drive"))
            for number in range(2):
                (source / "hello.txt").write_text(f"local backend update {number}\n")
                a.call("add", "hello.txt")
                a.call("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", f"update {number}")
                self.assertIn("Bundle: incremental", cli(source, "push", "drive"))
            manifests = [decode(path.read_bytes()) for path in (root / "transport/updates").rglob("*.json")]
            self.assertEqual(sorted(value["bundle_kind"] for value in manifests), ["full", "incremental", "incremental"])
            identity = a.config("gdi.remote.drive.repositoryid")
            cli(target, "remote", "add", "drive", url, "--repository-id", identity)
            cli(target, "fetch", "drive")
            self.assertIsNone(b.oid("HEAD"))
            cli(target, "pull", "drive")
            self.assertEqual(b.oid("HEAD"), a.oid("HEAD"))
            self.assertEqual((target / "hello.txt").read_text(), "local backend update 1\n")
            cli(source, "push", "drive", "--full")
            cli(target, "cache", "clear", "drive")
            cli(target, "pull", "drive")
            self.assertEqual(b.oid("HEAD"), a.oid("HEAD"))
            self.assertIn(identity, cli(target, "remote", "list"))
            cli(target, "remote", "remove", "drive")
            self.assertEqual(cli(target, "remote", "list"), "")


if __name__ == "__main__":
    unittest.main()
