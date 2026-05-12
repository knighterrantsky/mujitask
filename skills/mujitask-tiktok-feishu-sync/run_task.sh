#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

intent=""
product_url=""
search_keyword=""
sales_7d_threshold=""
total_sales_threshold=""
price_range_max_threshold=""
max_candidates=""
target_intent=""
items_json=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --intent)
      intent="${2:-}"
      shift 2
      ;;
    --product-url)
      product_url="${2:-}"
      shift 2
      ;;
    --search-keyword)
      search_keyword="${2:-}"
      shift 2
      ;;
    --sales-7d-threshold)
      sales_7d_threshold="${2:-}"
      shift 2
      ;;
    --total-sales-threshold)
      total_sales_threshold="${2:-}"
      shift 2
      ;;
    --price-range-max-threshold)
      price_range_max_threshold="${2:-}"
      shift 2
      ;;
    --max-candidates)
      max_candidates="${2:-}"
      shift 2
      ;;
    --target-intent)
      target_intent="${2:-}"
      shift 2
      ;;
    --items-json)
      items_json="${2:-}"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

case "$intent" in
  competitor_table_refresh)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" refresh-current-competitor-table-submit
    ;;
  keyword_competitor_search)
    args=(keyword-search-submit --search-keyword "$search_keyword")
    if [[ -n "$total_sales_threshold" ]]; then
      args+=(--total-sales-threshold "$total_sales_threshold")
      if [[ -n "$sales_7d_threshold" ]]; then
        args+=(--sales-7d-threshold "$sales_7d_threshold")
      fi
    else
      args+=(--sales-7d-threshold "${sales_7d_threshold:-200}")
    fi
    args+=(--max-candidates "${max_candidates:-20}")
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" "${args[@]}"
    ;;
  influencer_pool_sync)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" influencer-pool-sync-submit
    ;;
  selection_table_ingest)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" selection-table-complete-submit
    ;;
  keyword_selection_search)
    args=(selection-keyword-search-submit --search-keyword "$search_keyword")
    args+=(--sales-7d-threshold "${sales_7d_threshold:-500}")
    args+=(--price-range-max-threshold "${price_range_max_threshold:-10.99}")
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" "${args[@]}"
    ;;
  batch_keyword_search_submit)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" batch-keyword-search-submit --target-intent "$target_intent" --items-json "$items_json"
    ;;
  product_url_complete)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" product-url-complete-submit --product-url "$product_url"
    ;;
  competitor_row_by_url)
    exec python3 -u "$SCRIPT_DIR/run_skill_step.py" competitor-row-by-url-submit --product-url "$product_url"
    ;;
  *)
    echo "Unknown or missing intent: $intent" >&2
    exit 2
    ;;
esac
