# CC OCR

On-premises importer for scanned documents. Drop a scanned batch (PDF or
multi-page TIFF) into a folder; CC OCR splits it into documents, makes them
searchable and writes timestamped PDF/A files with metadata.

This started as **increment 0**, the pipeline proof of concept from the
specification (CC_OCR_Specification v0.4, chapter 10.3), and now includes the
admin console and the operator separation screen. Profiles with automatic
acceptance on run unattended and every output says "auto-accepted"; profiles
with it off wait for an operator to confirm the split.

## What it does

| Step | What happens |
| --- | --- |
| Import | Polls `in/`; takes a file only when its size is stable for N polls and it can be opened, or when `<file>.done` exists. Duplicates (same SHA-256) are refused. Originals go unchanged to `archive/`. |
| Optimise | Auto-levels, despeckle, Sauvola binarisation, border removal, DPI normalisation, skew detection, blank-page detection. The original scan stays the image in the PDF (default). |
| Split proposals | QR separator sheets, blank pages, **text rules** (logical expressions on the page's OCR text), other barcodes, fixed page count. |
| OCR | Per-page orientation and script detection; Latin pages read with `slv+hrv+srp_latn+eng`, Cyrillic with `srp+srp_latn` (configurable); low-confidence pages retried with the other set. |
| Output | **One file per document** by default: PDF/A-3 with the metadata XML embedded (`urn:cc:ocr:metadata:1`, XSD in `src/ccocr/schemas`) and a PAdES document timestamp (RFC 3161, `ETSI.RFC3161`) that Adobe Reader shows and verifies. Optional `package: sidecars` writes PDF/A-2 + `.xml` + `.manifest.xml` + `.tsr` instead. A JSON job report per batch goes to `out/_reports/`. |

```
in/  ->  processing/  ->  out/invoice_batch_001.pdf       searchable PDF/A-3, metadata XML inside, timestamped
                          out/invoice_batch_002.pdf
                          out/_reports/batch.<job>.job-report.json
          archive/<date>/batch.pdf   (original, unchanged)
          error/                     (failed or duplicate batches + .error.txt)
```

With `output.package: sidecars` each document is instead four files:
`.pdf` (PDF/A-2), `.xml`, `.manifest.xml` (SHA-256 of PDF + XML) and `.tsr`
(RFC 3161 token over the manifest).

Downstream systems can read the embedded XML with any PDF library, or with
`ccocr extract-xml file.pdf`.

## Run with Docker

### Windows PC (Docker Desktop with WSL2)

```powershell
docker load -i cc-ocr-0.2.0.tar          # or: docker compose build
$env:CCOCR_DATA="C:\Scan"; docker compose up -d
```

Scanners save into `C:\Scan\in`; results appear in `C:\Scan\out`. The admin
console runs on http://localhost:8080 (see below).

### Linux server (e.g. next to CC_archive)

```bash
docker load -i cc-ocr-0.2.0.tar
CCOCR_DATA=/srv/scan docker compose up -d
```

### Offline transfer

```bash
docker build -t cc-ocr:0.2.0 .
docker save cc-ocr:0.2.0 | gzip > cc-ocr-0.2.0.tar.gz   # copy this file to the target machine
```

### One-off commands

```bash
docker run --rm -v C:\Scan:/data cc-ocr:0.2.0 process /data/batch.pdf --out /data/out
docker run --rm -v C:\Scan:/data cc-ocr:0.2.0 separator-sheet --doctype invoice --doctype contract --out /data/separators.pdf
docker run --rm -v C:\Scan:/data cc-ocr:0.2.0 test-rule /data/batch.pdf --expr 'top contains "račun"'
docker run --rm -v C:\Scan:/data cc-ocr:0.2.0 verify /data/out --tsa-dir /data/.ccocr/test-tsa
```

## Admin console

`docker compose up -d` starts two containers from the same image: `ccocr`
(the folder watcher) and `ccocr-admin` (the web console on port 8080, change
it with `CCOCR_ADMIN_PORT`).

1. On first start the console prints a setup code:
   `docker compose logs ccocr-admin`. Open `http://<host>:8080/setup`, enter
   the code and create the first administrator.
2. The console imports `/app/config/watch.yaml` and its profiles once (mount
   your own config there to import it). After that the database
   (`/data/.ccocr/admin.sqlite`) is the source of the configuration.
3. Every publish or folder change writes `/data/.ccocr/config/watch.yaml` and
   one YAML per published profile. The watcher reloads it within one poll
   interval; no restart is needed. Do not edit those files by hand.

Folders can be entered in the console as they are named on the host
(`C:\OCR_Work_SDS\in`) when the deployment declares its bind mounts in
`CCOCR_HOST_MOUNTS` (host=container pairs separated by `;`, e.g. in `.env`:
`CCOCR_HOST_MOUNTS=C:\cc-ocr\data=/data;C:\OCR_Work_SDS=/mnt/sds`). A folder
outside the shared ones is rejected with a hint; to add one, add it to the
`volumes` of both services and to `CCOCR_HOST_MOUNTS`, then `docker compose up -d`.

What the console covers: profiles (draft, publish, history with compare and
restore, YAML import/export), split rules with a live tester, folders (input
and output settings per folder), separator sheets, document types, trust
services (TimeStamp URL, trust anchors, credentials, connection test; e-seal
settings are stored but sealing is not active yet), local users with the roles
Administrator, Operator and Reviewer, system settings and the append-only audit
log with CSV export. LDAP / Active Directory follows in version 2.

Automatic acceptance of split proposals is **off by default** for new
profiles. Switching it on needs a written reason and is audited. The shipped
example profiles have it on.

### Operator separation screen

A batch from a profile with auto-accept off is analysed once (optimise, codes,
OCR) and parked in `/data/review/<job>/` with page images and the proposed
documents. Nothing reaches the output folder yet. Under **Separation** an
Operator (or an Administrator when separation of duties is off):

- sees every page with its proposed document boundaries, separator sheets,
  blank pages and low-confidence flags, and the OCR text of each page;
- splits or joins documents by clicking between two pages, leaves pages out,
  turns pages that are upside down or sideways (those pages are read again);
- sets the document type and classification per document;
- confirms and releases the batch, or rejects it with a reason.

The console writes `decision.json` into the job folder; the watcher applies
it on its next poll, reusing the OCR it already did, and the job report and
metadata say "confirmed by <login>". A rejected batch moves to `error/` with
the reason. Both are in the audit log. Reordering pages and correcting OCR
text are not part of this version.

Passwords for the TimeStamp service entered in the console are stored in
`/data/.ccocr/secrets.json` (mode 0600); `CCOCR_TSA_USER` / `CCOCR_TSA_PASSWORD`
in the environment take precedence. Put the console behind HTTPS (reverse
proxy) when it is reachable from other machines, and set `CCOCR_HTTPS_ONLY=1`.

## Splitting by text rules

A rule proposes a new document on every page where it is true:

```yaml
split:
  proposals: [qr-separator, blank-page, text-rule]
  rules:
    - name: invoice-header
      expr: 'top matches /(RAČUN|INVOICE)\s+(št|no)\.?/i AND NOT top contains "nadaljevanje"'
      doctype: invoice
    - name: contract
      expr: 'top contains "pogodba" OR top contains "уговор"'
      doctype: contract
```

| Term | Meaning |
| --- | --- |
| `text`, `top`, `bottom` | whole page, top zone, bottom zone (`split.zone`, default 25 %) |
| `contains "x"`, `startswith "x"` | case-insensitive; diacritics count (`č` is not `c`) |
| `matches /regex/i` | Python regular expression, flags `i m s` |
| `page`, `words`, `confidence` | numbers, compared with `== != < <= > >=` |
| `prev_blank`, `first_page` | true/false |
| `AND`, `OR`, `NOT`, `( )` | combine |

Use `ccocr test-rule <batch> --expr '…'` to see which pages a rule hits.

## Separator sheets

`ccocr separator-sheet --doctype invoice --classification 4.2.1` prints an A4
page with a QR code `CCOCR:1;SEP;DT=INVOICE;CLS=4.2.1`. Put one in front of each
document; the sheet is removed and its values become the document's metadata.

## Timestamps

In single-file mode the timestamp is a PAdES document timestamp inside the
PDF; it covers the whole file including the embedded XML. In sidecar mode it
is a `.tsr` token over the hash manifest.

| Mode | Use |
| --- | --- |
| `openssl-local` | Built-in **test** TSA (created on first start in `/data/.ccocr/test-tsa`). Non-qualified. |
| `http` | Any RFC 3161 TSA, e.g. the cc-ca TimeStamp service. Set `timestamp.url`; credentials via `CCOCR_TSA_USER` / `CCOCR_TSA_PASSWORD`. |
| `none` | No timestamp. |

Every report marks non-qualified timestamps as such. With `required: true`
(default) a document is never delivered without a verified timestamp: if the
TSA is down, the batch fails, is retried, and finally lands in `error/`.

### CC Test CA (cc-ca TimeStamp service)

Use `profiles/cc-test-ca.yaml`. Once per installation, download the test root
(checked against its pinned SHA-256) and the issuing CA into the data volume,
never into the image:

```bash
docker compose run --rm ccocr fetch-trust --dir /data/.ccocr/trust \
  --root-url http://pki-test.comp-consulting.eu/certs/root-2026.pem \
  --root-sha256 DD:79:4F:F8:E2:FE:E2:A8:16:CB:5B:26:DB:50:0F:47:B2:D7:C6:E6:D3:6C:5D:FB:2F:F8:96:53:FA:DE:9E:39 \
  --chain-url http://pki-test.comp-consulting.eu/certs/issuing-2026.crt
```

The profile uses the OCR unit `/tsa/ocr` (CC TEST OCR TSU 01), which needs basic auth
(`CCOCR_TSA_USER` / `CCOCR_TSA_PASSWORD` as environment secrets). The TSA is
firewalled to known servers. With `embed_validation: true` the TSA chain's OCSP/CRL
answers are stored in the PDF, so it still validates when the CA is offline.

## Develop

```bash
pip install -e '.[test]'      # needs tesseract (+ slv hrv srp srp-latn bos sqi osd), ghostscript
pytest
python tests/synth.py demo.tif   # synthetic 7-page batch (no customer data)
ccocr init-test-tsa ./tsa
ccocr process demo.tif --profile profiles/example.yaml --out out --tsa-dir ./tsa
```

## Known limits of this increment

- The separation screen keeps the batch's page order (no reordering) and
  does not edit OCR text. Field-level validation (spec step 7) beyond document
  type and classification comes later.
- The console UI is English; the Slovenian and Serbian catalogues are prepared
  but not translated yet.
- With `pdf_image: original`, skew is detected and reported but not corrected,
  so the text layer stays aligned with the unchanged scan.
- PDF pages are rasterised at their native resolution and embedded losslessly;
  the original JPEG/CCITT streams are not passed through byte for byte yet.
- PDF/A is produced by OCRmyPDF/Ghostscript; an independent veraPDF check is planned.
