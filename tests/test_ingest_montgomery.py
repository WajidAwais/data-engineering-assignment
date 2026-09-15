"""Offline tests for the ingestion mechanics.

No network. These cover the parts Part 1a says are checked specifically —
pagination correctness and re-run behaviour — so they need to run without
depending on whether a backfill happened to be done first.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from src.ingest import bronze, montgomery, socrata
from src.ingest.http import Response

FLOOR = "1970-01-01T00:00:00.000Z"
CEILING = "2026-09-04T12:00:00.000Z"


def _record(row_id: str, updated: str = "2026-09-01T00:00:00.000Z", **fields) -> dict:
    return {":id": row_id, ":updated_at": updated, ":created_at": updated, **fields}


def _response(records: list[dict]) -> Response:
    body = json.dumps(records).encode()
    return Response(body=body, status=200, url="http://test", headers={})


class _FakeApi:
    """Serves a fixed row set, honouring $where cursor + $limit like Socrata."""

    def __init__(self, records: list[dict]) -> None:
        self.records = records
        self.calls: list[dict] = []

    def __call__(self, url, *, params=None, session=None, timeout=None) -> Response:
        self.calls.append(params)
        where = params["$where"]
        limit = params["$limit"]

        rows = self.records
        if ":id > '" in where:
            cursor = where.split(":id > '")[1].split("'")[0]
            idx = next(i for i, r in enumerate(rows) if r[":id"] == cursor)
            rows = rows[idx + 1 :]
        return _response(rows[:limit])


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


# --- pagination -------------------------------------------------------------


def test_keyset_walk_yields_every_row_exactly_once(monkeypatch):
    records = [_record(f"row-{i:04d}") for i in range(250)]
    api = _FakeApi(records)
    monkeypatch.setattr(socrata.http, "get", api)

    seen = [r[":id"] for page in socrata.paginate(
        "http://b", "ds", floor=FLOOR, ceiling=CEILING, page_size=100
    ) for r in page.records]

    assert seen == [r[":id"] for r in records], "keyset walk must preserve order"
    assert len(seen) == len(set(seen)) == 250, "no duplicates, no gaps"


def test_pagination_never_uses_offset(monkeypatch):
    """$offset without a deterministic $order is the documented failure mode."""
    api = _FakeApi([_record(f"row-{i:04d}") for i in range(150)])
    monkeypatch.setattr(socrata.http, "get", api)

    list(socrata.paginate("http://b", "ds", floor=FLOOR, ceiling=CEILING, page_size=100))

    assert api.calls, "expected at least one request"
    for params in api.calls:
        assert "$offset" not in params
        assert params["$order"] == ":id"


def test_every_request_carries_the_frozen_ceiling(monkeypatch):
    """Without an upper bound, a row inserted mid-run below the cursor is lost."""
    api = _FakeApi([_record(f"row-{i:04d}") for i in range(150)])
    monkeypatch.setattr(socrata.http, "get", api)

    list(socrata.paginate("http://b", "ds", floor=FLOOR, ceiling=CEILING, page_size=100))

    assert len(api.calls) > 1, "need multiple pages to prove the ceiling is not just page 1"
    for params in api.calls:
        assert f":updated_at <= '{CEILING}'" in params["$where"]
        assert f":updated_at > '{FLOOR}'" in params["$where"]


def test_cursor_advances_from_the_last_row_of_each_page(monkeypatch):
    api = _FakeApi([_record(f"row-{i:04d}") for i in range(250)])
    monkeypatch.setattr(socrata.http, "get", api)

    list(socrata.paginate("http://b", "ds", floor=FLOOR, ceiling=CEILING, page_size=100))

    assert ":id >" not in api.calls[0]["$where"], "first page has no cursor"
    assert ":id > 'row-0099'" in api.calls[1]["$where"]
    assert ":id > 'row-0199'" in api.calls[2]["$where"]


# --- content hashing --------------------------------------------------------


def test_absent_and_null_fields_hash_identically():
    """Socrata omits null fields entirely, so the same logical record arrives
    with different key sets on different days."""
    assert bronze.content_hash({"a": "1", "b": None}) == bronze.content_hash({"a": "1"})
    assert bronze.content_hash({"a": "1", "b": ""}) == bronze.content_hash({"a": "1"})


def test_system_fields_excluded_from_hash():
    """A publisher truncate-and-reload rewrites :updated_at on every row without
    changing any data. That must not read as 220k changed records."""
    a = _record("row-1", updated="2024-01-01T00:00:00.000Z", road_name="MAIN ST")
    b = _record("row-1", updated="2026-06-12T00:00:00.000Z", road_name="MAIN ST")
    assert bronze.content_hash(a) == bronze.content_hash(b)


def test_business_change_does_change_hash():
    a = _record("row-1", road_name="MAIN ST")
    b = _record("row-1", road_name="OAK AVE")
    assert bronze.content_hash(a) != bronze.content_hash(b)


# --- bronze writes ----------------------------------------------------------


def test_replaying_identical_records_inserts_nothing(con):
    writer = bronze.BronzeWriter(con, "bronze_test", ["road_name"])
    records = [_record(f"row-{i}", road_name="MAIN ST") for i in range(10)]

    first = writer.append(records, run_id="run-1")
    second = writer.append(records, run_id="run-2")

    assert first.inserted == 10
    assert second.inserted == 0 and second.received == 10
    assert con.execute('SELECT count(*) FROM "bronze_test"').fetchone()[0] == 10


def test_changed_record_appends_a_new_version(con):
    writer = bronze.BronzeWriter(con, "bronze_test", ["road_name"])
    writer.append([_record("row-1", road_name="MAIN ST")], run_id="run-1")
    writer.append([_record("row-1", road_name="OAK AVE")], run_id="run-2")

    rows = con.execute(
        'SELECT "_content_hash" FROM "bronze_test" WHERE "_source_record_id" = \'row-1\''
    ).fetchall()
    assert len(rows) == 2, "bronze is versioned, not overwritten"


def test_duplicate_within_one_batch_inserts_once(con):
    writer = bronze.BronzeWriter(con, "bronze_test", ["road_name"])
    rec = _record("row-1", road_name="MAIN ST")
    assert writer.append([rec, rec], run_id="run-1").inserted == 1


def test_new_column_is_added_rather_than_dropped(con):
    writer = bronze.BronzeWriter(con, "bronze_test", ["road_name"])
    writer.append([_record("row-1", road_name="MAIN ST")], run_id="run-1")
    writer.append([_record("row-2", road_name="OAK AVE", new_field="x")], run_id="run-2")

    val = con.execute(
        'SELECT "new_field" FROM "bronze_test" WHERE "_source_record_id" = \'row-2\''
    ).fetchone()[0]
    assert val == "x", "schema drift must widen the table, not silently drop data"


def test_nested_values_are_preserved_as_json(con):
    writer = bronze.BronzeWriter(con, "bronze_test", ["geolocation"])
    geo = {"latitude": "39.1", "longitude": "-77.1"}
    writer.append([_record("row-1", geolocation=geo)], run_id="run-1")

    stored = con.execute('SELECT "geolocation" FROM "bronze_test"').fetchone()[0]
    assert json.loads(stored) == geo


# --- watermark / reload guard ----------------------------------------------


def test_reload_guard_trips_when_delta_dominates(monkeypatch, con):
    """The 2024-06-12 min :updated_at across all three datasets is a reload
    artefact. When that recurs, the delta path must refuse rather than stream
    the whole table through it."""
    monkeypatch.setattr(socrata, "fetch_schema", lambda *a, **k: (["road_name"], ["text"]))
    monkeypatch.setattr(socrata, "count_rows", lambda *a, **k: 100_000)
    monkeypatch.setattr(montgomery.watermark, "read_watermark", lambda *a, **k: "2026-01-01T00:00:00.000Z")

    settings = type("S", (), {
        "sources": {"montgomery": {"base": "http://b", "incidents": "ds", "app_token": ""}},
        "socrata_app_token": "",
    })()

    with pytest.raises(montgomery.ReloadSuspected, match="publisher-side reload"):
        montgomery.ingest_dataset(
            settings, con, "incidents", since=None, page_size=100,
            full_refresh=False, allow_full_reload=False, dry_run=False,
        )


def test_cold_start_is_not_mistaken_for_a_reload(monkeypatch, con):
    """A first run legitimately pulls 100% of the table."""
    monkeypatch.setattr(socrata, "fetch_schema", lambda *a, **k: (["road_name"], ["text"]))
    monkeypatch.setattr(socrata, "count_rows", lambda *a, **k: 100_000)
    monkeypatch.setattr(montgomery.watermark, "read_watermark", lambda *a, **k: None)

    settings = type("S", (), {
        "sources": {"montgomery": {"base": "http://b", "incidents": "ds", "app_token": ""}},
        "socrata_app_token": "",
    })()

    result = montgomery.ingest_dataset(
        settings, con, "incidents", since=None, page_size=100,
        full_refresh=False, allow_full_reload=False, dry_run=True,
    )
    assert result.status == "DRY_RUN"


def test_watermark_is_not_advanced_when_a_run_fails(con, monkeypatch):
    from src.ingest import watermark as wm

    con.execute(wm._SCHEMA)

    assert wm.read_watermark(con, "montgomery", "incidents") is None
    wm.start_run(con, run_id="r1", source="montgomery", dataset="incidents",
                 watermark_from=FLOOR, watermark_to=CEILING)
    wm.finish_run(con, run_id="r1", status="FAILED", counters=wm.RunCounters(), error="boom")

    assert wm.read_watermark(con, "montgomery", "incidents") is None, (
        "a failed run must leave the watermark where it was, so the next run redoes it"
    )
