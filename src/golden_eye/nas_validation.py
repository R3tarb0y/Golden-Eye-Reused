from __future__ import annotations

import hashlib
import os
from concurrent.futures import Executor
from pathlib import Path
from typing import Any, Iterable


def logical_image_name(value: Any) -> str:
    """Canonical basename yang sama untuk path Windows maupun nama biasa."""
    return Path(str(value or "").replace("\\", "/")).name.strip().upper()


class NasCandidateReader:
    """Resolve payload candidate MATCH dari NAS tanpa menyalinnya ke Silver.

    Directory hanya dipindai sekali. Pada mode ini payload candidate selalu
    dibaca dari NAS, tidak pernah dari Silver atau gambar_reused. Payload segera
    dilepas setelah descriptor selesai dibentuk. Hanya evidence yang lolos dan
    masuk Gold yang nantinya disimpan oleh flow gambar_reused.
    """

    def __init__(self, roots: Iterable[str | Path], wanted_names: Iterable[str]):
        self.roots = [Path(value) for value in roots]
        self.wanted = {
            logical_image_name(value) for value in wanted_names
            if logical_image_name(value)
        }
        self.paths: dict[str, Path] = {}
        self.db_payloads = 0
        self.nas_payloads = 0
        self.nas_bytes = 0
        self.read_failures = 0
        self._build_index()

    def _build_index(self) -> None:
        remaining = set(self.wanted)
        for root in self.roots:
            if not root.exists():
                raise FileNotFoundError(f"NAS validation root tidak ditemukan: {root}")
            print(
                f"[VALIDATE NAS] scan directory root={root} "
                f"wanted_remaining={len(remaining):,}",
                flush=True,
            )
            for dirpath, _dirnames, filenames in os.walk(root):
                for filename in filenames:
                    key = logical_image_name(filename)
                    if key in remaining:
                        self.paths[key] = Path(dirpath) / filename
                        remaining.discard(key)
                if not remaining:
                    break
            print(
                f"[VALIDATE NAS] indexed={len(self.paths):,}/{len(self.wanted):,} "
                f"remaining={len(remaining):,}",
                flush=True,
            )
            if not remaining:
                break
        self.missing_names = sorted(remaining)

    def require_complete(self) -> None:
        if not self.missing_names:
            return
        raise RuntimeError(
            f"VALIDATE NAS coverage tidak lengkap: missing={len(self.missing_names):,} "
            f"dari candidate_needed={len(self.wanted):,}; "
            f"sample={self.missing_names[:10]}. Tambahkan --source-root yang "
            "memuat file tersebut. Validasi belum dijalankan."
        )

    @staticmethod
    def _read_one(item: tuple[dict[str, Any], Path | None]) -> tuple[dict[str, Any], str, int]:
        row, path = item
        output = dict(row)
        if output.get("image_bytes") is not None:
            return output, "DB", 0
        if path is None:
            return output, "MISSING", 0
        try:
            payload = path.read_bytes()
            if not payload:
                raise ValueError("file kosong")
            expected_hash = str(output.get("file_sha256") or "").strip().lower()
            if expected_hash:
                actual_hash = hashlib.sha256(payload).hexdigest()
                if actual_hash != expected_hash:
                    raise ValueError(
                        f"SHA256 berbeda expected={expected_hash} actual={actual_hash}"
                    )
            output["image_bytes"] = payload
            output["nas_path"] = str(path)
            return output, "NAS", len(payload)
        except Exception as exc:
            output["nas_read_error"] = f"{path}: {exc}"
            return output, "FAILED", 0

    def attach_payloads(
        self,
        rows: list[dict[str, Any]],
        executor: Executor,
    ) -> list[dict[str, Any]]:
        work = [
            (row, self.paths.get(logical_image_name(row.get("file_name"))))
            for row in rows
        ]
        output: list[dict[str, Any]] = []
        failures: list[str] = []
        for row, source, payload_size in executor.map(self._read_one, work):
            output.append(row)
            if source == "DB":
                self.db_payloads += 1
            elif source == "NAS":
                self.nas_payloads += 1
                self.nas_bytes += payload_size
            else:
                self.read_failures += 1
                failures.append(
                    str(row.get("nas_read_error") or row.get("file_name") or "UNKNOWN")
                )
        if failures:
            raise RuntimeError(
                f"VALIDATE NAS gagal membaca/menyamakan hash {len(failures):,} "
                f"candidate pada batch; sample={failures[:5]}. "
                "Batch tidak dipublish."
            )
        return output

    def metrics(self) -> dict[str, Any]:
        return {
            "validation_payload_mode": "NAS_CANDIDATE_STREAM",
            "validation_nas_candidate_names": len(self.wanted),
            "validation_nas_indexed_names": len(self.paths),
            "validation_nas_missing_names": len(self.missing_names),
            "validation_nas_payload_reads": self.nas_payloads,
            "validation_nas_payload_bytes": self.nas_bytes,
            "validation_hot_payload_reads": self.db_payloads,
            "validation_nas_read_failures": self.read_failures,
        }
