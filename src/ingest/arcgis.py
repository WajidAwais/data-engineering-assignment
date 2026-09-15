"""ArcGIS FeatureServer extraction (TxDOT CRIS).

Extraction is ID-planned, not offset-paged:

  1. One `returnIdsOnly=true` call pins the exact ID set matching the scope.
  2. Sort it client-side — the service returns it unsorted.
  3. Walk it in batches, fetching each by an `ESRI_OID BETWEEN lo AND hi` range
     whose bounds come from that pinned list.

Why ranges rather than an `objectIds=` list: a 2000-ID comma list is ~12.9 kB of
URL, past what many servers accept. Ranges keep the request short while staying
deterministic, because the bounds are derived from the snapshot rather than from
a position in a moving table. `resultOffset` was rejected for the usual reason —
it shifts under concurrent edits.

Three service-specific traps, all verified 2026-09-04:

  * The ObjectID field is `ESRI_OID`. `OBJECTID` returns HTTP 400.
  * Geometry defaults to EPSG:3081 (a projected Texas Lambert). Every query must
    pass `outSR=4326` or coordinates come back in metres.
  * Dates are strings in ISO `YYYY-MM-DD`, so range predicates work — but a
    US-format literal does not error, it silently matches all 3,088,450 rows
    (`'2020-11-04' > '01/01/2024'` as a string). ISO literals only.

`ESRI_OID` is service-generated and reassigned when the layer is republished, so
it is a within-run cursor only. `crash_id` is the business key.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Iterator

import requests

from . import http

log = logging.getLogger(__name__)

OID_FIELD = "ESRI_OID"
BUSINESS_KEY = "crash_id"
WGS84 = 4326

# The service enforces maxRecordCount=2000 on ordinary queries and ignores its
# own advertised standardMaxRecordCount of 16000.
DEFAULT_BATCH_SIZE = 2000


class ArcGISError(RuntimeError):
    """ArcGIS reports failures as HTTP 200 with an {"error": ...} body."""


@dataclass(frozen=True)
class Batch:
    records: list[dict]
    response: http.Response
    params: dict
    lo: int
    hi: int


def _parse(resp: http.Response) -> dict:
    payload = json.loads(resp.body)
    if isinstance(payload, dict) and "error" in payload:
        err = payload["error"]
        raise ArcGISError(
            f"{err.get('code')}: {err.get('message')} {err.get('details', '')} "
            f"(url={resp.url[:200]})"
        )
    return payload


def layer_metadata(layer_url: str, *, session: requests.Session | None = None) -> dict:
    return _parse(http.get(layer_url, params={"f": "json"}, session=session))


def field_schema(metadata: dict) -> tuple[list[str], list[str]]:
    fields = metadata.get("fields", [])
    return [f["name"] for f in fields], [f.get("type", "") for f in fields]


def last_edit_date(metadata: dict) -> str | None:
    """Epoch-millis string. Changes when the layer is republished, which is the
    only reliable signal that a full re-extract is warranted."""
    value = metadata.get("editingInfo", {}).get("lastEditDate")
    return str(value) if value is not None else None


def count_rows(
    layer_url: str, where: str, *, session: requests.Session | None = None
) -> int:
    payload = _parse(
        http.get(
            f"{layer_url}/query",
            params={"where": where, "returnCountOnly": "true", "f": "json"},
            session=session,
        )
    )
    return int(payload.get("count", 0))


def fetch_object_ids(
    layer_url: str, where: str, *, session: requests.Session | None = None
) -> list[int]:
    """Pin the ID set for this run. Returned unsorted by the service — sorting
    is required before deriving batch bounds, not cosmetic."""
    payload = _parse(
        http.get(
            f"{layer_url}/query",
            params={"where": where, "returnIdsOnly": "true", "f": "json"},
            session=session,
        )
    )
    return sorted(payload.get("objectIds") or [])


def _flatten(feature: dict) -> dict:
    """attributes + geometry into one flat record, geometry kept verbatim."""
    record = dict(feature.get("attributes") or {})
    geometry = feature.get("geometry")
    if geometry:
        record["geometry"] = geometry
    return record


def iter_batches(
    layer_url: str,
    *,
    where: str,
    object_ids: list[int],
    batch_size: int = DEFAULT_BATCH_SIZE,
    session: requests.Session | None = None,
    max_batches: int | None = None,
) -> Iterator[Batch]:
    query_url = f"{layer_url}/query"

    for i in range(0, len(object_ids), batch_size):
        if max_batches is not None and i // batch_size >= max_batches:
            log.warning("stopping after %d batches (--max-batches)", max_batches)
            return

        chunk = object_ids[i : i + batch_size]
        lo, hi = chunk[0], chunk[-1]
        params = {
            "where": f"({where}) AND {OID_FIELD} >= {lo} AND {OID_FIELD} <= {hi}",
            "outFields": "*",
            "returnGeometry": "true",
            "outSR": WGS84,
            "f": "json",
        }
        resp = http.get(query_url, params=params, session=session)
        payload = _parse(resp)

        features = payload.get("features") or []
        if payload.get("exceededTransferLimit"):
            # Bounds come from the pinned ID set, so this means the layer changed
            # under us — the batch is incomplete and the run is no longer coherent.
            raise ArcGISError(
                f"batch {lo}-{hi} hit the transfer limit; the ID snapshot is stale. "
                "Re-run to re-pin."
            )
        if len(features) != len(chunk):
            log.warning(
                "batch %d-%d: expected %d features, got %d (rows changed since the "
                "ID snapshot was taken)", lo, hi, len(chunk), len(features),
            )

        yield Batch(
            records=[_flatten(f) for f in features],
            response=resp,
            params=params,
            lo=lo,
            hi=hi,
        )
