# syntax=docker/dockerfile:1
# CC OCR - scanned-document importer and admin web console
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OMP_THREAD_LIMIT=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-osd \
        tesseract-ocr-slv tesseract-ocr-eng tesseract-ocr-hrv tesseract-ocr-srp \
        tesseract-ocr-srp-latn tesseract-ocr-bos tesseract-ocr-sqi \
        python3 python3-venv ghostscript qpdf pngquant openssl fonts-dejavu-core libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# Optional: behind a TLS-intercepting proxy, pass its CA bundle as a build secret:
#   docker build --secret id=extra_ca,src=/path/ca-bundle.crt .
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    python3 -m venv /opt/venv && pip install --no-cache-dir . && rm -rf /root/.cache

COPY profiles /app/profiles
COPY config /app/config
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && (userdel -r ubuntu 2>/dev/null || true) \
    && useradd --create-home --uid 1000 ccocr \
    && mkdir -p /data && chown ccocr:ccocr /data

USER ccocr
VOLUME ["/data"]
EXPOSE 8080
ENV CCOCR_TEST_TSA_DIR=/data/.ccocr/test-tsa
ENTRYPOINT ["tini", "--", "entrypoint.sh"]
CMD ["watch", "--config", "/app/config/watch.yaml"]
