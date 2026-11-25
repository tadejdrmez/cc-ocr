#!/bin/sh
# Creates the hot-folder layout and, on first start, a TEST timestamp authority
# (non-qualified, for test/pilot only). Then runs the ccocr command.
set -e
mkdir -p /data/in /data/out /data/processing /data/archive /data/error /data/review /data/.ccocr
case "$1" in
  watch|process)
    if [ -n "$CCOCR_TEST_TSA_DIR" ] && [ ! -f "$CCOCR_TEST_TSA_DIR/tsa.cnf" ]; then
        ccocr init-test-tsa "$CCOCR_TEST_TSA_DIR"
    fi ;;
esac
exec ccocr "$@"
