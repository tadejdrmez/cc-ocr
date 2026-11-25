"""RFC 3161 timestamps over the hash manifest (spec 2.11.4, 7.2).

Modes (profile ``timestamp.mode``):

* ``http``          - any RFC 3161 TSA over HTTP(S): the cc-ca TimeStamp service,
                      a QTSP's qualified TSA in production. Configuration only.
* ``openssl-local`` - development/test TSA built with ``openssl ts``; keys are
                      created by ``ccocr init-test-tsa``. Non-qualified.
* ``none``          - no timestamp (report says so).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from asn1crypto import tsp


class TimestampError(RuntimeError):
    pass


@dataclass
class TimestampResult:
    token: bytes  # DER TimeStampResp
    gen_time: str
    tsa: str
    serial: int
    qualified: bool


def _request(data: bytes) -> bytes:
    digest = hashlib.sha256(data).digest()
    req = tsp.TimeStampReq(
        {
            "version": 1,
            "message_imprint": {"hash_algorithm": {"algorithm": "sha256"}, "hashed_message": digest},
            "nonce": int.from_bytes(secrets.token_bytes(8), "big"),
            "cert_req": True,
        }
    )
    return req.dump()


def _parse(resp_der: bytes, data: bytes) -> tuple[str, int, str]:
    resp = tsp.TimeStampResp.load(resp_der)
    status = resp["status"]["status"].native
    if status not in ("granted", "granted_with_mods"):
        raise TimestampError(f"TSA refused the request: {status}")
    signed = resp["time_stamp_token"]["content"]
    info = signed["encap_content_info"]["content"].parsed
    imprint = info["message_imprint"]["hashed_message"].native
    if imprint != hashlib.sha256(data).digest():
        raise TimestampError("timestamp token does not match the manifest hash")
    tsa_name = ""
    try:
        tsa_name = info["tsa"].native and str(info["tsa"].chosen.human_friendly) or ""
    except Exception:
        pass
    return info["gen_time"].native.isoformat(), int(info["serial_number"].native), tsa_name


def _openssl_local(data: bytes, cfg: dict[str, Any]) -> bytes:
    tsa_dir = Path(cfg.get("openssl_dir") or os.environ.get("CCOCR_TEST_TSA_DIR", ""))
    conf = tsa_dir / "tsa.cnf"
    if not conf.exists():
        raise TimestampError(f"test TSA not initialised in {tsa_dir!s} (run: ccocr init-test-tsa {tsa_dir})")
    with tempfile.TemporaryDirectory() as td:
        q = Path(td) / "req.tsq"
        r = Path(td) / "resp.tsr"
        q.write_bytes(_request(data))
        res = subprocess.run(
            ["openssl", "ts", "-reply", "-config", str(conf), "-queryfile", str(q), "-out", str(r)],
            capture_output=True, text=True, cwd=tsa_dir,
        )
        if res.returncode != 0:
            raise TimestampError(f"openssl ts -reply failed: {res.stderr.strip()}")
        return r.read_bytes()


def _http(data: bytes, cfg: dict[str, Any]) -> bytes:
    url = cfg.get("url")
    if not url:
        raise TimestampError("timestamp.url is not set")
    req = urllib.request.Request(url, data=_request(data), headers={"Content-Type": "application/timestamp-query"})
    user, pw = os.environ.get("CCOCR_TSA_USER"), os.environ.get("CCOCR_TSA_PASSWORD")
    if user and pw:
        import base64

        req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode())
    with urllib.request.urlopen(req, timeout=float(cfg.get("timeout", 30))) as resp:
        return resp.read()


def timestamp(data: bytes, cfg: dict[str, Any]) -> TimestampResult | None:
    mode = cfg.get("mode", "none")
    if mode == "none":
        return None
    der = _openssl_local(data, cfg) if mode == "openssl-local" else _http(data, cfg)
    gen_time, serial, tsa_name = _parse(der, data)
    return TimestampResult(token=der, gen_time=gen_time, tsa=tsa_name or mode, serial=serial, qualified=bool(cfg.get("qualified")))


def verify(data_path: Path, token_path: Path, cfg: dict[str, Any]) -> tuple[bool, str]:
    """Verify a .tsr against the data with ``openssl ts -verify``."""
    ca = cfg.get("ca_file") or ""
    untrusted = cfg.get("tsa_cert") or ""
    tsa_dir = cfg.get("openssl_dir") or os.environ.get("CCOCR_TEST_TSA_DIR", "")
    if cfg.get("mode") == "openssl-local" and tsa_dir:
        ca = ca or str(Path(tsa_dir) / "ca.pem")
        untrusted = untrusted or str(Path(tsa_dir) / "tsa.pem")
    if not ca:
        # Without a trust anchor we can still check the imprint.
        try:
            _parse(token_path.read_bytes(), data_path.read_bytes())
            return True, "imprint matches (signature not checked: no CA configured)"
        except TimestampError as e:
            return False, str(e)
    cmd = ["openssl", "ts", "-verify", "-data", str(data_path), "-in", str(token_path), "-CAfile", ca]
    if untrusted:
        cmd += ["-untrusted", untrusted]
    res = subprocess.run(cmd, capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    ok = res.returncode == 0 and "Verification: OK" in out
    line = next((l for l in out.splitlines() if l.startswith("Verification")), out.splitlines()[-1] if out else "")
    return ok, line


TSA_CNF = """\
[ tsa ]
default_tsa = tsa_config

