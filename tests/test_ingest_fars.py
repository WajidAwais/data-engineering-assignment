"""Offline tests for FARS bulk ingestion.

No network. Covers the per-year inconsistencies that made the loader non-trivial:
archive layout, filename case, and text encoding all vary across years.
"""

from __future__ import annotations

import zipfile

import pytest

from src.ingest import fars

HEADER = "STATE,ST_CASE,LATITUDE,LONGITUD\n"
ROWS = "1,10001,33.47131111,-87.49670278\n1,10002,77.7777,777.7777\n"


def _zip(tmp_path, name: str, arcname: str, *, encoding: str = "latin-1", bom: bool = False):
    path = tmp_path / name
    body = HEADER + ROWS
    raw = ("﻿" + body).encode(encoding) if bom else body.encode(encoding)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(arcname, raw)
    return path


# --- archive layout varies by year -----------------------------------------


def test_reads_csv_from_a_subdirectory(tmp_path):
    """2024 layout: FARS2024NationalCSV/accident.csv"""
    z = _zip(tmp_path, "F2024.zip", "FARS2024NationalCSV/accident.csv")
    records, fields = fars.read_table(z, 2024, "accident")
    assert len(records) == 2
    assert "ST_CASE" in fields


def test_reads_csv_from_the_archive_root(tmp_path):
    """2019 layout: accident.CSV at the root, uppercase extension."""
    z = _zip(tmp_path, "F2019.zip", "accident.CSV")
    records, _ = fars.read_table(z, 2019, "accident")
    assert len(records) == 2


def test_filename_case_is_ignored(tmp_path):
    """2019 ships `Person.CSV`, 2024 ships `person.csv`."""
    z = _zip(tmp_path, "F2019.zip", "Person.CSV")
    records, _ = fars.read_table(z, 2019, "person")
    assert len(records) == 2


def test_missing_table_names_what_is_actually_there(tmp_path):
    z = _zip(tmp_path, "F2024.zip", "FARS2024NationalCSV/accident.csv")
    with pytest.raises(FileNotFoundError, match="accident.csv"):
        fars.read_table(z, 2024, "vehicle")


# --- encoding varies by year ------------------------------------------------


def test_utf8_bom_does_not_corrupt_the_first_column(tmp_path):
    """2021 is UTF-8 with a BOM. Decoded as latin-1 the first column name
    becomes 'ï»¿STATE', which lands as a separate bronze column and splits the
    field across years."""
    z = _zip(tmp_path, "F2021.zip", "accident.csv", encoding="utf-8", bom=True)
    _, fields = fars.read_table(z, 2021, "accident")

    assert "STATE" in fields
    assert not [f for f in fields if "﻿" in f or f.startswith("ï»¿")]


def test_latin1_bytes_do_not_raise(tmp_path):
    """Most years are latin-1. utf-8-sig is tried first and must fall back."""
    path = tmp_path / "F2019.zip"
    body = (HEADER + "1,10001,33.4,-87.4\n").encode("latin-1") + b"1,10003,\xe9,\xe9\n"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("accident.CSV", body)

    records, _ = fars.read_table(path, 2019, "accident")
    assert len(records) == 2


# --- keys -------------------------------------------------------------------


def test_record_id_includes_the_year(tmp_path):
    """ST_CASE is unique only within a year; 2019 and 2024 both have 10001."""
    rec = {"ST_CASE": "10001"}
    assert fars.record_id("accident", 2019, rec) == "2019:10001"
    assert fars.record_id("accident", 2024, rec) != fars.record_id("accident", 2019, rec)


def test_person_record_id_uses_the_full_grain():
    rec = {"ST_CASE": "10001", "VEH_NO": "1", "PER_NO": "2"}
    assert fars.record_id("person", 2024, rec) == "2024:10001:1:2"


def test_year_is_stamped_on_every_record(tmp_path):
    z = _zip(tmp_path, "F2024.zip", "FARS2024NationalCSV/accident.csv")
    records, fields = fars.read_table(z, 2024, "accident")
    assert "_fars_year" in fields
    assert {r["_fars_year"] for r in records} == {"2024"}
