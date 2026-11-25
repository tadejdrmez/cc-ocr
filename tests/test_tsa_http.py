"""RFC 3161 over HTTP (how the cc-ca TimeStamp service is used), pinned trust download, and TSA failure."""

import hashlib
import shutil
import socket
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from asn1crypto import pem

from ccocr.pipeline import process_batch
from ccocr.profile import load_profile
from ccocr.timestamp import TimestampError, fetch_trust, init_test_tsa
from synth import demo_batch

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def tsa(tmp_path_factory):
    """A test TSA over HTTP plus a static file server for its certificates."""
    from ccocr import timestamp as ts

    d = tmp_path_factory.mktemp("tsa")
    init_test_tsa(d)
    port = _free_port()
    t = threading.Thread(target=ts.serve_test_tsa, args=(d, "127.0.0.1", port), daemon=True)
    t.start()

    class Files(SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=str(d), **k)

        def log_message(self, *a):
            pass

    fport = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", fport), Files)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import time

    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield d, f"http://127.0.0.1:{port}/tsa", f"http://127.0.0.1:{fport}"
    srv.shutdown()


def test_fetch_trust_checks_pin(tsa, tmp_path):
    d, _, files = tsa
    der = pem.unarmor((d / "ca.pem").read_bytes())[2]
    fp = ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    root, chain = fetch_trust(f"{files}/ca.pem", fp, tmp_path / "trust", [f"{files}/tsa.pem"])
    assert root.read_bytes().startswith(b"-----BEGIN CERTIFICATE")
    with pytest.raises(TimestampError):
        fetch_trust(f"{files}/ca.pem", "00" * 32, tmp_path / "bad")


def test_http_tsa_single_file(tsa, tmp_path):
    d, url, _ = tsa
    batch = tmp_path / "b.tif"
    demo_batch(batch)
    profile = load_profile({
        "timestamp": {"mode": "http", "url": url, "ca_file": str(d / "ca.pem"), "chain_file": str(d / "tsa.pem")},
    })
    report = process_batch(batch, profile, tmp_path / "out")
    assert all(doc["timestamp"]["verified"] for doc in report["documents"])
    assert "trusted=True" in report["documents"][0]["timestamp"]["verify_detail"]


def test_unreachable_tsa_delivers_nothing(tmp_path):
    batch = tmp_path / "b.tif"
    demo_batch(batch)
    profile = load_profile({"timestamp": {"mode": "http", "url": f"http://127.0.0.1:{_free_port()}/tsa", "timeout": 3}})
    with pytest.raises(TimestampError):
        process_batch(batch, profile, tmp_path / "out")
    assert not list((tmp_path / "out").glob("*.pdf"))
    assert not [p for p in (tmp_path / "out").iterdir() if p.name.startswith(".staging")]