[ tsa_config ]
dir = .
serial = $dir/serial
crypto_device = builtin
signer_cert = $dir/tsa.pem
certs = $dir/tsa.pem
signer_key = $dir/tsa.key
signer_digest = sha256
default_policy = 1.3.6.1.4.1.99999.1.1
other_policies = 1.3.6.1.4.1.99999.1.2
digests = sha256, sha384, sha512
accuracy = secs:1
clock_precision_digits = 0
ordering = no
tsa_name = yes
ess_cert_id_chain = no
ess_cert_id_alg = sha256
"""


def init_test_tsa(directory: Path, name: str = "CC OCR Test TSA") -> None:
    """Create a self-signed test CA and a TSA certificate for ``openssl ts``. NOT for production."""
    d = directory
    d.mkdir(parents=True, exist_ok=True)

    def run(*args: str) -> None:
        res = subprocess.run(["openssl", *args], capture_output=True, text=True, cwd=d)
        if res.returncode != 0:
            raise TimestampError(res.stderr)

    (d / "ext.cnf").write_text(
        "[tsa_ext]\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,nonRepudiation\n"
        "extendedKeyUsage=critical,timeStamping\nsubjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid\n",
        encoding="ascii",
    )
    run("req", "-x509", "-newkey", "rsa:3072", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "3650",
        "-subj", f"/O=CC/CN={name} Root (TEST ONLY)")
    run("req", "-newkey", "rsa:3072", "-nodes", "-keyout", "tsa.key", "-out", "tsa.csr", "-subj", f"/O=CC/CN={name} (TEST ONLY)")
    run("x509", "-req", "-in", "tsa.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "tsa.pem",
        "-days", "1825", "-extfile", "ext.cnf", "-extensions", "tsa_ext")
    (d / "serial").write_text("01\n", encoding="ascii")
    (d / "tsa.cnf").write_text(TSA_CNF, encoding="ascii")
    os.chmod(d / "ca.key", 0o600)
    os.chmod(d / "tsa.key", 0o600)


def serve_test_tsa(directory: Path, host: str = "0.0.0.0", port: int = 3180) -> None:
    """Minimal RFC 3161 HTTP endpoint backed by ``openssl ts``. For tests and demos only."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if not (directory / "tsa.cnf").exists():
        init_test_tsa(directory)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if self.headers.get("Content-Type") != "application/timestamp-query" or not 0 < length < 65536:
                self.send_error(400, "expected application/timestamp-query")
                return
            body = self.rfile.read(length)
            with tempfile.TemporaryDirectory() as td:
                q, r = Path(td) / "q.tsq", Path(td) / "r.tsr"
                q.write_bytes(body)
                res = subprocess.run(
                    ["openssl", "ts", "-reply", "-config", str(directory / "tsa.cnf"), "-queryfile", str(q), "-out", str(r)],
                    capture_output=True, cwd=directory,
                )
                if res.returncode != 0:
                    self.send_error(500, "timestamping failed")
                    return
                data = r.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/timestamp-reply")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: object) -> None:
            print("test-tsa:", fmt % args, flush=True)

    print(f"test TSA (NOT for production) listening on http://{host}:{port}/", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
