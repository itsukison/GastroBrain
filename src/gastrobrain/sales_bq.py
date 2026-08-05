"""BigQuery sales-data access for the MCP surface.

Text-to-SQL happens on the *caller's* side (same Plan-A philosophy as
search_knowledge: we provide context + execution, the client LLM writes the
SQL). This module provides the two halves:

  - sales_schema()  — table/column docs + live value catalogs + example
                      queries, so the calling LLM can write correct SQL.
  - run_sales_sql() — guarded read-only execution: dry-run first, SELECT
                      statements only, referenced tables must stay inside the
                      sales dataset, bytes-billed cap, row cap.

Queries are billed to the Gastrobrain GCP project; the data lives in the
ec-data-retrive project (schemas defined in that repo's tf/bigquery.tf —
keep SALES_TABLES below in sync with it).
"""

from __future__ import annotations

import datetime as _dt
import decimal
import logging
import threading
import time
from functools import lru_cache
from typing import Any

from gastrobrain.config import get_settings

log = logging.getLogger("gastrobrain.sales_bq")

# --------------------------------------------------------------------------------------
# Static schema documentation — mirrors ec-data-retrive/tf/bigquery.tf.
# --------------------------------------------------------------------------------------

SALES_TABLES: dict[str, dict[str, Any]] = {
    "sales_data": {
        "description": "店舗×モール×日次の売上サマリ。売上・注文数・アクセス数・転換率・客単価。",
        "date_column": "date",
        "columns": {
            "date": "DATE — 日付(必須)",
            "store_id": "STRING — 店舗名 (例: 福栄組合, 松屋)",
            "ec_platform": "STRING — モール (rakuten / amazon / yahoo など)",
            "sales_amount": "NUMERIC — 売上金額(円)",
            "order_count": "INTEGER — 注文件数",
            "access_count": "INTEGER — アクセス数(PV/訪問)",
            "conversion_rate": "FLOAT — 転換率(CVR)",
            "unit_price": "FLOAT — 客単価",
        },
    },
    "ad_performance": {
        "description": "広告パフォーマンス日次レポート。広告費・クリック・ROAS・新規/既存顧客売上。",
        "date_column": "date",
        "columns": {
            "date": "DATE — 日付(必須)",
            "store_id": "STRING — 店舗名",
            "ec_platform": "STRING — モール",
            "frame_type": "STRING — 枠種別",
            "ad_product_name": "STRING — 広告商品名",
            "frame_name": "STRING — 枠名",
            "device": "STRING — デバイス",
            "media": "STRING — メディア",
            "placement_start_date": "DATE — 掲載開始日",
            "placement_end_date": "DATE — 掲載終了日",
            "ad_cost": "NUMERIC — 広告費(円)",
            "ad_cost_daily_allocation": "NUMERIC — 広告費 日割り按分(円)",
            "click_count": "INTEGER — クリック数",
            "cpc": "NUMERIC — CPC(円)",
            "sales_amount_cross_device": "NUMERIC — 売上金額 クロスデバイス含む(円)",
            "sales_count_cross_device": "INTEGER — 売上件数 クロスデバイス含む",
            "cvr_cross_device": "NUMERIC — CVR(%)",
            "roas_cross_device": "NUMERIC — ROAS(%)",
            "cpa_cross_device": "NUMERIC — 注文獲得単価(円)",
            "assist_count_cross_device": "INTEGER — アシスト数",
            "new_customer_acquisition_count": "INTEGER — 新規顧客獲得数",
            "existing_customer_sales_count": "INTEGER — 既存顧客売上件数",
            "new_customer_sales_amount": "NUMERIC — 新規顧客売上金額(円)",
            "existing_customer_sales_amount": "NUMERIC — 既存顧客売上金額(円)",
            "new_customer_acquisition_cost": "NUMERIC — 新規顧客獲得単価(円)",
            "new_customer_growth_amount": "NUMERIC — 新規顧客成長額(円)",
        },
    },
    "ad_purchase_history": {
        "description": "広告購入履歴(月単位)。申込・掲載期間・購入額・同意状況。",
        "date_column": "placement_start_date",
        "columns": {
            "target_month": "STRING — 対象月",
            "store_id": "STRING — 店舗名",
            "ec_platform": "STRING — モール",
            "ad_type": "STRING — 広告種別",
            "application_id": "STRING — お申し込みID",
            "ad_product_name": "STRING — 広告商品名",
            "frame_name": "STRING — 枠名",
            "status": "STRING — ステータス",
            "placement_start_date": "DATE — 掲載開始日",
            "placement_end_date": "DATE — 掲載終了日",
            "purchase_amount_provisional": "NUMERIC — 購入額/暫定額",
            "quantity": "INTEGER — 数量",
            "purchase_proposal_date": "DATE — 購入日/提案日",
            "consent_date": "DATE — 同意日",
            "consent_status": "STRING — 同意状態",
            "product_management_number": "STRING — 商品管理番号",
            "sale_content": "STRING — セール内容",
        },
    },
    "market_share": {
        "description": "市場シェア分析。自店とサブジャンルTOP10平均の売上・アクセス・転換率・客単価比較。",
        "date_column": "date",
        "columns": {
            "date": "DATE — 日付(必須)",
            "store_id": "STRING — 店舗名",
            "ec_platform": "STRING — モール",
            "sales_amount_all": "NUMERIC — 売上金額(すべて)",
            "sales_count_all": "INTEGER — 売上件数(すべて)",
            "subgenre_top10_avg_sales_count_all": "INTEGER — サブジャンルTOP10平均 売上件数",
            "subgenre_top10_avg_sales_amount_all": "NUMERIC — サブジャンルTOP10平均 売上金額",
            "access_users_all": "INTEGER — アクセス人数",
            "subgenre_top10_avg_access_users_all": "INTEGER — サブジャンルTOP10平均 アクセス人数",
            "conversion_rate_all": "FLOAT — 転換率",
            "subgenre_top10_avg_conversion_rate_all": "FLOAT — サブジャンルTOP10平均 転換率",
            "unit_price_all": "NUMERIC — 客単価",
            "subgenre_top10_avg_unit_price_all": "NUMERIC — サブジャンルTOP10平均 客単価",
        },
    },
    "product_page_analysis": {
        "description": "商品ページ分析(商品×日次)。売上・アクセス・転換率・レビュー・在庫など。",
        "date_column": "analysis_date",
        "columns": {
            "product_management_number": "STRING — 商品管理番号(必須)",
            "store_id": "STRING — 店舗名",
            "ec_platform": "STRING — モール",
            "analysis_date": "DATE — 分析対象日(必須)",
            "product_name": "STRING — 商品名",
            "sales_amount": "NUMERIC — 売上",
            "sales_count": "INTEGER — 売上件数",
            "units_pc": "INTEGER — 売上個数",
            "access_users": "INTEGER — アクセス人数",
            "conversion_rate": "FLOAT — 転換率",
            "unit_price": "NUMERIC — 客単価",
            "access_value": "NUMERIC — アクセス価値",
            "new_customer_count": "INTEGER — 新規顧客",
            "existing_customer_count": "INTEGER — 既存顧客",
            "review_point": "NUMERIC — レビュー評価",
            "review_count": "INTEGER — レビュー投稿数",
            "review_total": "INTEGER — レビュー件数",
            "duration_seconds": "INTEGER — 滞在時間(秒)",
            "exit_rate": "FLOAT — 離脱率",
            "bookmark_add": "INTEGER — お気に入り登録ユーザ数",
            "bookmark_total": "INTEGER — お気に入り総ユーザ数",
            "inventory": "INTEGER — 在庫数",
        },
    },
    "access_analysis_referrer": {
        "description": "アクセス解析(参照元)。日次×参照元のアクセス人数と割合。",
        "date_column": "analysis_date",
        "columns": {
            "analysis_date": "DATE — 分析対象日(必須)",
            "store_id": "STRING — 店舗名",
            "ec_platform": "STRING — モール",
            "referrer_source": "STRING — 参照元(必須)",
            "access_users": "INTEGER — アクセス人数",
            "percentage": "FLOAT — 割合",
        },
    },
    "access_analysis_keywords": {
        "description": "アクセス解析(検索キーワード)。日次×検索キーワードのアクセス人数と割合。",
        "date_column": "analysis_date",
        "columns": {
            "analysis_date": "DATE — 分析対象日(必須)",
            "store_id": "STRING — 店舗名",
            "ec_platform": "STRING — モール",
            "search_keyword": "STRING — 検索キーワード(必須)",
            "access_users": "INTEGER — アクセス人数",
            "percentage": "FLOAT — 割合",
        },
    },
}

