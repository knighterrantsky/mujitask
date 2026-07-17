#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

intent=""
source_record_id=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intent)
      intent="${2:-}"
      shift 2
      ;;
    --source-record-id)
      source_record_id="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$intent" in
  amazon_product_row_refresh)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" amazon-product-row-submit \
      --source-record-id "$source_record_id"
    ;;
  amazon_product_table_refresh)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" amazon-product-table-submit
    ;;
  *)
    echo "Unknown or missing intent: $intent" >&2
    exit 2
    ;;
esac
