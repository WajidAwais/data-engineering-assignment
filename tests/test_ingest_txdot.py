"""Offline tests for ArcGIS extraction.

No network. Covers the service-specific traps that reconnaissance found, so a
future refactor cannot quietly reintroduce them.
"""

from __future__ import annotations

import json

import pytest

from src.ingest import arcgis
from src.ingest.http import Response


def _response(payload: dict) -> Response:
    return Response(body=json.dumps(payload).encode(), status=200, url="http://test", headers={})


def _feature(oid: int, crash_id: int) -> dict:
    return {
        "attributes": {"ESRI_OID": oid, "crash_id": crash_id, "crash_date": "2024-01-15"},
        "geometry": {"x": -97.7, "y": 30.4},
    }


class _FakeService:
    def __init__(self, oids: list[int]) -> None:
        self.oids = oids
        self.calls: list[dict] = []

    def __call__(self, url, *, params=None, session=None, timeout=None) -> Response:
        self.calls.append(params)
        if params.get("returnIdsOnly") == "true":
            return _response({"objectIdFieldName": "ESRI_OID", "objectIds": self.oids})
        if params.get("returnCountOnly") == "true":
            return _response({"count": len(self.oids)})

        where = params["where"]
        lo = int(where.split("ESRI_OID >= ")[1].split(" ")[0])
        hi = int(where.split("ESRI_OID <= ")[1].rstrip(")").split(" ")[0])
        matched = [o for o in sorted(self.oids) if lo <= o <= hi]
        return _response({"features": [_feature(o, 17_000_000 + o) for o in matched]})


# --- error surface ----------------------------------------------------------


def test_arcgis_errors_arrive_as_http_200(monkeypatch):
    """The service reports failures in the body, not the status line. Missing
    this means a failed run looks like an empty one."""
    err = _response({"error": {"code": 400, "message": "Cannot perform query.",
                               "details": ["'Invalid field: OBJECTID' parameter is invalid"]}})
    monkeypatch.setattr(arcgis.http, "get", lambda *a, **k: err)

    with pytest.raises(arcgis.ArcGISError, match="Invalid field: OBJECTID"):
        arcgis.layer_metadata("http://layer")


# --- id planning ------------------------------------------------------------


def test_object_ids_are_sorted(monkeypatch):
    """The service returns them unsorted; batch bounds are derived from order,
    so sorting is correctness, not tidiness."""
    unsorted = [500, 1, 900, 42, 7]
    monkeypatch.setattr(arcgis.http, "get", _FakeService(unsorted))

    assert arcgis.fetch_object_ids("http://layer", "1=1") == [1, 7, 42, 500, 900]


def test_batches_cover_every_id_exactly_once(monkeypatch):
    oids = list(range(1, 5001))
    svc = _FakeService(oids)
    monkeypatch.setattr(arcgis.http, "get", svc)

    seen = [
        r["ESRI_OID"]
        for b in arcgis.iter_batches("http://l", where="1=1", object_ids=oids, batch_size=2000)
        for r in b.records
    ]
    assert sorted(seen) == oids
    assert len(seen) == len(set(seen)) == 5000


def test_batch_uses_oid_range_not_an_id_list(monkeypatch):
    """A 2000-id comma list is ~12.9 kB of URL. Ranges keep it short."""
    oids = list(range(1, 4001))
    svc = _FakeService(oids)
    monkeypatch.setattr(arcgis.http, "get", svc)

    list(arcgis.iter_batches("http://l", where="1=1", object_ids=oids, batch_size=2000))

    for params in svc.calls:
        assert "objectIds" not in params
        assert "resultOffset" not in params, "offset paging shifts under concurrent edits"
        assert "ESRI_OID >=" in params["where"] and "ESRI_OID <=" in params["where"]


def test_every_batch_requests_wgs84(monkeypatch):
    """Geometry defaults to EPSG:3081. Without outSR the coordinates are metres."""
    oids = list(range(1, 3001))
    svc = _FakeService(oids)
    monkeypatch.setattr(arcgis.http, "get", svc)

    list(arcgis.iter_batches("http://l", where="1=1", object_ids=oids, batch_size=2000))

    assert svc.calls, "expected requests"
    for params in svc.calls:
        assert params["outSR"] == 4326


def test_scope_predicate_is_preserved_in_every_batch(monkeypatch):
    oids = list(range(1, 3001))
    svc = _FakeService(oids)
    monkeypatch.setattr(arcgis.http, "get", svc)

    list(arcgis.iter_batches(
        "http://l", where="bicyclist_involved_fl=1", object_ids=oids, batch_size=2000
    ))

    for params in svc.calls:
        assert "bicyclist_involved_fl=1" in params["where"]


def test_max_batches_bounds_the_run(monkeypatch):
    oids = list(range(1, 10001))
    monkeypatch.setattr(arcgis.http, "get", _FakeService(oids))

    batches = list(arcgis.iter_batches(
        "http://l", where="1=1", object_ids=oids, batch_size=2000, max_batches=2
    ))
    assert len(batches) == 2


def test_transfer_limit_is_treated_as_a_stale_snapshot(monkeypatch):
    """Bounds come from the pinned ID set, so exceeding the limit means the layer
    changed mid-run and the batch is incomplete."""
    monkeypatch.setattr(
        arcgis.http, "get",
        lambda *a, **k: _response({"features": [_feature(1, 1)], "exceededTransferLimit": True}),
    )
    with pytest.raises(arcgis.ArcGISError, match="stale"):
        list(arcgis.iter_batches("http://l", where="1=1", object_ids=[1, 2], batch_size=2000))


# --- record shape -----------------------------------------------------------


def test_geometry_is_flattened_alongside_attributes(monkeypatch):
    monkeypatch.setattr(arcgis.http, "get", _FakeService([1]))

    batch = next(arcgis.iter_batches("http://l", where="1=1", object_ids=[1], batch_size=2000))
    record = batch.records[0]

    assert record["crash_id"] == 17_000_001
    assert record["geometry"] == {"x": -97.7, "y": 30.4}


def test_last_edit_date_is_read_as_a_string():
    """Epoch millis; compared for equality only, never arithmetic."""
    assert arcgis.last_edit_date({"editingInfo": {"lastEditDate": 1766433775689}}) == "1766433775689"
    assert arcgis.last_edit_date({}) is None
