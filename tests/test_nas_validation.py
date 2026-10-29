from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from golden_eye.nas_validation import NasCandidateReader


def test_nas_candidate_reader_streams_and_verifies_hash(tmp_path: Path) -> None:
    payload = b"candidate-image-payload"
    image_path = tmp_path / "nested" / "PHOTO_20260101.JPG"
    image_path.parent.mkdir()
    image_path.write_bytes(payload)

    reader = NasCandidateReader([tmp_path], ["photo_20260101.jpg"])
    reader.require_complete()
    rows = [
        {
            "file_uid": "uid-1",
            "file_name": "PHOTO_20260101.JPG",
            "file_sha256": hashlib.sha256(payload).hexdigest(),
            "image_bytes": None,
        }
    ]
    with ThreadPoolExecutor(max_workers=1) as executor:
        loaded = reader.attach_payloads(rows, executor)

    assert loaded[0]["image_bytes"] == payload
    assert reader.metrics()["validation_payload_mode"] == "NAS_CANDIDATE_STREAM"
    assert reader.metrics()["validation_nas_payload_reads"] == 1


def test_nas_candidate_reader_rejects_missing_or_wrong_payload(tmp_path: Path) -> None:
    missing = NasCandidateReader([tmp_path], ["missing.jpg"])
    with pytest.raises(RuntimeError, match="coverage tidak lengkap"):
        missing.require_complete()

    image_path = tmp_path / "wrong.jpg"
    image_path.write_bytes(b"wrong")
    reader = NasCandidateReader([tmp_path], ["wrong.jpg"])
    reader.require_complete()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(RuntimeError, match="SHA256"):
            reader.attach_payloads(
                [
                    {
                        "file_uid": "uid-wrong",
                        "file_name": "wrong.jpg",
                        "file_sha256": hashlib.sha256(b"expected").hexdigest(),
                        "image_bytes": None,
                    }
                ],
                executor,
            )


def test_pipeline_contract_uses_embeddings_and_nas_candidates() -> None:
    root = Path(__file__).resolve().parents[1]
    matcher = (root / "src" / "golden_eye" / "matcher.py").read_text(
        encoding="utf-8"
    )
    validator = (root / "src" / "golden_eye" / "validator.py").read_text(
        encoding="utf-8"
    )
    cli = (root / "src" / "golden_eye" / "cli.py").read_text(encoding="utf-8")
    audit = (root / "src" / "golden_eye" / "audit.py").read_text(
        encoding="utf-8"
    )

    assert "allow_nas_validation=bool(" in cli
    assert "validation_nas_roots=[Path(value) for value in args.source_root]" in cli
    assert "vector_source=processing.image_feature" in matcher
    assert "NAS_CANDIDATE_STREAM" in validator
    assert "nas_reader.attach_payloads" in validator
    nas_start = validator.index(
        "if nas_reader is not None:\n                    member_rows"
    )
    nas_branch = validator[
        nas_start : validator.index("                else:", nas_start)
    ]
    assert "r.image_bytes" not in nas_branch
    assert "processing.gambar_reused" not in nas_branch
    assert "NULL::bytea AS image_bytes" in nas_branch
    assert '"f.ctid, m.file_uid"' in validator
    assert "thread_name_prefix=\"golden-validate-nas\"" in validator
    assert "db={db_fetch_seconds:,.1f}s nas={nas_fetch_seconds:,.1f}s" in validator
    assert "batch_progress=" in validator
    assert "eta={eta_hours:02d}h{eta_minutes:02d}m" in validator
    assert "FROM processing.image_feature f" in audit
    assert '"historical_months_missing_silver"' in audit