# Verified example queries — few-shot context for the calling LLM.
EXAMPLE_QUERIES: list[dict[str, str]] = [
    {
        "question": "福栄組合の楽天の直近30日の売上合計と注文件数は？",
        "sql": (
            "SELECT SUM(sales_amount) AS total_sales, SUM(order_count) AS total_orders\n"
            "FROM `{data_project}.{dataset}.sales_data`\n"
            "WHERE store_id = '福栄組合' AND ec_platform = 'rakuten'\n"
            "  AND date >= DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 30 DAY)"
        ),
    },
    {
        "question": "店舗ごとの今月のモール別売上（前年同期間比つき — 月初〜今日 vs 前年の同じ日数で比較）",
        "sql": (
            "WITH cur AS (\n"
            "  SELECT store_id, ec_platform, SUM(sales_amount) AS sales\n"
            "  FROM `{data_project}.{dataset}.sales_data`\n"
            "  WHERE date >= DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), MONTH)\n"
            "  GROUP BY 1, 2),\n"
            "prev AS (\n"
            "  SELECT store_id, ec_platform, SUM(sales_amount) AS sales\n"
            "  FROM `{data_project}.{dataset}.sales_data`\n"
            "  WHERE date BETWEEN DATE_SUB(DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), MONTH), INTERVAL 1 YEAR)\n"
            "                 AND DATE_SUB(CURRENT_DATE('Asia/Tokyo'), INTERVAL 1 YEAR)\n"
            "  GROUP BY 1, 2)\n"
            "SELECT cur.store_id, cur.ec_platform, cur.sales,\n"
            "       prev.sales AS sales_last_year,\n"
            "       SAFE_DIVIDE(cur.sales, prev.sales) AS yoy_ratio\n"
            "FROM cur LEFT JOIN prev USING (store_id, ec_platform)\n"
            "ORDER BY cur.sales DESC"
        ),
    },
    {
        "question": "先月ROASが低かった広告商品トップ10（楽天）",
        "sql": (
            "SELECT ad_product_name,\n"
            "       SUM(ad_cost_daily_allocation) AS ad_cost,\n"
            "       SUM(sales_amount_cross_device) AS ad_sales,\n"
            "       SAFE_DIVIDE(SUM(sales_amount_cross_device), SUM(ad_cost_daily_allocation)) * 100 AS roas_pct\n"
            "FROM `{data_project}.{dataset}.ad_performance`\n"
            "WHERE ec_platform = 'rakuten'\n"
            "  AND DATE_TRUNC(date, MONTH)\n"
            "      = DATE_SUB(DATE_TRUNC(CURRENT_DATE('Asia/Tokyo'), MONTH), INTERVAL 1 MONTH)\n"
            "GROUP BY 1\n"
            "HAVING SUM(ad_cost_daily_allocation) > 0\n"
            "ORDER BY roas_pct ASC\n"
            "LIMIT 10"
        ),
    },
]

