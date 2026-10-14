import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("collector", Path(__file__).parents[1] / "collector.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_overlaps_duplicates_hardlinks_and_symlinks(self):
        data = self.root / "data"
        data.mkdir()
        child = data / "nested"
        child.mkdir()
        file = child / "file"
        file.write_bytes(b"data" * 4096)
        os.link(file, data / "hardlink")
        outside = self.root / "outside"
        outside.write_bytes(b"outside" * 8192)
        link = data / "symlink"
        link.symlink_to(outside)
        expected = sum(p.lstat().st_blocks * 512 for p in [data, child, file, link])
        result = collector.allocated_bytes([("app", str(data)), ("app", str(child)), ("app", str(data))], ["app", "empty"])
        self.assertEqual(result, {"app": expected, "empty": 0})

    def test_shared_across_namespaces_is_not_double_counted(self):
        with self.assertRaisesRegex(ValueError, "shared"):
            collector.allocated_bytes([("a", str(self.root)), ("b", str(self.root))], ["a", "b"])

    def test_symlink_root_rejected(self):
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            collector.allocated_bytes([("app", str(link))], ["app"])

    def test_disappearing_files_and_permission_failures_invalidate_scan(self):
        file = self.root / "file"
        file.write_text("data")
        original = os.stat
        for error in (FileNotFoundError, PermissionError):
            def broken(path, *args, **kwargs):
                if path == "file":
                    raise error("scan incomplete")
                return original(path, *args, **kwargs)
            with self.subTest(error=error), patch.object(collector.os, "stat", side_effect=broken):
                with self.assertRaises(error):
                    collector.allocated_bytes([("app", str(self.root))], ["app"])

    def volume(self, path):
        return {"status": {"phase": "Bound"}, "spec": {
            "hostPath": {"path": path}, "claimRef": {"namespace": "app"},
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["amdre"]}
            ]}]}},
        }}

    def test_pvc_claim_attribution_and_path_boundaries(self):
        volume = self.volume(collector.HOST_ROOT + "/pvc-123")
        self.assertEqual(collector.volume_roots([volume], "amdre"), [("app", collector.VOLUME_ROOT + "/pvc-123")])
        for path in ["/etc", collector.HOST_ROOT, collector.HOST_ROOT + "/../etc"]:
            with self.subTest(path=path), self.assertRaises(ValueError):
                collector.volume_roots([self.volume(path)], "amdre")
        with self.assertRaises(ValueError):
            collector.volume_roots([volume], "another-node")

    def summary(self):
        return {"pods": [
            {"podRef": {"uid": str(i), "namespace": namespace},
             "ephemeral-storage": {"time": "2026-01-01T00:00:00Z", "usedBytes": value},
             # These are already included in ephemeral-storage, and must not be added again.
             "containers": [{"rootfs": {"usedBytes": 999}}]}
            for i, (namespace, value) in enumerate([("a", 12), ("a", 8), ("b", 7)])
        ]}

    def test_ephemeral_aggregation_and_missing_measurements(self):
        now = 1767225605
        self.assertEqual(collector.ephemeral_bytes(self.summary(), ["a", "b", "c"], now), {"a": 20, "b": 7, "c": 0})
        summary = self.summary()
        del summary["pods"][0]["ephemeral-storage"]
        with self.assertRaises(KeyError):
            collector.ephemeral_bytes(summary, [], now)
        with self.assertRaises(ValueError):
            collector.ephemeral_bytes(self.summary(), [], now + 1000)

    def test_api_failure_does_not_erase_successful_other_kind(self):
        with patch.object(collector, "api_list", side_effect=[[{"metadata": {"name": "a"}}], PermissionError("forbidden")]), \
             patch.object(collector, "api_get", return_value={"pods": []}):
            result = collector.scan("amdre")
        self.assertTrue(result["ephemeral"]["success"])
        self.assertFalse(result["persistent"]["success"])

    def test_scan_timeout_and_crash_do_not_publish_partial_data(self):
        for error in [collector.subprocess.TimeoutExpired("scan", 240), collector.subprocess.CalledProcessError(1, "scan")]:
            with self.subTest(error=error), patch.object(collector.subprocess, "run", side_effect=error) as run:
                self.assertEqual(collector.collect_once(), {})
                self.assertEqual(run.call_args.kwargs["timeout"], 240)

    def test_duplicate_pod_rejected(self):
        summary = self.summary()
        summary["pods"].append(summary["pods"][0])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            collector.ephemeral_bytes(summary, [], 1767225605)

    def test_failed_or_stale_samples_are_omitted_not_zero(self):
        sample = {"success": True, "values": {"a": 1234}, "timestamp": 1000}
        fresh = collector.metrics({"persistent": sample}, "amdre", 1001).decode()
        self.assertIn('workload_namespace="a"} 1234', fresh)
        stale = collector.metrics({"persistent": sample}, "amdre", 1601).decode()
        self.assertNotIn('workload_namespace="a"', stale)
        failed = collector.merge_results({"persistent": sample}, {})
        self.assertEqual(failed["persistent"]["timestamp"], 1000)
        self.assertNotIn('workload_namespace="a"', collector.metrics(failed, "amdre", 1001).decode())


if __name__ == "__main__":
    unittest.main()
