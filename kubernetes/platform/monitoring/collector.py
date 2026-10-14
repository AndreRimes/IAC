"""Single-node disk accounting. HTTP scrapes only read the last completed scan."""

import datetime
import json
import logging
import os
from pathlib import Path, PurePosixPath
import ssl
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INTERVAL = 300
SCAN_TIMEOUT = 240
MAX_AGE = 600
HOST_ROOT = "/opt/local-path-provisioner"
VOLUME_ROOT = "/data/pvcs"
HOST_PATHS = (
    ("media-namespace", "/data/media"),
    ("media", "/data/legacy-media"),
    ("media", "/data/legacy-config"),
    ("media", "/data/legacy-cache"),
)
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def api_get(path):
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    request = urllib.request.Request(
        "https://kubernetes.default.svc" + path,
        headers={"Authorization": "Bearer " + token},
    )
    with urllib.request.urlopen(request, context=context, timeout=15) as response:
        return json.load(response)


def api_list(resource):
    items, continuation = [], ""
    while True:
        query = urllib.parse.urlencode({"limit": 500, "continue": continuation})
        page = api_get(f"/api/v1/{resource}?{query}")
        items.extend(page["items"])
        continuation = page.get("metadata", {}).get("continue", "")
        if not continuation:
            return items


def volume_roots(volumes, node):
    roots = []
    for volume in volumes:
        spec = volume["spec"]
        claim = spec.get("claimRef")
        if not claim or volume.get("status", {}).get("phase") != "Bound":
            continue
        terms = spec.get("nodeAffinity", {}).get("required", {}).get("nodeSelectorTerms", [])
        # shortcut: this collector targets the homelab's one node; review on expansion.
        if not terms or not any(
            any(e.get("key") == "kubernetes.io/hostname" and e.get("operator") == "In"
                and node in e.get("values", []) for e in t.get("matchExpressions", []))
            for t in terms
        ):
            raise ValueError("PVC node affinity is outside the configured single-node scope")
        path = PurePosixPath(spec.get("hostPath", {}).get("path", ""))
        if ".." in path.parts or not path.is_relative_to(HOST_ROOT) or str(path) == HOST_ROOT:
            raise ValueError("PVC path is outside the read-only local-path mount")
        roots.append((claim["namespace"], str(Path(VOLUME_ROOT) / path.relative_to(HOST_ROOT))))
    return roots


def allocated_bytes(roots, namespaces):
    """Count allocated blocks, once per inode, without following symlinks."""
    totals = {namespace: 0 for namespace in namespaces}
    seen = {}

    def account(info, namespace):
        key = (info.st_dev, info.st_ino)
        owner = seen.get(key)
        if owner is not None:
            if owner != namespace:
                raise ValueError("A filesystem object is shared across namespace owners")
            return False
        seen[key] = namespace
        totals[namespace] = totals.get(namespace, 0) + info.st_blocks * 512
        return True

    def fail(error):
        raise error

    for namespace, root in sorted(set(roots), key=lambda item: (len(Path(item[1]).parts), item)):
        # Reject symlink roots/parents too; os.fwalk protects traversal against replacement.
        if str(Path(root).resolve(strict=True)) != os.path.abspath(root):
            raise ValueError("Storage root must not traverse a symlink")
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise ValueError("Storage root must be a directory")
        for _, dirs, files, fd in os.fwalk(root, follow_symlinks=False, onerror=fail):
            if not account(os.fstat(fd), namespace):
                dirs[:] = []
                continue
            for name in files + dirs[:]:
                info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    account(info, namespace)
                    if name in dirs:
                        dirs.remove(name)
    return totals


def ephemeral_bytes(summary, namespaces, now):
    totals = {namespace: 0 for namespace in namespaces}
    seen = set()
    for pod in summary["pods"]:
        ref = pod["podRef"]
        key = ref["uid"]
        if key in seen:
            raise ValueError("Duplicate pod in kubelet summary")
        seen.add(key)
        usage = pod["ephemeral-storage"]
        measured = datetime.datetime.fromisoformat(usage["time"].replace("Z", "+00:00")).timestamp()
        if not 0 <= now - measured <= MAX_AGE:
            raise ValueError("Stale kubelet storage measurement")
        value = usage["usedBytes"]
        if not isinstance(value, int) or value < 0:
            raise ValueError("Invalid ephemeral storage measurement")
        namespace = ref["namespace"]
        totals[namespace] = totals.get(namespace, 0) + value
    return totals


def scan(node):
    namespaces = [item["metadata"]["name"] for item in api_list("namespaces")]
    result = {}
    for kind in ("ephemeral", "persistent"):
        try:
            if kind == "ephemeral":
                summary = api_get(f"/api/v1/nodes/{urllib.parse.quote(node, safe='')}/proxy/stats/summary")
                values = ephemeral_bytes(summary, namespaces, time.time())
            else:
                roots = volume_roots(api_list("persistentvolumes"), node) + list(HOST_PATHS)
                values = allocated_bytes(roots, namespaces)
            result[kind] = {"values": values, "timestamp": time.time(), "success": True}
        except Exception as error:
            logging.error("%s collection failed: %s", kind, error)
            result[kind] = {"success": False}
    return result


def merge_results(previous, result):
    return {
        kind: result.get(kind) if result.get(kind, {}).get("success") else {
            "success": False, "timestamp": previous.get(kind, {}).get("timestamp", 0)
        }
        for kind in ("persistent", "ephemeral")
    }


def metrics(snapshot, node, now):
    names = {
        "infrastructure_namespace_storage_used_bytes": "Allocated storage bytes attributed to a namespace.",
        "infrastructure_storage_collection_success": "Whether the latest scan succeeded and is fresh.",
        "infrastructure_storage_collection_last_success_timestamp_seconds": "Time of last successful scan.",
    }
    lines = []
    for name, help_text in names.items():
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} gauge"])
    for kind in ("persistent", "ephemeral"):
        sample = snapshot.get(kind, {})
        timestamp = sample.get("timestamp", 0)
        fresh = sample.get("success", False) and 0 <= now - timestamp <= MAX_AGE
        labels = f"node={json.dumps(node)},kind={json.dumps(kind)}"
        lines.append(f"infrastructure_storage_collection_success{{{labels}}} {int(fresh)}")
        lines.append(f"infrastructure_storage_collection_last_success_timestamp_seconds{{{labels}}} {timestamp}")
        if fresh:
            for namespace, value in sorted(sample["values"].items()):
                lines.append(f"infrastructure_namespace_storage_used_bytes{{{labels},workload_namespace={json.dumps(namespace)}}} {value}")
    return ("\n".join(lines) + "\n").encode()


def collect_once():
    try:
        child = subprocess.run(
            [sys.executable, __file__, "--scan"], capture_output=True, text=True,
            timeout=SCAN_TIMEOUT, check=True,
        )
        if child.stderr:
            logging.warning("%s", child.stderr.strip())
        return json.loads(child.stdout)
    except (subprocess.SubprocessError, ValueError) as error:
        logging.error("Collection did not complete: %s", type(error).__name__)
        return {}


def serve(node):
    state = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/metrics":
                with lock:
                    body = metrics(state, node, time.time())
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            elif path == "/healthz":
                body = b"ok\n"
                self.send_response(200)
            else:
                body = b"not found\n"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 9108), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while True:
        started = time.monotonic()
        result = collect_once()
        with lock:
            state = merge_results(state, result)
        time.sleep(max(0, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    node_name = os.environ["NODE_NAME"]
    if sys.argv[1:] == ["--scan"]:
        print(json.dumps(scan(node_name)))
    else:
        serve(node_name)