USAGE_NOTES = (
    "SQLはBigQuery標準SQL (GoogleSQL)。テーブルは必ず `{data_project}.{dataset}.<table>` の完全修飾で参照。"
    "全テーブルは月パーティション — 必ず日付列でWHERE絞り込みを入れること(コスト上限で拒否されます)。"
    "store_id は日本語の店舗名文字列。ユーザーの略称は catalogs.store_ids の正確な値に対応させること。"
    "日付は Asia/Tokyo 基準。読み取り専用 (SELECT/WITHのみ)。結果は最大 {max_rows} 行。"
)


# --------------------------------------------------------------------------------------
# BigQuery client + live value catalogs (cached)
# --------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _client():
    from google.cloud import bigquery  # deferred: heavy import

    return bigquery.Client(project=get_settings().sales_bq_billing_project)


_CATALOG_TTL_S = 6 * 3600
_catalog_cache: dict[str, Any] = {}
_catalog_lock = threading.Lock()


def _fetch_catalogs() -> dict[str, Any]:
    """Distinct store/platform values + data date range, from the core table.
    Small clustered table — the scan is a few MB."""
    s = get_settings()
    table = f"`{s.sales_bq_data_project}.{s.sales_bq_dataset}.sales_data`"
    sql = (
        "SELECT ARRAY_AGG(DISTINCT store_id ORDER BY store_id) AS store_ids,"
        " ARRAY_AGG(DISTINCT ec_platform ORDER BY ec_platform) AS ec_platforms,"
        " CAST(MIN(date) AS STRING) AS min_date, CAST(MAX(date) AS STRING) AS max_date"
        f" FROM {table}"
    )
    row = next(iter(_client().query(sql).result(timeout=30)))
    return {
        "store_ids": list(row["store_ids"]),
        "ec_platforms": list(row["ec_platforms"]),
        "sales_data_date_range": [row["min_date"], row["max_date"]],
    }


