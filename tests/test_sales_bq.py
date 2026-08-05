"""Unit tests for the sales-BQ guardrails (no network)."""

import pytest

from gastrobrain.sales_bq import SalesQueryError, _json_safe, _reject_non_select


class TestRejectNonSelect:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "  select store_id from t  ",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "SELECT 1;",  # trailing semicolon ok
            "(SELECT 1)",
        ],
    )
    def test_allows_select(self, sql):
        _reject_non_select(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "   ",
            "DELETE FROM t WHERE 1=1",
            "DROP TABLE t",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a=1",
            "MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN DELETE",
            "CREATE TABLE t (a INT64)",
            "TRUNCATE TABLE t",
            "SELECT 1; DELETE FROM t",  # multi-statement
            "CALL my_proc()",
        ],
    )
    def test_rejects_non_select(self, sql):
        with pytest.raises(SalesQueryError):
            _reject_non_select(sql)


class TestJsonSafe:
    def test_converts_bq_types(self):
        import datetime
        import decimal

        assert _json_safe(decimal.Decimal("12.5")) == 12.5
        assert _json_safe(datetime.date(2026, 7, 1)) == "2026-07-01"
        assert _json_safe([decimal.Decimal("1")]) == [1.0]
        assert _json_safe({"d": datetime.date(2026, 1, 2)}) == {"d": "2026-01-02"}
        assert _json_safe("福栄組合") == "福栄組合"
        assert _json_safe(None) is None
