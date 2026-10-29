from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePath
from typing import Iterable, Iterator, Sequence, TypeVar

import numpy as np


T = TypeVar("T")


REMOVE_PREFIXES = (
    "before_",
    "after_",
    "before-after_",
    "beforeafter_",
    "before-",
    "after-",
)


def basename(value: object) -> str:
    text = str(value or "").strip().replace("\\", "/")
    return text.rsplit("/", 1)[-1]


def normalized_filename(value: object) -> str:
    name = basename(value).strip()
    lowered = name.lower()
    changed = True
    while changed:
        changed = False
        for prefix in REMOVE_PREFIXES:
            if lowered.startswith(prefix):
                name = name[len(prefix) :]
                lowered = name.lower()
                changed = True
    return name


def should_compare_filename(value: object) -> bool:
    name = basename(value).lower()
    return bool(name) and not name.startswith("before_")


def filename_has_excluded_token(value: object, tokens: Iterable[str] = ("AI",)) -> bool:
    name = basename(value).upper()
    for token in tokens:
        token = str(token or "").strip().upper()
        if token and re.search(rf"(^|[^A-Z0-9]){re.escape(token)}([^A-Z0-9]|$)", name):
            return True
    return False


def normalize_oph(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").strip().upper())


def parse_date_from_filename(value: object, utc_offset_hours: int = 7) -> date | None:
    name = basename(value)
    for pattern in (r"(?<!\d)(20\d{6})(?!\d)", r"^(20\d{6})\d{6}(?!\d)"):
        match = re.search(pattern, name)
        if not match:
            continue
        try:
            return datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue

    # Beberapa aplikasi Android menulis System.currentTimeMillis() ke nama file,
    # misalnya JPEG_1782472739654.jpg. Batasi pada prefix kamera dan rentang
    # kalender yang wajar agar nomor identitas acak tidak dianggap sebagai tanggal.
    epoch_match = re.search(r"^(?:JPEG|IMG)[_-](\d{13})(?:\D|$)", name, re.IGNORECASE)
    if epoch_match:
        try:
            offset = timezone(timedelta(hours=utc_offset_hours))
            parsed = datetime.fromtimestamp(
                int(epoch_match.group(1)) / 1000,
                tz=timezone.utc,
            ).astimezone(offset).date()
        except (OverflowError, OSError, ValueError):
            return None
        if date(2000, 1, 1) <= parsed < date(2100, 1, 1):
            return parsed
    return None


def pack_vector(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float16).tobytes(order="C")


def unpack_vector(payload: bytes | memoryview, dim: int) -> np.ndarray:
    vector = np.frombuffer(payload, dtype=np.float16, count=dim).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


def group_signature(file_uids: Iterable[str]) -> str:
    value = "\n".join(sorted({str(x) for x in file_uids if x}))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def chunks(values: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.rank = [0] * size
        self.size = [1] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int, max_size: int | None = None) -> bool:
        root_l = self.find(left)
        root_r = self.find(right)
        if root_l == root_r:
            return True
        merged_size = self.size[root_l] + self.size[root_r]
        if max_size is not None and merged_size > max_size:
            return False
        if self.rank[root_l] < self.rank[root_r]:
            self.parent[root_l] = root_r
            self.size[root_r] = merged_size
        elif self.rank[root_l] > self.rank[root_r]:
            self.parent[root_r] = root_l
            self.size[root_l] = merged_size
        else:
            self.parent[root_r] = root_l
            self.rank[root_l] += 1
            self.size[root_l] = merged_size
        return True

    def component_size(self, value: int) -> int:
        return self.size[self.find(value)]

    def components(self, minimum_size: int = 2) -> list[list[int]]:
        grouped: dict[int, list[int]] = {}
        for idx in range(len(self.parent)):
            grouped.setdefault(self.find(idx), []).append(idx)
        return [members for members in grouped.values() if len(members) >= minimum_size]