def _catalogs() -> dict[str, Any]:
    with _catalog_lock:
        if _catalog_cache.get("at", 0) > time.time() - _CATALOG_TTL_S:
            return _catalog_cache["value"]
    try:
        value = _fetch_catalogs()
    except Exception as exc:  # BQ unreachable / not yet granted — degrade, don't fail
        log.warning("sales catalogs unavailable: %s", exc)
        value = {"error": f"live value catalogs unavailable: {exc}"}
    with _catalog_lock:
        # Cache errors briefly (5 min) so a permission outage doesn't hammer BQ.
        ttl_offset = 0 if "error" not in value else _CATALOG_TTL_S - 300
        _catalog_cache.update({"at": time.time() - ttl_offset, "value": value})
    return value


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def sales_schema() -> dict[str, Any]:
    """Everything the calling LLM needs to write correct SQL."""
    s = get_settings()
    fmt = {"data_project": s.sales_bq_data_project, "dataset": s.sales_bq_dataset}
    return {
        "dataset": f"{s.sales_bq_data_project}.{s.sales_bq_dataset}",
        "usage_notes": USAGE_NOTES.format(max_rows=s.sales_bq_max_rows, **fmt),
        "tables": SALES_TABLES,
        "catalogs": _catalogs(),
        "example_queries": [
            {"question": ex["question"], "sql": ex["sql"].format(**fmt)}
            for ex in EXAMPLE_QUERIES
        ],
    }


class SalesQueryError(ValueError):
    """Rejected or failed query — message is safe to show the calling LLM."""


def _reject_non_select(sql: str) -> None:
    """Cheap pre-filter before the dry run. The dry run's statement_type is the
    real gate; this just fails fast with clearer messages."""
    body = sql.strip().rstrip(";")
    if not body:
        raise SalesQueryError("empty SQL")
    if ";" in body:
        raise SalesQueryError("multiple statements are not allowed — send one SELECT")
    head = body.lstrip("(").split(None, 1)[0].upper() if body.split() else ""
    if head not in ("SELECT", "WITH"):
        raise SalesQueryError(
            f"only read-only SELECT/WITH queries are allowed (got '{head}')"
        )


def _check_references(job, s) -> None:
    """Every referenced table must live in the sales dataset."""
    allowed = (s.sales_bq_data_project, s.sales_bq_dataset)
    for ref in job.referenced_tables or []:
        if (ref.project, ref.dataset_id) != allowed:
            raise SalesQueryError(
                f"table `{ref.project}.{ref.dataset_id}.{ref.table_id}` is outside the "
                f"sales dataset `{allowed[0]}.{allowed[1]}` — only sales tables may be queried"
            )


def _json_safe(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, list):
        return [_json_safe(x) for x in v]
    if isinstance(v, dict):
        return {k: _json_safe(x) for k, x in v.items()}
    return v


def run_sales_sql(sql: str) -> dict[str, Any]:
    """Validate via dry run, then execute with cost and row caps.

    Raises SalesQueryError with an LLM-actionable message on any rejection or
    BigQuery error (including SQL syntax errors, so the caller can retry)."""
    from google.api_core.exceptions import GoogleAPICallError
    from google.cloud import bigquery

    s = get_settings()
    _reject_non_select(sql)

    try:
        dry = _client().query(
            sql, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        )
    except GoogleAPICallError as exc:
        raise SalesQueryError(f"BigQuery rejected the query: {exc.message}") from exc

    if dry.statement_type != "SELECT":
        raise SalesQueryError(
            f"only SELECT statements are allowed (got {dry.statement_type})"
        )
    _check_references(dry, s)
    if (dry.total_bytes_processed or 0) > s.sales_bq_max_bytes:
        raise SalesQueryError(
            f"query would scan {dry.total_bytes_processed:,} bytes "
            f"(limit {s.sales_bq_max_bytes:,}) — add date-range filters to hit the "
            "month partitions, or aggregate over fewer columns"
        )

    try:
        job = _client().query(
            sql,
            job_config=bigquery.QueryJobConfig(
                maximum_bytes_billed=s.sales_bq_max_bytes,
                labels={"app": "gastrobrain", "surface": "mcp-sales"},
            ),
        )
        it = job.result(timeout=s.sales_bq_timeout_s, max_results=s.sales_bq_max_rows + 1)
        columns = [f.name for f in it.schema]
        rows = [{c: _json_safe(r[c]) for c in columns} for r in it]
    except GoogleAPICallError as exc:
        raise SalesQueryError(f"BigQuery error: {exc.message}") from exc

    truncated = len(rows) > s.sales_bq_max_rows
    if truncated:
        rows = rows[: s.sales_bq_max_rows]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "bytes_processed": job.total_bytes_processed,
    }
