from __future__ import annotations

from pathlib import Path

from PIL import Image

from golden_eye.silver_loader import (
    ScanStats,
    deterministic_file_uid,
    discover_source_files,
    iter_month_scoped_files,
    iter_source_files,
    normalize_expected_month,
    resolve_duplicate_logical_names,
    silver_write_failure_message,
)


def _image(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), color).save(path, format="JPEG")


def test_uid_is_stable_by_source_and_logical_filename() -> None:
    first = deterministic_file_uid("pwp", "Foto_01.JPG")
    assert first == deterministic_file_uid("PWP", "foto_01.jpg")
    assert first != deterministic_file_uid("DIN", "foto_01.jpg")
    assert first != deterministic_file_uid("PWP", "foto_02.jpg")


def test_corrupt_silver_index_message_is_actionable_and_quoted() -> None:
    message = silver_write_failure_message(
        RuntimeError(
            'index "ix_image_all_sha256" contains unexpected zero page at block 101'
        ),
        schema="silver",
        table="image_store_all_files",
    )
    assert message is not None
    assert 'REINDEX INDEX "silver"."ix_image_all_sha256";' in message
    assert 'ANALYZE "silver"."image_store_all_files";' in message
    assert "tidak dicoba dua kali" in message


def test_non_storage_write_error_is_not_relabelled() -> None:
    assert (
        silver_write_failure_message(
            RuntimeError("connection timed out"),
            schema="silver",
            table="image_store_all_files",
        )
        is None
    )


def test_discovery_filters_extensions_and_limits(tmp_path: Path) -> None:
    _image(tmp_path / "a.jpg", (255, 0, 0))
    _image(tmp_path / "nested" / "b.png", (0, 255, 0))
    (tmp_path / "notes.txt").write_text("skip", encoding="utf-8")

    rows, problems = discover_source_files(
        [tmp_path], [".jpg", ".png"], max_files=1
    )
    assert len(rows) == 1
    assert rows[0].logical_name in {"A.JPG", "B.PNG"}
    assert problems == []


def test_stream_scan_stops_at_max_files(tmp_path: Path) -> None:
    for index in range(5):
        _image(tmp_path / "nested" / f"{index}.jpg", (index, 0, 0))
    (tmp_path / "ignored.txt").write_text("skip", encoding="utf-8")

    problems: list[dict] = []
    stats = ScanStats()
    rows = list(
        iter_source_files(
            [tmp_path],
            [".jpg"],
            problems,
            stats,
            max_files=2,
            progress_every=0,
        )
    )

    assert len(rows) == 2
    assert stats.eligible_files == 2
    assert stats.eligible_bytes > 0
    assert problems == []


def test_stream_scan_can_target_only_recovery_report_names(tmp_path: Path) -> None:
    _image(tmp_path / "wanted_20260728.jpg", (255, 0, 0))
    _image(tmp_path / "ignored_20260728.jpg", (0, 255, 0))
    problems: list[dict] = []
    stats = ScanStats()
    found: set[str] = set()
    rows = list(
        iter_source_files(
            [tmp_path],
            [".jpg"],
            problems,
            stats,
            progress_every=0,
            target_names={"WANTED_20260728.JPG"},
            found_target_names=found,
        )
    )
    assert [row.logical_name for row in rows] == ["WANTED_20260728.JPG"]
    assert found == {"WANTED_20260728.JPG"}
    assert stats.target_outside == 1


def test_incremental_month_scope_keeps_only_expected_filename_month(
    tmp_path: Path,
) -> None:
    _image(tmp_path / "0102132_20260728_175524.jpg", (255, 0, 0))
    _image(tmp_path / "0102132_20260628_175524.jpg", (0, 255, 0))
    _image(tmp_path / "NO_CAMERA_DATE.jpg", (0, 0, 255))
    problems: list[dict] = []
    scan_stats = ScanStats()
    stats = ScanStats()
    scanned = iter_source_files(
        [tmp_path], [".jpg"], problems, scan_stats, progress_every=0
    )
    rows = list(
        iter_month_scoped_files(
            scanned,
            "2026-07",
            timestamp_utc_offset_hours=7,
            report_rows=problems,
            stats=stats,
        )
    )

    assert [row.path.name for row in rows] == ["0102132_20260728_175524.jpg"]
    assert stats.month_accepted == 1
    assert stats.month_outside == 1
    assert stats.month_unparsed == 1
    assert {row["status"] for row in problems} == {
        "MONTH_OUT_OF_SCOPE_SKIPPED",
        "MONTH_UNPARSED_SKIPPED",
    }


def test_mixed_root_month_filter_runs_before_target_file_stat(
    tmp_path: Path,
) -> None:
    _image(tmp_path / "0102132_20260728_175524.jpg", (255, 0, 0))
    _image(tmp_path / "0102132_20260628_175524.jpg", (0, 255, 0))
    _image(tmp_path / "NO_CAMERA_DATE.jpg", (0, 0, 255))
    problems: list[dict] = []
    stats = ScanStats()

    rows = list(
        iter_source_files(
            [tmp_path],
            [".jpg"],
            problems,
            stats,
            progress_every=0,
            expected_month="2026-07",
            timestamp_utc_offset_hours=7,
        )
    )

    assert [row.path.name for row in rows] == ["0102132_20260728_175524.jpg"]
    assert stats.eligible_files == 1
    assert stats.month_accepted == 1
    assert stats.month_outside == 1
    assert stats.month_unparsed == 1


def test_incremental_month_requires_yyyy_mm() -> None:
    assert normalize_expected_month("2026-07") == "2026-07"
    try:
        normalize_expected_month("Juli-2026")
    except ValueError as exc:
        assert "YYYY-MM" in str(exc)
    else:
        raise AssertionError("invalid month harus ditolak")


def test_exact_duplicate_name_keeps_one(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _image(root_a / "same.jpg", (255, 0, 0))
    (root_b / "same.jpg").parent.mkdir(parents=True)
    (root_b / "same.jpg").write_bytes((root_a / "same.jpg").read_bytes())

    files, report = discover_source_files(
        [root_a, root_b], [".jpg"]
    )
    survivors, conflicts = resolve_duplicate_logical_names(files, "PWP", report)

    assert len(survivors) == 1
    assert conflicts == 0
    assert any(
        row["status"] == "DUPLICATE_LOGICAL_SAME_HASH_SKIPPED"
        for row in report
    )


def test_same_name_different_hash_blocks_load(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    _image(root_a / "same.jpg", (255, 0, 0))
    _image(root_b / "same.jpg", (0, 0, 255))

    files, report = discover_source_files(
        [root_a, root_b], [".jpg"]
    )
    survivors, conflicts = resolve_duplicate_logical_names(files, "DWT", report)

    assert survivors == []
    assert conflicts == 1
    assert sum(
        row["status"] == "CONFLICT_SAME_NAME_DIFFERENT_HASH"
        for row in report
    ) == 2
