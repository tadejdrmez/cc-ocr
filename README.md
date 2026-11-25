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
| Output | PDF/A-2 per document, sidecar XML (`urn:cc:ocr:metadata:1`, XSD in `src/ccocr/schemas`), hash manifest, RFC 3161 timestamp (`.tsr`) over the manifest, job report JSON per batch. |

```
in/  ->  processing/  ->  out/invoice_batch_001.pdf
                              invoice_batch_001.xml           sidecar metadata
                              invoice_batch_001.manifest.xml  SHA-256 of PDF + XML
                              invoice_batch_001.tsr           timestamp over the manifest
                              batch.<job>.job-report.json
          archive/<date>/batch.pdf   (original, unchanged)
          error/                     (failed or duplicate batches + .error.txt)
```

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

| Mode | Use |
| --- | --- |
| `openssl-local` | Built-in **test** TSA (created on first start in `/data/.ccocr/test-tsa`). Non-qualified. |
| `http` | Any RFC 3161 TSA, e.g. the cc-ca TimeStamp service. Set `timestamp.url`; credentials via `CCOCR_TSA_USER` / `CCOCR_TSA_PASSWORD`. |
| `none` | No timestamp. |

Every report marks non-qualified timestamps as such.

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
