# CC OCR

On-premises importer for scanned documents. Drop a scanned batch (PDF or
multi-page TIFF) into a folder; CC OCR splits it into documents, makes them
searchable and writes timestamped PDF/A files with metadata.

This is **increment 0**, the pipeline proof of concept from the specification
(CC_OCR_Specification v0.4, chapter 10.3). It runs unattended. The operator
screens (manual separation, validation) come in the next increments, so split
proposals and documents are **auto-accepted** for now, and every output says so.

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
docker load -i cc-ocr-0.1.0.tar          # or: docker compose build
docker run -d --name cc-ocr --restart unless-stopped -v C:\Scan:/data cc-ocr:0.1.0
```

Scanners save into `C:\Scan\in`; results appear in `C:\Scan\out`.

### Linux server (e.g. next to CC_archive)

```bash
docker load -i cc-ocr-0.1.0.tar
CCOCR_DATA=/srv/scan docker compose up -d
```

### Offline transfer

```bash
docker build -t cc-ocr:0.1.0 .
docker save cc-ocr:0.1.0 | gzip > cc-ocr-0.1.0.tar.gz   # copy this file to the target machine
```

### One-off commands

```bash
docker run --rm -v C:\Scan:/data cc-ocr:0.1.0 process /data/batch.pdf --out /data/out
docker run --rm -v C:\Scan:/data cc-ocr:0.1.0 separator-sheet --doctype invoice --doctype contract --out /data/separators.pdf
docker run --rm -v C:\Scan:/data cc-ocr:0.1.0 test-rule /data/batch.pdf --expr 'top contains "račun"'
docker run --rm -v C:\Scan:/data cc-ocr:0.1.0 verify /data/out --tsa-dir /data/.ccocr/test-tsa
```

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

The TSA is firewalled to known servers and may need basic auth
(`CCOCR_TSA_USER` / `CCOCR_TSA_PASSWORD` as environment secrets). On the PC that
runs the CA itself, the container reaches it through `extra_hosts` (see
`docker-compose.yml`). With `embed_validation: true` the TSA chain's OCSP/CRL
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

- No operator screens yet: proposals and documents are auto-accepted.
- With `pdf_image: original`, skew is detected and reported but not corrected,
  so the text layer stays aligned with the unchanged scan.
- PDF pages are rasterised at their native resolution and embedded losslessly;
  the original JPEG/CCITT streams are not passed through byte for byte yet.
- PDF/A is produced by OCRmyPDF/Ghostscript; an independent veraPDF check is planned.
