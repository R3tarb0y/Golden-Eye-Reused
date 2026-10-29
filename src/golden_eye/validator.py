from __future__ import annotations

import io
import itertools
import json
import threading
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import cv2
import imagehash
import numpy as np
import torch
from PIL import Image
from psycopg2.extras import execute_values

from .config import Settings
from .db import connection, fetch_all_dict, qname, update_run
from .nas_validation import NasCandidateReader
from .utils import UnionFind, group_signature, unpack_vector


@dataclass
class Descriptor:
    uid: str
    file_name: str
    full: np.ndarray
    center: np.ndarray
    phash: Any
    edgehash: Any
    edgevec: np.ndarray | None
    hsvhist: np.ndarray | None
    keypoints: Any
    orb: np.ndarray | None
    luma_mean: float
    dark_fraction: float


@dataclass
class PairResult:
    full_emb_sim: float
    center_emb_sim: float
    phash_dist: int
    edgehash_dist: int
    edgevec_sim: float
    hsvhist_sim: float
    raw_matches: int
    inliers: int
    inlier_ratio: float
    pair_score: float
    support_count: int
    color_texture_trap: bool
    same_object: bool
    structural_evidence: bool
    raw_pair_score: float
    low_light_pair: bool
    low_light_guard_pass: bool
    left_luma_mean: float
    right_luma_mean: float
    left_dark_fraction: float
    right_dark_fraction: float


@dataclass(frozen=True)
class NumericPairMetrics:
    full_emb_sim: float
    center_emb_sim: float
    edgevec_sim: float
    hsvhist_sim: float


@dataclass(frozen=True)
class ValidationRuntime:
    numeric_device: torch.device
    device_name: str
    cpu_workers: int


@dataclass(frozen=True)
class GroupEmbeddingQuality:
    """Exact all-pair quality in the same 0..100 scale as MATCH similarity."""

    mean_similarity: float
    min_member_mean_similarity: float
    member_means: dict[str, float]


_ORB_LOCAL = threading.local()

# Aturan ini sengaja sama dengan file validasi image_utils lama. Threshold
# similarity awal tetap REVIEW=90 dan REUSED=93; nilai berikut hanya dipakai
# untuk membersihkan/split candidate berdasarkan bukti visual multimodal.
_COMPONENT_EDGE_SCORE = 0.52
_COMPONENT_EDGE_ALT_CENTER = 87.5
_COMPONENT_EDGE_ALT_EDGEVEC = 0.70
_MIN_COMPONENT_MEAN_CONFIRMED = 0.50
_MIN_COMPONENT_MEAN_REVIEW = 0.52
_MIN_REP_PAIR_SCORE = 0.50
_MIN_MEMBER_TOP2_MEAN = 0.50
_MIN_NEIGHBOR_COUNT = 1
_STRONG_EDGE_SCORE = 0.62
_PAIR_SCORE_GROUP2_MIN = 0.66
_CENTER_SIM_GROUP2_MIN = 88.8
_EDGE_VEC_GROUP2_MIN = 0.68
_INLIERS_GROUP2_MIN = 5


def _thread_orb_detector():
    detector = getattr(_ORB_LOCAL, "detector", None)
    if detector is None:
        detector = cv2.ORB_create(nfeatures=2200)
        _ORB_LOCAL.detector = detector
    return detector


def _validation_cuda_ready() -> tuple[bool, str]:
    if not torch.cuda.is_available():
        return False, "torch.cuda.is_available()=False"
    try:
        left = torch.ones((16, 16), dtype=torch.float32, device="cuda")
        right = left @ left
        _ = float(right[0, 0].item())
        torch.cuda.synchronize()
        return True, torch.cuda.get_device_name(0)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _validation_runtime(settings: Settings) -> ValidationRuntime:
    requested = settings.validation_device
    try_cuda = requested == "cuda" or (requested == "auto" and settings.use_gpu)
    cuda_ready, cuda_info = _validation_cuda_ready() if try_cuda else (False, "disabled")
    if requested == "cuda" and not cuda_ready:
        raise RuntimeError(
            "VALIDATION_DEVICE=cuda tetapi CUDA validator tidak siap: "
            f"{cuda_info}. Perbarui PyTorch/driver atau gunakan VALIDATION_DEVICE=auto."
        )
    if cuda_ready:
        runtime = ValidationRuntime(
            numeric_device=torch.device("cuda"),
            device_name=cuda_info,
            cpu_workers=settings.validation_cpu_workers,
        )
    else:
        runtime = ValidationRuntime(
            numeric_device=torch.device("cpu"),
            device_name="CPU",
            cpu_workers=settings.validation_cpu_workers,
        )
        if try_cuda:
            print(f"[VALIDATE] CUDA tidak siap ({cuda_info}); numeric fallback ke CPU")
    print(
        f"[VALIDATE] runtime numeric_device={runtime.numeric_device} "
        f"({runtime.device_name}) cpu_workers={runtime.cpu_workers} "
        f"gpu_pair_batch={settings.validation_gpu_pair_batch:,}"
    )
    return runtime


def _center_crop(image: np.ndarray, ratio: float) -> np.ndarray:
    height, width = image.shape[:2]
    crop_w = max(1, int(width * ratio))
    crop_h = max(1, int(height * ratio))
    left = max(0, (width - crop_w) // 2)
    top = max(0, (height - crop_h) // 2)
    return image[top : top + crop_h, left : left + crop_w]


def _edge_vector(gray: np.ndarray) -> np.ndarray:
    edges = cv2.Canny(gray, 80, 160)
    small = cv2.resize(edges, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    vector = small.ravel()
    return vector / (np.linalg.norm(vector) + 1e-8)


def _hsv_hist(image: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [16, 16], [0, 180, 0, 256]).astype(np.float32)
    return cv2.normalize(hist, None).ravel()


def _hist_intersection(left: np.ndarray | None, right: np.ndarray | None) -> float:
    if left is None or right is None:
        return 0.0
    return float(np.minimum(left, right).sum() / (np.maximum(left, right).sum() + 1e-8))


def _orb_stats(left: Descriptor, right: Descriptor) -> tuple[int, int, float]:
    if left.orb is None or right.orb is None or len(left.orb) < 8 or len(right.orb) < 8:
        return 0, 0, 0.0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    try:
        matches = matcher.knnMatch(left.orb, right.orb, k=2)
    except cv2.error:
        return 0, 0, 0.0
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance]
    raw = len(good)
    if raw < 4:
        return raw, 0, 0.0
    source = np.float32([left.keypoints[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
    target = np.float32([right.keypoints[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
    try:
        _matrix, mask = cv2.findHomography(source, target, cv2.RANSAC, 5.0)
    except cv2.error:
        mask = None
    inliers = int(mask.ravel().sum()) if mask is not None else 0
    return raw, inliers, float(inliers / raw) if raw else 0.0


def _descriptor(
    row: dict[str, Any],
    settings: Settings,
    orb_detector=None,
) -> Descriptor:
    payload = bytes(row["image_bytes"])
    array = np.frombuffer(payload, dtype=np.uint8)
    bgr = cv2.imdecode(array, cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError("OpenCV tidak dapat decode BYTEA")
    center = _center_crop(bgr, settings.center_crop_ratio)
    gray = cv2.cvtColor(center, cv2.COLOR_BGR2GRAY)
    luma_mean = float(np.mean(gray))
    dark_fraction = float(
        np.mean(gray <= settings.low_light_dark_pixel_threshold)
    )
    detector = orb_detector or _thread_orb_detector()
    keypoints, orb_desc = detector.detectAndCompute(gray, None)
    with Image.open(io.BytesIO(payload)) as pil:
        full_hash = imagehash.phash(pil.convert("RGB"))
    edge_hash = imagehash.phash(Image.fromarray(cv2.Canny(gray, 80, 160)))
    return Descriptor(
        uid=row["file_uid"],
        file_name=row["file_name"],
        full=unpack_vector(row["full_embedding"], int(row["embedding_dim"])),
        center=unpack_vector(row["center_embedding"], int(row["embedding_dim"])),
        phash=full_hash,
        edgehash=edge_hash,
        edgevec=_edge_vector(gray),
        hsvhist=_hsv_hist(center),
        keypoints=keypoints,
        orb=orb_desc,
        luma_mean=luma_mean,
        dark_fraction=dark_fraction,
    )


def _is_low_light(descriptor: Descriptor, settings: Settings) -> bool:
    normal_dark = (
        descriptor.luma_mean <= settings.low_light_max_mean
        and descriptor.dark_fraction >= settings.low_light_min_dark_fraction
    )
    very_dark_mean = (
        descriptor.luma_mean <= settings.low_light_max_mean * 0.75
        and descriptor.dark_fraction >= settings.low_light_min_dark_fraction * 0.75
    )
    dominant_dark_pixels = descriptor.dark_fraction >= min(
        0.95, settings.low_light_min_dark_fraction + 0.20
    )
    return bool(
        settings.low_light_guard
        and (normal_dark or very_dark_mean or dominant_dark_pixels)
    )


def _descriptor_job(
    payload: tuple[dict[str, Any], Settings],
) -> tuple[Descriptor | None, tuple[str, str] | None]:
    row, settings = payload
    try:
        return _descriptor(row, settings), None
    except Exception as exc:
        return None, (str(row["file_uid"]), str(exc)[:500])


def _decode_descriptors(
    rows: list[dict[str, Any]],
    settings: Settings,
    executor: ThreadPoolExecutor,
) -> tuple[dict[str, Descriptor], list[tuple[str, str]]]:
    descriptors: dict[str, Descriptor] = {}
    errors: list[tuple[str, str]] = []
    for descriptor, error in executor.map(
        _descriptor_job,
        ((row, settings) for row in rows),
    ):
        if descriptor is not None:
            descriptors[descriptor.uid] = descriptor
        elif error is not None:
            errors.append(error)
    return descriptors, errors


@torch.inference_mode()
def _gpu_numeric_pair_metrics(
    descriptors: dict[str, Descriptor],
    members: list[str],
    pairs: list[tuple[str, str]],
    runtime: ValidationRuntime,
    batch_size: int,
) -> list[NumericPairMetrics] | None:
    if not pairs or runtime.numeric_device.type != "cuda":
        return None

    positions = {uid: idx for idx, uid in enumerate(members)}
    full = torch.from_numpy(
        np.stack([descriptors[uid].full for uid in members]).astype(np.float32, copy=False)
    ).to(runtime.numeric_device)
    center = torch.from_numpy(
        np.stack([descriptors[uid].center for uid in members]).astype(np.float32, copy=False)
    ).to(runtime.numeric_device)
    edgevec = torch.from_numpy(
        np.stack(
            [
                descriptors[uid].edgevec
                if descriptors[uid].edgevec is not None
                else np.zeros(1024, dtype=np.float32)
                for uid in members
            ]
        ).astype(np.float32, copy=False)
    ).to(runtime.numeric_device)
    hsvhist = torch.from_numpy(
        np.stack(
            [
                descriptors[uid].hsvhist
                if descriptors[uid].hsvhist is not None
                else np.zeros(256, dtype=np.float32)
                for uid in members
            ]
        ).astype(np.float32, copy=False)
    ).to(runtime.numeric_device)

    output: list[NumericPairMetrics] = []
    for start in range(0, len(pairs), batch_size):
        batch_pairs = pairs[start : start + batch_size]
        left_idx = torch.tensor(
            [positions[left] for left, _right in batch_pairs],
            dtype=torch.long,
            device=runtime.numeric_device,
        )
        right_idx = torch.tensor(
            [positions[right] for _left, right in batch_pairs],
            dtype=torch.long,
            device=runtime.numeric_device,
        )
        full_sim = (full[left_idx] * full[right_idx]).sum(dim=1) * 100.0
        center_sim = (center[left_idx] * center[right_idx]).sum(dim=1) * 100.0
        edge_sim = (edgevec[left_idx] * edgevec[right_idx]).sum(dim=1)
        hist_left = hsvhist[left_idx]
        hist_right = hsvhist[right_idx]
        hist_sim = torch.minimum(hist_left, hist_right).sum(dim=1) / (
            torch.maximum(hist_left, hist_right).sum(dim=1) + 1e-8
        )
        values = torch.stack((full_sim, center_sim, edge_sim, hist_sim), dim=1).cpu().numpy()
        output.extend(
            NumericPairMetrics(
                full_emb_sim=float(row[0]),
                center_emb_sim=float(row[1]),
                edgevec_sim=float(row[2]),
                hsvhist_sim=float(row[3]),
            )
            for row in values
        )
    return output


def compare_pair(
    left: Descriptor,
    right: Descriptor,
    settings: Settings,
    numeric: NumericPairMetrics | None = None,
) -> PairResult:
    full_sim = (
        numeric.full_emb_sim
        if numeric is not None
        else float(np.dot(left.full, right.full) * 100.0)
    )
    center_sim = (
        numeric.center_emb_sim
        if numeric is not None
        else float(np.dot(left.center, right.center) * 100.0)
    )
    phash_dist = int(left.phash - right.phash) if left.phash is not None and right.phash is not None else 999
    edgehash_dist = int(left.edgehash - right.edgehash) if left.edgehash is not None and right.edgehash is not None else 999
    edgevec_sim = (
        numeric.edgevec_sim
        if numeric is not None
        else float(np.dot(left.edgevec, right.edgevec))
        if left.edgevec is not None and right.edgevec is not None
        else 0.0
    )
    hsvhist_sim = (
        numeric.hsvhist_sim
        if numeric is not None
        else _hist_intersection(left.hsvhist, right.hsvhist)
    )
    raw, inliers, inlier_ratio = _orb_stats(left, right)

    full_score = float(np.clip((full_sim - 78.0) / 22.0, 0.0, 1.0))
    center_score = float(np.clip((center_sim - 78.0) / 22.0, 0.0, 1.0))
    phash_score = 0.0 if phash_dist >= 20 else float(np.clip(1.0 - phash_dist / 20.0, 0.0, 1.0))
    edgehash_score = 0.0 if edgehash_dist >= 20 else float(np.clip(1.0 - edgehash_dist / 20.0, 0.0, 1.0))
    raw_pair_score = (
        0.14 * full_score
        + 0.24 * center_score
        + 0.08 * phash_score
        + 0.12 * edgehash_score
        + 0.18 * float(np.clip(edgevec_sim, 0.0, 1.0))
        + 0.10 * float(np.clip(hsvhist_sim, 0.0, 1.0))
        + 0.10 * float(np.clip(inliers / 25.0, 0.0, 1.0))
        + 0.03 * float(np.clip(inlier_ratio / 0.40, 0.0, 1.0))
        + 0.01 * float(np.clip(raw / 40.0, 0.0, 1.0))
    )
    support = sum(
        [
            full_sim >= 83.5,
            center_sim >= 85.5,
            phash_dist <= 13,
            edgehash_dist <= 14,
            edgevec_sim >= 0.64,
            hsvhist_sim >= 0.48,
            inliers >= 5,
            inlier_ratio >= 0.10,
        ]
    )
    trap = hsvhist_sim >= 0.85 and edgevec_sim <= 0.54 and inliers <= 4
    left_low_light = _is_low_light(left, settings)
    right_low_light = _is_low_light(right, settings)
    low_light_pair = left_low_light or right_low_light
    near_exact_evidence = (
        min(full_sim, center_sim) >= settings.low_light_near_exact_similarity
        and phash_dist <= settings.low_light_max_phash_distance
        and edgehash_dist <= settings.low_light_max_edgehash_distance
    )
    geometric_evidence = (
        center_sim >= settings.review_threshold
        and edgevec_sim >= settings.low_light_min_edgevec_similarity
        and inliers >= settings.low_light_min_orb_inliers
        and inlier_ratio >= settings.low_light_min_orb_ratio
    )
    low_light_guard_pass = bool(
        not low_light_pair or near_exact_evidence or geometric_evidence
    )
    low_light_guard_rejected = low_light_pair and not low_light_guard_pass
    near_exact_structural = (
        min(full_sim, center_sim)
        >= getattr(settings, "duplicate_near_exact_similarity", 96.0)
        and phash_dist <= getattr(settings, "duplicate_max_phash_distance", 10)
        and edgehash_dist <= getattr(settings, "duplicate_max_edgehash_distance", 12)
    )
    geometric_similarity = getattr(
        settings,
        "duplicate_geometric_similarity",
        getattr(settings, "reused_threshold", 93.0),
    )
    geometric_hash_support = (
        phash_dist
        <= getattr(settings, "duplicate_geometric_max_hash_distance", 18)
        or edgehash_dist
        <= getattr(settings, "duplicate_geometric_max_hash_distance", 18)
    )
    geometric_structural = (
        full_sim >= geometric_similarity
        and center_sim >= geometric_similarity
        and edgevec_sim >= getattr(settings, "duplicate_min_edgevec_similarity", 0.80)
        and inliers >= getattr(settings, "duplicate_min_orb_inliers", 12)
        and inlier_ratio >= getattr(settings, "duplicate_min_orb_ratio", 0.18)
        and (
            not getattr(settings, "duplicate_require_hash_support", True)
            or geometric_hash_support
        )
    )
    structural_evidence = bool(
        not getattr(settings, "duplicate_require_structural_evidence", True)
        or near_exact_structural
        or geometric_structural
    )
    # Pair malam yang hanya mirip karena dominasi hitam/tekstur diberi score
    # efektif nol. raw_pair_score tetap disimpan pada audit untuk tuning.
    pair_score = 0.0 if low_light_guard_rejected else raw_pair_score
    trap = trap or low_light_guard_rejected
    same = (
        pair_score >= settings.pair_score_edge
        and support >= settings.min_modal_support
        and not trap
        and structural_evidence
    )
    if (
        pair_score >= settings.pair_score_strong
        and not low_light_guard_rejected
        and structural_evidence
    ):
        same = True
    return PairResult(
        full_emb_sim=full_sim,
        center_emb_sim=center_sim,
        phash_dist=phash_dist,
        edgehash_dist=edgehash_dist,
        edgevec_sim=edgevec_sim,
        hsvhist_sim=hsvhist_sim,
        raw_matches=raw,
        inliers=inliers,
        inlier_ratio=inlier_ratio,
        pair_score=float(pair_score),
        support_count=int(support),
        color_texture_trap=bool(trap),
        same_object=bool(same),
        structural_evidence=structural_evidence,
        raw_pair_score=float(raw_pair_score),
        low_light_pair=bool(low_light_pair),
        low_light_guard_pass=bool(low_light_guard_pass),
        left_luma_mean=left.luma_mean,
        right_luma_mean=right.luma_mean,
        left_dark_fraction=left.dark_fraction,
        right_dark_fraction=right.dark_fraction,
    )


def _compare_pair_job(
    payload: tuple[
        tuple[str, str],
        Descriptor,
        Descriptor,
        Settings,
        NumericPairMetrics | None,
    ],
) -> tuple[tuple[str, str], PairResult]:
    pair, left, right, settings, numeric = payload
    return pair, compare_pair(left, right, settings, numeric)


def _compare_pairs(
    descriptors: dict[str, Descriptor],
    pairs: list[tuple[str, str]],
    settings: Settings,
    runtime: ValidationRuntime,
    executor: ThreadPoolExecutor,
) -> tuple[dict[tuple[str, str], PairResult], bool]:
    numeric_metrics = _gpu_numeric_pair_metrics(
        descriptors,
        sorted(descriptors),
        pairs,
        runtime,
        settings.validation_gpu_pair_batch,
    )
    used_gpu = numeric_metrics is not None

    results: dict[tuple[str, str], PairResult] = {}
    for start in range(0, len(pairs), settings.validation_cpu_pair_batch):
        stop = min(len(pairs), start + settings.validation_cpu_pair_batch)

        def payloads():
            for index in range(start, stop):
                pair = pairs[index]
                left_uid, right_uid = pair
                numeric = numeric_metrics[index] if numeric_metrics is not None else None
                yield (
                    pair,
                    descriptors[left_uid],
                    descriptors[right_uid],
                    settings,
                    numeric,
                )

        results.update(executor.map(_compare_pair_job, payloads()))
    return results, used_gpu


def _candidate_pairs(
    members: list[str],
    oversize: bool,
    edge_rows: list[dict[str, Any]],
    top_k: int,
) -> list[tuple[str, str]]:
    if not oversize:
        return list(itertools.combinations(members, 2))
    by_uid: dict[str, list[tuple[float, str]]] = {uid: [] for uid in members}
    allowed = set(members)
    for row in edge_rows:
        left, right = row["file_uid_a"], row["file_uid_b"]
        if left in allowed and right in allowed:
            score = float(row["similarity"])
            by_uid[left].append((score, right))
            by_uid[right].append((score, left))
    pairs: set[tuple[str, str]] = set()
    for uid, values in by_uid.items():
        for _score, other in sorted(values, reverse=True)[:top_k]:
            pairs.add(tuple(sorted((uid, other))))
    return sorted(pairs)


def _components(
    members: list[str],
    results: dict[tuple[str, str], PairResult],
    minimum_score: float | None = None,
) -> list[list[str]]:
    positions = {uid: idx for idx, uid in enumerate(members)}
    uf = UnionFind(len(members))
    for (left, right), result in results.items():
        if left not in positions or right not in positions:
            continue
        if minimum_score is None:
            # Sama dengan build_components() pada image_utils validator lama.
            accepted = result.structural_evidence and (
                result.pair_score >= _COMPONENT_EDGE_SCORE
                or (
                    result.center_emb_sim >= _COMPONENT_EDGE_ALT_CENTER
                    and result.edgevec_sim >= _COMPONENT_EDGE_ALT_EDGEVEC
                    and not result.color_texture_trap
                )
            )
        else:
            accepted = result.pair_score >= minimum_score
        if accepted:
            uf.union(positions[left], positions[right])
    return [[members[idx] for idx in component] for component in uf.components(2)]


def _mean_or_zero(values: Iterable[float]) -> float:
    materialized = list(values)
    return float(np.mean(materialized)) if materialized else 0.0


def _result_for(
    left: str,
    right: str,
    results: dict[tuple[str, str], PairResult],
) -> PairResult | None:
    return results.get(tuple(sorted((left, right))))


def _strong_neighbors(
    uid: str,
    component: list[str],
    results: dict[tuple[str, str], PairResult],
) -> list[str]:
    return [
        other
        for other in component
        if other != uid
        and (result := _result_for(uid, other, results)) is not None
        and result.same_object
    ]


def _choose_medoid(
    component: list[str],
    results: dict[tuple[str, str], PairResult],
    preferred: str | None = None,
) -> tuple[str, dict[str, float]]:
    validity: dict[str, float] = {}
    if preferred in component and _strong_neighbors(preferred, component, results):
        chosen = preferred
    else:
        chosen = component[0]
        chosen_key: tuple[int, float, float, float] | None = None
        for uid in component:
            pair_results = [
                result
                for other in component
                if other != uid
                and (result := _result_for(uid, other, results)) is not None
            ]
            key = (
                len(_strong_neighbors(uid, component, results)),
                _mean_or_zero(result.pair_score for result in pair_results),
                _mean_or_zero(result.center_emb_sim for result in pair_results),
                _mean_or_zero(result.edgevec_sim for result in pair_results),
            )
            if chosen_key is None or key > chosen_key or (key == chosen_key and uid < chosen):
                chosen = uid
                chosen_key = key

    for uid in component:
        validity[uid] = _mean_or_zero(
            result.pair_score
            for other in component
            if other != uid
            and (result := _result_for(uid, other, results)) is not None
        )
    return chosen, validity


def _filter_component_members(
    component: list[str],
    results: dict[tuple[str, str], PairResult],
    preferred: str | None,
) -> list[str]:
    if len(component) == 2:
        return list(component)

    representative, _validity = _choose_medoid(component, results, preferred)
    kept = [representative]
    for uid in component:
        if uid == representative:
            continue
        rep_result = _result_for(uid, representative, results)
        rep_score = rep_result.pair_score if rep_result is not None else 0.0
        pair_results = [
            result
            for other in component
            if other != uid
            and (result := _result_for(uid, other, results)) is not None
        ]
        scores = sorted((result.pair_score for result in pair_results), reverse=True)
        top2_mean = _mean_or_zero(scores[:2])
        top3_mean = _mean_or_zero(scores[:3])
        neighbor_count = sum(result.same_object for result in pair_results)
        strong_count = sum(result.pair_score >= _STRONG_EDGE_SCORE for result in pair_results)
        structural_count = sum(result.structural_evidence for result in pair_results)
        if (
            structural_count >= 1
            and (
                rep_score >= _MIN_REP_PAIR_SCORE
                or top2_mean >= _MIN_MEMBER_TOP2_MEAN
                or top3_mean >= (_MIN_MEMBER_TOP2_MEAN - 0.02)
                or neighbor_count >= (_MIN_NEIGHBOR_COUNT + 1)
                or strong_count >= 2
            )
        ):
            kept.append(uid)
    return list(dict.fromkeys(kept))


def _component_mean_score(
    component: list[str],
    results: dict[tuple[str, str], PairResult],
) -> float:
    return _mean_or_zero(
        result.pair_score
        for left, right in itertools.combinations(component, 2)
        if (result := _result_for(left, right, results)) is not None
    )


@torch.inference_mode()
def _exact_embedding_similarity_matrix(
    descriptors: dict[str, Descriptor],
    members: list[str],
    runtime: ValidationRuntime,
) -> np.ndarray:
    """Return exact min(full, center) cosine similarity for every member pair.

    MATCH may be bounded KNN and legacy VALIDATE may compare sparse multimodal
    edges for large candidates. This matrix is intentionally dense but small:
    accepted candidates are capped at 500 members, so 500x500 float32 is safe
    and fast on the configured numeric device.
    """
    if not members:
        return np.empty((0, 0), dtype=np.float32)
    full = torch.from_numpy(
        np.stack([descriptors[uid].full for uid in members]).astype(
            np.float32, copy=False
        )
    ).to(runtime.numeric_device)
    center = torch.from_numpy(
        np.stack([descriptors[uid].center for uid in members]).astype(
            np.float32, copy=False
        )
    ).to(runtime.numeric_device)
    full_similarity = full @ full.T
    center_similarity = center @ center.T
    solid_similarity = torch.minimum(full_similarity, center_similarity) * 100.0
    solid_similarity = torch.clamp(solid_similarity, -100.0, 100.0)
    solid_similarity.fill_diagonal_(100.0)
    return solid_similarity.cpu().numpy().astype(np.float32, copy=False)


def _embedding_quality(
    component: list[str],
    positions: dict[str, int],
    similarity_matrix: np.ndarray,
) -> GroupEmbeddingQuality:
    if len(component) < 2:
        return GroupEmbeddingQuality(0.0, 0.0, {uid: 0.0 for uid in component})
    indices = [positions[uid] for uid in component]
    submatrix = similarity_matrix[np.ix_(indices, indices)].astype(
        np.float64, copy=False
    )
    pair_count_per_member = len(component) - 1
    member_values = (
        submatrix.sum(axis=1) - np.diag(submatrix)
    ) / pair_count_per_member
    member_means = {
        uid: float(value) for uid, value in zip(component, member_values)
    }
    # Mean ordered off-diagonal sama dengan mean pasangan unik karena matrix
    # simetris, tetapi lebih murah daripada membentuk list combinations.
    mean_similarity = float(member_values.mean())
    return GroupEmbeddingQuality(
        mean_similarity=mean_similarity,
        min_member_mean_similarity=float(member_values.min()),
        member_means=member_means,
    )


def _similarity_components(
    component: list[str],
    positions: dict[str, int],
    similarity_matrix: np.ndarray,
    threshold: float,
) -> list[list[str]]:
    union = UnionFind(len(component))
    for left_index in range(len(component)):
        left = component[left_index]
        for right_index in range(left_index + 1, len(component)):
            right = component[right_index]
            if (
                float(similarity_matrix[positions[left], positions[right]])
                >= threshold
            ):
                union.union(left_index, right_index)
    return [
        [component[index] for index in indices]
        for indices in union.components(2)
    ]


def _solidify_components(
    components: list[list[str]],
    positions: dict[str, int],
    similarity_matrix: np.ndarray,
    group_mean_threshold: float,
    member_mean_threshold: float,
) -> tuple[
    list[list[str]],
    set[str],
    dict[tuple[str, ...], GroupEmbeddingQuality],
]:
    """Split/prune legacy components until exact all-pair quality is solid.

    A transitive KNN chain can connect A~B and B~C even when A is unrelated to
    C. We first try progressively stronger similarity graph cuts so real
    subgroups survive. If a chain still cannot be split, the weakest member is
    removed and the remainder is evaluated again.
    """
    pending = [sorted(dict.fromkeys(component)) for component in components]
    accepted: list[list[str]] = []
    rejected: set[str] = set()
    quality_by_component: dict[tuple[str, ...], GroupEmbeddingQuality] = {}
    split_base = min(group_mean_threshold, member_mean_threshold)
    split_thresholds = sorted(
        {
            min(99.0, split_base + offset)
            for offset in (0.0, 2.0, 4.0, 6.0, 8.0, 9.0)
        }
    )

    while pending:
        component = pending.pop(0)
        if len(component) < 2:
            rejected.update(component)
            continue
        quality = _embedding_quality(component, positions, similarity_matrix)
        if (
            quality.mean_similarity >= group_mean_threshold
            and quality.min_member_mean_similarity >= member_mean_threshold
        ):
            accepted.append(component)
            quality_by_component[tuple(component)] = quality
            continue

        split_parts: list[list[str]] | None = None
        split_singletons: set[str] = set()
        for threshold in split_thresholds:
            parts = _similarity_components(
                component, positions, similarity_matrix, threshold
            )
            covered = {uid for part in parts for uid in part}
            if len(parts) > 1 or (parts and len(covered) < len(component)):
                split_parts = [sorted(part) for part in parts]
                split_singletons = set(component) - covered
                break
        if split_parts is not None:
            pending.extend(split_parts)
            rejected.update(split_singletons)
            continue

        weakest = min(
            component,
            key=lambda uid: (quality.member_means.get(uid, 0.0), uid),
        )
        rejected.add(weakest)
        remainder = [uid for uid in component if uid != weakest]
        if len(remainder) >= 2:
            pending.append(remainder)
        else:
            rejected.update(remainder)

    accepted.sort(key=lambda values: (-len(values), values[0]))
    return accepted, rejected, quality_by_component


def _representative_star_guard(
    components: list[list[str]],
    descriptors: dict[str, Descriptor],
    results: dict[tuple[str, str], PairResult],
    positions: dict[str, int],
    similarity_matrix: np.ndarray,
    settings: Settings,
    runtime: ValidationRuntime,
    executor: ThreadPoolExecutor,
) -> tuple[
    list[list[str]],
    set[str],
    dict[tuple[str, ...], GroupEmbeddingQuality],
    dict[tuple[str, ...], str],
    int,
    bool,
]:
    """Require every final member to match one physical representative.

    ResNet similarity is useful for candidate retrieval, but it can connect two
    different piles of fruit because the scene is semantically similar.  The
    final reused group therefore uses a star contract: every member must match
    the embedding medoid directly at reused-level similarity and must also have
    near-exact/hash or edge+ORB structural evidence.  This rule is identical
    for every source group.
    """
    accepted: list[list[str]] = []
    rejected: set[str] = set()
    quality_by_component: dict[tuple[str, ...], GroupEmbeddingQuality] = {}
    representatives: dict[tuple[str, ...], str] = {}
    compared_pairs = 0
    used_gpu_any = False

    for component in components:
        component = sorted(dict.fromkeys(component))
        if len(component) < 2:
            rejected.update(component)
            continue
        initial_quality = _embedding_quality(component, positions, similarity_matrix)
        representative = max(
            component,
            key=lambda uid: (initial_quality.member_means.get(uid, 0.0), uid),
        )
        star_pairs = [
            tuple(sorted((representative, uid)))
            for uid in component
            if uid != representative
        ]
        missing_pairs = [pair for pair in star_pairs if pair not in results]
        if missing_pairs:
            new_results, used_gpu = _compare_pairs(
                descriptors,
                missing_pairs,
                settings,
                runtime,
                executor,
            )
            results.update(new_results)
            compared_pairs += len(missing_pairs)
            used_gpu_any = used_gpu_any or used_gpu

        kept = [representative]
        for uid in component:
            if uid == representative:
                continue
            result = _result_for(representative, uid, results)
            if (
                result is not None
                and result.same_object
                and result.structural_evidence
                and min(result.full_emb_sim, result.center_emb_sim)
                >= settings.validation_representative_similarity
            ):
                kept.append(uid)
            else:
                rejected.add(uid)

        kept = sorted(dict.fromkeys(kept))
        if len(kept) < 2:
            rejected.update(kept)
            continue
        quality = _embedding_quality(kept, positions, similarity_matrix)
        if (
            quality.mean_similarity < settings.validation_group_mean_similarity
            or quality.min_member_mean_similarity
            < settings.validation_member_mean_similarity
        ):
            rejected.update(kept)
            continue
        key = tuple(kept)
        accepted.append(kept)
        quality_by_component[key] = quality
        representatives[key] = representative

    accepted.sort(key=lambda values: (-len(values), values[0]))
    return (
        accepted,
        rejected,
        quality_by_component,
        representatives,
        compared_pairs,
        used_gpu_any,
    )


def _validated_components_legacy(
    members: list[str],
    results: dict[tuple[str, str], PairResult],
    representative: str | None,
    candidate_tier: str,
    strict_validation: bool = True,
) -> list[list[str]]:
    if len(members) < 2:
        return []
    if len(members) == 2:
        result = _result_for(members[0], members[1], results)
        if result is None:
            return []
        keep_pair = (
            result.structural_evidence
            and result.same_object
            and
            result.pair_score >= _PAIR_SCORE_GROUP2_MIN
            and result.center_emb_sim >= _CENTER_SIM_GROUP2_MIN
            and (
                result.edgevec_sim >= _EDGE_VEC_GROUP2_MIN
                or result.inliers >= _INLIERS_GROUP2_MIN
            )
            and not (result.color_texture_trap and result.pair_score < 0.74)
        )
        return [list(members)] if keep_pair else []

    minimum_mean = (
        _MIN_COMPONENT_MEAN_REVIEW
        if candidate_tier == "REVIEW"
        else _MIN_COMPONENT_MEAN_CONFIRMED
    )
    valid_components: list[list[str]] = []
    for component in _components(members, results):
        preferred = representative if representative in component else None
        filtered = _filter_component_members(component, results, preferred)
        if len(filtered) < 2:
            continue
        mean_score = _component_mean_score(filtered, results)
        strong_edges = sum(
            1
            for left, right in itertools.combinations(filtered, 2)
            if (result := _result_for(left, right, results)) is not None
            and result.pair_score >= _STRONG_EDGE_SCORE
        )
        # Hotfix strict: satu strong edge tidak boleh melegalkan seluruh
        # component dengan mean rendah (contoh 0.42). Strong edge tetap
        # dipakai memilih/memberi evidence, tetapi quality gate group adalah
        # mean pair score sesuai threshold confirmed/review.
        quality_ok = (
            mean_score >= minimum_mean
            if strict_validation
            else mean_score >= minimum_mean or strong_edges >= 1
        )
        if quality_ok:
            valid_components.append(filtered)
    return valid_components


def _enforce_size_limit(
    components: list[list[str]],
    results: dict[tuple[str, str], PairResult],
    max_size: int,
) -> tuple[list[list[str]], list[list[str]]]:
    accepted: list[list[str]] = []
    quarantine: list[list[str]] = []
    for component in components:
        if len(component) <= max_size:
            accepted.append(component)
            continue
        split_done = False
        for threshold in (0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90):
            split = _components(component, results, threshold)
            if split and max(map(len, split)) <= max_size:
                accepted.extend(split)
                split_done = True
                break
        if not split_done:
            quarantine.append(component)
    return accepted, quarantine


def _representative(
    component: list[str],
    results: dict[tuple[str, str], PairResult],
    preferred: str | None = None,
) -> tuple[str, dict[str, float]]:
    return _choose_medoid(component, results, preferred)


def _candidate_batches(
    candidates: list[dict[str, Any]],
    max_members: int,
    max_candidates: int,
) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    member_count = 0
    for candidate in candidates:
        size = max(1, int(candidate["usage_count"]))
        if batch and (member_count + size > max_members or len(batch) >= max_candidates):
            yield batch
            batch = []
            member_count = 0
        batch.append(candidate)
        member_count += size
        if member_count >= max_members or len(batch) >= max_candidates:
            yield batch
            batch = []
            member_count = 0
    if batch:
        yield batch


def _image_uid_join_condition(conn, settings: Settings) -> str:
    rows = fetch_all_dict(
        conn,
        """
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s AND column_name='file_uid'
        LIMIT 1
        """,
        (settings.silver_schema, settings.image_table),
    )
    data_type = str(rows[0]["data_type"]).lower() if rows else ""
    if data_type == "uuid":
        return "i.file_uid=m.file_uid::uuid"
    if data_type in {"text", "character varying", "character"}:
        return "i.file_uid=m.file_uid"
    # Fallback untuk view/custom type. Source normal memakai uuid/text sehingga
    # cabang utama tetap memakai index (source_group, is_transport, file_uid).
    return "i.file_uid::text=m.file_uid"


def _validate_source_unbatched(settings: Settings, run_id: uuid.UUID, source_group: str) -> dict[str, Any]:
    """Compatibility reference for pre-1.3.10 runs; CLI never calls this path."""
    update_run(settings, run_id, stage="VALIDATE")
    with connection(settings) as conn:
        candidates = fetch_all_dict(
            conn,
            """
            SELECT candidate_group_id, representative_file_uid, usage_count,
                   requires_oversize_validation
            FROM processing.candidate_group
            WHERE run_id=%s
            ORDER BY usage_count DESC, candidate_group_id
            """,
            (run_id,),
        )
        progress_rows = fetch_all_dict(
            conn,
            "SELECT candidate_group_id FROM processing.validation_progress WHERE run_id=%s",
            (run_id,),
        )
        id_rows = fetch_all_dict(
            conn,
            "SELECT coalesce(max(validated_group_id), 0) AS max_id FROM processing.validated_group WHERE run_id=%s",
            (run_id,),
        )

    image_table = qname(settings.silver_schema, settings.image_table)
    completed_candidates = {int(row["candidate_group_id"]) for row in progress_rows}
    next_validated_id = int(id_rows[0]["max_id"]) + 1
    started = time.time()
    runtime = _validation_runtime(settings)
    if runtime.cpu_workers > 1:
        # Hindari oversubscription: paralelisme diatur ThreadPool validator,
        # bukan thread internal OpenCV dikali jumlah worker.
        cv2.setNumThreads(1)
    executor = ThreadPoolExecutor(
        max_workers=runtime.cpu_workers,
        thread_name_prefix="golden-validate",
    )
    gpu_candidates = 0
    gpu_pairs = 0
    cpu_numeric_candidates = 0

    for candidate in candidates:
        candidate_id = int(candidate["candidate_group_id"])
        if candidate_id in completed_candidates:
            print(f"[VALIDATE] candidate={candidate_id} skip=ALREADY_COMPLETED")
            continue
        if int(candidate["usage_count"]) > settings.max_validation_group_read:
            with connection(settings) as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processing.pipeline_exception
                        (run_id, source_group, stage, entity_type, entity_key, reason, payload)
                    VALUES (%s, %s, 'VALIDATE', 'CANDIDATE_GROUP', %s,
                            'GROUP_TOO_LARGE_TO_DECODE_SAFELY', %s::jsonb)
                    """,
                    (
                        run_id,
                        source_group.upper(),
                        str(candidate_id),
                        json.dumps({"member_count": int(candidate["usage_count"])}),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO processing.validation_progress
                        (run_id, candidate_group_id, status, accepted_group_count,
                         quarantined_group_count, removed_image_count)
                    VALUES (%s, %s, 'QUARANTINED_TOO_LARGE', 0, 1, %s)
                    ON CONFLICT (run_id, candidate_group_id) DO NOTHING
                    """,
                    (run_id, candidate_id, int(candidate["usage_count"])),
                )
            print(
                f"[VALIDATE] candidate={candidate_id} members={int(candidate['usage_count']):,} "
                "quarantine=GROUP_TOO_LARGE_TO_DECODE_SAFELY"
            )
            continue
        sparse_validation = (
            bool(candidate["requires_oversize_validation"])
            or int(candidate["usage_count"]) > settings.validation_full_pair_max_group
        )
        with connection(settings) as conn:
            member_rows = fetch_all_dict(
                conn,
                """
                SELECT m.file_uid, m.file_name, f.embedding_dim,
                       f.full_embedding, f.center_embedding,
                       coalesce(r.image_bytes, i.image_bytes) AS image_bytes
                FROM processing.candidate_group_member m
                JOIN processing.image_feature f
                  ON f.file_uid=m.file_uid AND f.model_version=%s AND f.is_valid=true
                JOIN """ + image_table + """ i ON i.file_uid::text=m.file_uid
                LEFT JOIN processing.gambar_reused r
                  ON r.source_group=upper(i.source_group::text)
                 AND r.file_uid=m.file_uid AND r.is_active=true
                LEFT JOIN processing.source_bytea_exception bx
                  ON bx.source_group=upper(i.source_group::text)
                 AND bx.file_uid=m.file_uid AND bx.is_active=true
                WHERE m.run_id=%s AND m.candidate_group_id=%s
                  AND f.center_embedding IS NOT NULL
                  AND (r.image_bytes IS NOT NULL OR i.image_bytes IS NOT NULL)
                  AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)
                ORDER BY m.file_uid
                """,
                (settings.model_version, run_id, candidate_id),
            )
            edge_rows = []
            if sparse_validation:
                edge_rows = fetch_all_dict(
                    conn,
                    """
                    SELECT e.file_uid_a, e.file_uid_b, e.similarity
                    FROM processing.candidate_edge e
                    JOIN processing.candidate_group_member a
                      ON a.run_id=e.run_id AND a.candidate_group_id=%s
                     AND a.file_uid=e.file_uid_a
                    JOIN processing.candidate_group_member b
                      ON b.run_id=e.run_id AND b.candidate_group_id=%s
                     AND b.file_uid=e.file_uid_b
                    WHERE e.run_id=%s
                    """,
                    (candidate_id, candidate_id, run_id),
                )

        descriptors, decode_errors = _decode_descriptors(
            member_rows,
            settings,
            executor,
        )

        members = sorted(descriptors)
        sparse_validation = (
            sparse_validation or len(members) > settings.validation_full_pair_max_group
        )
        pairs = _candidate_pairs(
            members,
            sparse_validation,
            edge_rows,
            settings.validation_top_k_oversize,
        )
        results, used_gpu = _compare_pairs(
            descriptors,
            pairs,
            settings,
            runtime,
            executor,
        )
        if pairs:
            if used_gpu:
                gpu_candidates += 1
                gpu_pairs += len(pairs)
            else:
                cpu_numeric_candidates += 1
        print(
            f"[VALIDATE] candidate={candidate_id} members={len(members):,} "
            f"mode={'SPARSE_EDGE' if sparse_validation else 'FULL_PAIR'} "
            f"pairs={len(pairs):,} numeric_device="
            f"{'cuda' if used_gpu else 'cpu'} cpu_workers={runtime.cpu_workers}"
        )

        components = _components(members, results)
        if len(members) == 2 and components:
            only = results.get(tuple(sorted(members)))
            if only is None or not (
                only.pair_score >= 0.66
                and (only.center_emb_sim >= 88.8 or only.edgevec_sim >= 0.68 or only.inliers >= 5)
            ):
                components = []
        accepted, quarantined = _enforce_size_limit(
            components,
            results,
            settings.max_accepted_group_size,
        )
        accepted_uids = {uid for component in accepted for uid in component}
        removed = set(members) - accepted_uids - {uid for group in quarantined for uid in group}

        group_rows = []
        group_member_rows = []
        for component in accepted:
            representative, validity = _representative(component, results)
            pair_scores = [
                result.pair_score
                for (left, right), result in results.items()
                if left in component and right in component
            ]
            signature = group_signature(component)
            group_rows.append(
                (
                    run_id,
                    next_validated_id,
                    source_group.upper(),
                    signature,
                    representative,
                    len(component),
                    candidate_id,
                    (
                        "VALIDATED_SPLIT"
                        if len(accepted) > 1
                        else "VALIDATED_SPARSE"
                        if sparse_validation
                        else "VALIDATED"
                    ),
                    float(np.mean(pair_scores)) if pair_scores else None,
                )
            )
            for uid in component:
                group_member_rows.append(
                    (
                        run_id,
                        next_validated_id,
                        uid,
                        descriptors[uid].file_name,
                        uid == representative,
                        validity.get(uid),
                    )
                )
            next_validated_id += 1

        audit_rows = []
        for (left, right), result in results.items():
            audit_rows.append(
                (
                    run_id,
                    candidate_id,
                    left,
                    right,
                    result.full_emb_sim,
                    result.center_emb_sim,
                    result.phash_dist,
                    result.edgehash_dist,
                    result.edgevec_sim,
                    result.hsvhist_sim,
                    result.raw_matches,
                    result.inliers,
                    result.inlier_ratio,
                    result.pair_score,
                    result.support_count,
                    result.color_texture_trap,
                    result.same_object,
                    result.raw_pair_score,
                    result.low_light_pair,
                    result.low_light_guard_pass,
                    result.left_luma_mean,
                    result.right_luma_mean,
                    result.left_dark_fraction,
                    result.right_dark_fraction,
                )
            )

        exception_rows = []
        for group in quarantined:
            exception_rows.append(
                (
                    run_id,
                    source_group.upper(),
                    "VALIDATE",
                    "CANDIDATE_GROUP",
                    str(candidate_id),
                    "GROUP_STILL_OVER_500_AFTER_VALIDATION",
                    '{"member_count": %d}' % len(group),
                )
            )
        for uid in removed:
            exception_rows.append(
                (
                    run_id,
                    source_group.upper(),
                    "VALIDATE",
                    "IMAGE",
                    uid,
                    "NO_VALID_COMPONENT",
                    "{}",
                )
            )
        for uid, error in decode_errors:
            exception_rows.append(
                (
                    run_id,
                    source_group.upper(),
                    "VALIDATE",
                    "IMAGE",
                    uid,
                    "BYTEA_DECODE_FAILED",
                    json.dumps({"error": error}, ensure_ascii=False),
                )
            )

        with connection(settings) as conn, conn.cursor() as cur:
            if group_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO processing.validated_group
                        (run_id, validated_group_id, source_group, group_signature,
                         representative_file_uid, usage_count, source_candidate_group_id,
                         validation_status, mean_pair_score)
                    VALUES %s ON CONFLICT (run_id, validated_group_id) DO NOTHING
                    """,
                    group_rows,
                )
                execute_values(
                    cur,
                    """
                    INSERT INTO processing.validated_group_member
                        (run_id, validated_group_id, file_uid, file_name,
                         is_representative, member_validity)
                    VALUES %s ON CONFLICT (run_id, validated_group_id, file_uid) DO NOTHING
                    """,
                    group_member_rows,
                    page_size=2000,
                )
            if audit_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO processing.validation_pair_audit
                        (run_id, candidate_group_id, file_uid_a, file_uid_b,
                         full_emb_sim, center_emb_sim, phash_dist, edgehash_dist,
                         edgevec_sim, hsvhist_sim, raw_matches, inliers, inlier_ratio,
                         pair_score, support_count, color_texture_trap, same_object,
                         raw_pair_score, low_light_pair, low_light_guard_pass,
                         left_luma_mean, right_luma_mean,
                         left_dark_fraction, right_dark_fraction)
                    VALUES %s
                    ON CONFLICT (run_id, candidate_group_id, file_uid_a, file_uid_b) DO NOTHING
                    """,
                    audit_rows,
                    page_size=2000,
                )
            if exception_rows:
                execute_values(
                    cur,
                    """
                    INSERT INTO processing.pipeline_exception
                        (run_id, source_group, stage, entity_type, entity_key, reason, payload)
                    VALUES %s
                    """,
                    exception_rows,
                    template="(%s,%s,%s,%s,%s,%s,%s::jsonb)",
                    page_size=1000,
                )
            cur.execute(
                """
                INSERT INTO processing.validation_progress
                    (run_id, candidate_group_id, status, accepted_group_count,
                     quarantined_group_count, removed_image_count)
                VALUES (%s, %s, 'COMPLETED', %s, %s, %s)
                ON CONFLICT (run_id, candidate_group_id) DO NOTHING
                """,
                (
                    run_id,
                    candidate_id,
                    len(group_rows),
                    len(quarantined),
                    len(removed) + len(decode_errors),
                ),
            )
        print(
            f"[VALIDATE] candidate={candidate_id} members={len(members):,} "
            f"accepted={len(group_rows)} quarantine={len(quarantined)} removed={len(removed)}"
        )

    executor.shutdown(wait=True, cancel_futures=False)
    with connection(settings) as conn:
        totals = fetch_all_dict(
            conn,
            """
            SELECT
                (SELECT count(*) FROM processing.validated_group WHERE run_id=%s) AS validated_groups,
                coalesce(sum(quarantined_group_count), 0) AS quarantined_groups,
                coalesce(sum(removed_image_count), 0) AS removed_images,
                (SELECT count(*) FROM processing.validation_pair_audit WHERE run_id=%s) AS validation_pairs,
                count(*) AS completed_candidates
            FROM processing.validation_progress
            WHERE run_id=%s
            """,
            (run_id, run_id, run_id),
        )[0]
    metrics = {
        "validated_groups": int(totals["validated_groups"]),
        "quarantined_groups": int(totals["quarantined_groups"]),
        "validation_removed_images": int(totals["removed_images"]),
        "validation_pairs": int(totals["validation_pairs"]),
        "validation_completed_candidates": int(totals["completed_candidates"]),
        "validation_numeric_device": runtime.numeric_device.type,
        "validation_device_name": runtime.device_name,
        "validation_cpu_workers": runtime.cpu_workers,
        "validation_gpu_candidates": gpu_candidates,
        "validation_gpu_pairs": gpu_pairs,
        "validation_cpu_numeric_candidates": cpu_numeric_candidates,
        "validation_seconds_this_call": int(time.time() - started),
    }
    update_run(settings, run_id, stage="VALIDATE", metrics=metrics)
    return metrics


def validate_source(
    settings: Settings,
    run_id: uuid.UUID,
    source_group: str,
    nas_roots: Iterable[str | Path] | None = None,
) -> dict[str, Any]:
    """Apply universal structural, representative-star and group quality checks."""
    update_run(settings, run_id, stage="VALIDATE")
    with connection(settings) as conn:
        candidates = fetch_all_dict(
            conn,
            """
            SELECT candidate_group_id, representative_file_uid, usage_count,
                   max_edge_similarity, requires_oversize_validation
            FROM processing.candidate_group
            WHERE run_id=%s
            ORDER BY usage_count DESC, candidate_group_id
            """,
            (run_id,),
        )
        progress_rows = fetch_all_dict(
            conn,
            "SELECT candidate_group_id FROM processing.validation_progress WHERE run_id=%s",
            (run_id,),
        )
        id_rows = fetch_all_dict(
            conn,
            "SELECT coalesce(max(validated_group_id), 0) AS max_id "
            "FROM processing.validated_group WHERE run_id=%s",
            (run_id,),
        )
        claimed_rows = fetch_all_dict(
            conn,
            """
            SELECT DISTINCT file_uid, upper(btrim(file_name)) AS logical_name
            FROM processing.validated_group_member
            WHERE run_id=%s
            """,
            (run_id,),
        )

    image_table = qname(settings.silver_schema, settings.image_table)
    completed_candidates = {int(row["candidate_group_id"]) for row in progress_rows}
    pending_candidates = [
        candidate
        for candidate in candidates
        if int(candidate["candidate_group_id"]) not in completed_candidates
    ]
    too_large = [
        candidate
        for candidate in pending_candidates
        if int(candidate["usage_count"]) > settings.max_validation_group_read
    ]
    processable = [
        candidate
        for candidate in pending_candidates
        if int(candidate["usage_count"]) <= settings.max_validation_group_read
    ]
    batches = list(
        _candidate_batches(
            processable,
            settings.validation_db_member_batch,
            settings.validation_db_candidate_batch,
        )
    )
    pending_members = sum(int(candidate["usage_count"]) for candidate in processable)
    nas_reader: NasCandidateReader | None = None
    normalized_nas_roots = [Path(value) for value in (nas_roots or [])]
    if normalized_nas_roots and processable:
        processable_ids = [int(candidate["candidate_group_id"]) for candidate in processable]
        with connection(settings) as conn:
            nas_name_rows = fetch_all_dict(
                conn,
                """
                SELECT DISTINCT m.file_name
                FROM processing.candidate_group_member m
                WHERE m.run_id=%s
                  AND m.candidate_group_id=ANY(%s)
                """,
                (run_id, processable_ids),
            )
        nas_reader = NasCandidateReader(
            normalized_nas_roots,
            (str(row.get("file_name") or "") for row in nas_name_rows),
        )
        nas_reader.require_complete()
    print(
        f"[VALIDATE] plan total_candidates={len(candidates):,} "
        f"completed={len(completed_candidates):,} pending={len(pending_candidates):,} "
        f"pending_members={pending_members:,} db_batches={len(batches):,} "
        f"payload_mode={'NAS_CANDIDATE_STREAM' if nas_reader is not None else 'DB_BYTEA'} "
        f"source_read_order={settings.validation_source_read_order} "
        f"legacy_rules=ON strict_mean={'ON' if settings.strict_group_validation else 'OFF'} "
        f"exact_group_mean={settings.validation_group_mean_similarity:.1f} "
        f"exact_member_mean={settings.validation_member_mean_similarity:.1f} "
        f"representative_similarity={settings.validation_representative_similarity:.1f} "
        f"low_light_guard={'ON' if settings.low_light_guard else 'OFF'} "
        f"thresholds={settings.review_threshold:.1f}/{settings.reused_threshold:.1f}",
        flush=True,
    )

    next_validated_id = int(id_rows[0]["max_id"]) + 1
    started = time.time()
    runtime = _validation_runtime(settings)
    if runtime.cpu_workers > 1:
        cv2.setNumThreads(1)
    executor = ThreadPoolExecutor(
        max_workers=runtime.cpu_workers,
        thread_name_prefix="golden-validate",
    )
    nas_executor = (
        ThreadPoolExecutor(
            max_workers=settings.validation_nas_io_workers,
            thread_name_prefix="golden-validate-nas",
        )
        if nas_reader is not None
        else None
    )
    batch_durations: list[float] = []
    gpu_candidates = 0
    gpu_pairs = 0
    cpu_numeric_candidates = 0
    examined_pairs = 0
    low_light_pairs = 0
    low_light_rejected_pairs = 0
    exact_embedding_pairs = 0
    similarity_rejected_images = 0
    representative_star_pairs = 0
    representative_star_rejected_images = 0
    source_group_upper = source_group.upper()
    completed_this_call = 0
    # Kontrak final: satu file_uid hanya boleh berada dalam satu validated group
    # pada run yang sama, termasuk ketika VALIDATE di-resume.
    claimed_uids = {str(row["file_uid"]) for row in claimed_rows}
    claimed_names = {
        str(row.get("logical_name") or "").strip().upper()
        for row in claimed_rows
        if str(row.get("logical_name") or "").strip()
    }

    try:
        # Satu read connection dan satu write connection untuk seluruh stage.
        # Versi lama membuka >42 ribu koneksi pada 21.100 candidate DIN.
        with (
            connection(settings, autocommit=True) as read_conn,
            connection(settings) as write_conn,
            write_conn.cursor() as write_cur,
        ):
            uid_join_condition = (
                None
                if nas_reader is not None
                else _image_uid_join_condition(read_conn, settings)
            )
            validation_member_order = (
                "f.ctid, m.file_uid"
                if nas_reader is not None
                and settings.validation_source_read_order == "physical"
                else "coalesce(r.ctid, i.ctid)"
                if settings.validation_source_read_order == "physical"
                else "m.candidate_group_id, m.file_uid"
            )
            for candidate in too_large:
                candidate_id = int(candidate["candidate_group_id"])
                member_count = int(candidate["usage_count"])
                write_cur.execute(
                    """
                    INSERT INTO processing.pipeline_exception
                        (run_id, source_group, stage, entity_type, entity_key, reason, payload)
                    VALUES (%s, %s, 'VALIDATE', 'CANDIDATE_GROUP', %s,
                            'GROUP_TOO_LARGE_TO_DECODE_SAFELY', %s::jsonb)
                    """,
                    (
                        run_id,
                        source_group_upper,
                        str(candidate_id),
                        json.dumps({"member_count": member_count}),
                    ),
                )
                write_cur.execute(
                    """
                    INSERT INTO processing.validation_progress
                        (run_id, candidate_group_id, status, accepted_group_count,
                         quarantined_group_count, removed_image_count)
                    VALUES (%s, %s, 'QUARANTINED_TOO_LARGE', 0, 1, %s)
                    ON CONFLICT (run_id, candidate_group_id) DO NOTHING
                    """,
                    (run_id, candidate_id, member_count),
                )
                print(
                    f"[VALIDATE] candidate={candidate_id} members={member_count:,} "
                    "quarantine=GROUP_TOO_LARGE_TO_DECODE_SAFELY",
                    flush=True,
                )
                completed_this_call += 1
            if too_large:
                write_conn.commit()

            for batch_no, batch in enumerate(batches, start=1):
                batch_started = time.time()
                candidate_ids = [int(candidate["candidate_group_id"]) for candidate in batch]
                expected_members = sum(int(candidate["usage_count"]) for candidate in batch)
                print(
                    f"[VALIDATE] preload batch={batch_no:,}/{len(batches):,} "
                    f"candidates={len(batch):,} expected_members={expected_members:,} status=FETCH_DB",
                    flush=True,
                )
                db_fetch_started = time.time()
                if nas_reader is not None:
                    member_rows = fetch_all_dict(
                        read_conn,
                        f"""
                        SELECT m.candidate_group_id, m.file_uid, m.file_name,
                               f.file_sha256, f.embedding_dim,
                               f.full_embedding, f.center_embedding,
                               NULL::bytea AS image_bytes
                        FROM processing.candidate_group_member m
                        JOIN processing.image_feature f
                          ON f.file_uid=m.file_uid
                         AND f.model_version=%s
                         AND f.is_valid=true
                        WHERE m.run_id=%s
                          AND m.candidate_group_id=ANY(%s)
                          AND f.center_embedding IS NOT NULL
                        ORDER BY {validation_member_order}
                        """,
                        (
                            settings.model_version,
                            run_id,
                            candidate_ids,
                        ),
                    )
                    db_fetch_seconds = time.time() - db_fetch_started
                    nas_fetch_started = time.time()
                    member_rows = nas_reader.attach_payloads(
                        member_rows,
                        nas_executor or executor,
                    )
                    nas_fetch_seconds = time.time() - nas_fetch_started
                else:
                    member_rows = fetch_all_dict(
                        read_conn,
                        f"""
                    SELECT m.candidate_group_id, m.file_uid, m.file_name,
                           f.file_sha256, f.embedding_dim,
                           f.full_embedding, f.center_embedding,
                           coalesce(r.image_bytes, i.image_bytes) AS image_bytes
                    FROM processing.candidate_group_member m
                    JOIN processing.image_feature f
                      ON f.file_uid=m.file_uid AND f.model_version=%s AND f.is_valid=true
                    JOIN {image_table} i
                      ON {uid_join_condition}
                     AND i.source_group=%s
                     AND i.is_transport=false
                    LEFT JOIN processing.gambar_reused r
                      ON r.source_group=%s
                     AND r.file_uid=m.file_uid
                     AND r.is_active=true
                    LEFT JOIN processing.source_bytea_exception bx
                      ON bx.source_group=%s
                     AND bx.file_uid=m.file_uid
                     AND bx.is_active=true
                    WHERE m.run_id=%s
                      AND m.candidate_group_id=ANY(%s)
                      AND f.center_embedding IS NOT NULL
                      AND (r.image_bytes IS NOT NULL OR i.image_bytes IS NOT NULL)
                      AND (bx.file_uid IS NULL OR r.file_uid IS NOT NULL)
                    ORDER BY {validation_member_order}
                        """,
                        (
                            settings.model_version,
                            source_group_upper,
                            source_group_upper,
                            source_group_upper,
                            run_id,
                            candidate_ids,
                        ),
                    )
                    db_fetch_seconds = time.time() - db_fetch_started
                    nas_fetch_seconds = 0.0
                edge_fetch_started = time.time()
                need_edges = any(
                    bool(candidate["requires_oversize_validation"])
                    or int(candidate["usage_count"]) > settings.validation_full_pair_max_group
                    for candidate in batch
                )
                edge_rows = []
                if need_edges:
                    # Semua endpoint candidate batch sudah tersedia di
                    # member_rows. Ambil edge lewat PK (run_id, file_uid_a)
                    # lalu petakan candidate_group_id di memori. Query lama
                    # melakukan join candidate_edge -> member a -> member b;
                    # pada source besar planner dapat men-scan jutaan edge
                    # untuk batch yang sebenarnya hanya berisi 1.000 member.
                    batch_uid_to_candidate = {
                        str(row["file_uid"]): int(row["candidate_group_id"])
                        for row in member_rows
                    }
                    raw_edge_rows = fetch_all_dict(
                        read_conn,
                        """
                        SELECT e.file_uid_a, e.file_uid_b, e.similarity, e.tier
                        FROM processing.candidate_edge e
                        WHERE e.run_id=%s
                          AND e.file_uid_a=ANY(%s)
                        ORDER BY e.file_uid_a, e.similarity DESC
                        """,
                        (run_id, list(batch_uid_to_candidate)),
                    )
                    for row in raw_edge_rows:
                        left_group = batch_uid_to_candidate.get(
                            str(row["file_uid_a"])
                        )
                        right_group = batch_uid_to_candidate.get(
                            str(row["file_uid_b"])
                        )
                        if left_group is None or left_group != right_group:
                            continue
                        edge_rows.append(
                            {"candidate_group_id": left_group, **row}
                        )
                edge_fetch_seconds = time.time() - edge_fetch_started
                members_by_candidate: dict[int, list[dict[str, Any]]] = defaultdict(list)
                edges_by_candidate: dict[int, list[dict[str, Any]]] = defaultdict(list)
                for row in member_rows:
                    members_by_candidate[int(row["candidate_group_id"])].append(row)
                for row in edge_rows:
                    edges_by_candidate[int(row["candidate_group_id"])].append(row)
                fetch_seconds = time.time() - batch_started
                print(
                    f"[VALIDATE] preload batch={batch_no:,}/{len(batches):,} "
                    f"fetched_members={len(member_rows):,} edges={len(edge_rows):,} "
                    f"elapsed={fetch_seconds:,.1f}s "
                    f"db={db_fetch_seconds:,.1f}s nas={nas_fetch_seconds:,.1f}s "
                    f"edge={edge_fetch_seconds:,.1f}s "
                    f"source_read_order={settings.validation_source_read_order} status=PROCESS",
                    flush=True,
                )

                for candidate in batch:
                    candidate_started = time.time()
                    candidate_id = int(candidate["candidate_group_id"])
                    candidate_member_rows = members_by_candidate.get(candidate_id, [])
                    decode_started = time.time()
                    descriptors, decode_errors = _decode_descriptors(
                        candidate_member_rows,
                        settings,
                        executor,
                    )
                    decode_seconds = time.time() - decode_started
                    members = sorted(descriptors)
                    sparse_validation = (
                        bool(candidate["requires_oversize_validation"])
                        or len(members) > settings.validation_full_pair_max_group
                    )
                    pairs = _candidate_pairs(
                        members,
                        sparse_validation,
                        edges_by_candidate.get(candidate_id, []),
                        settings.validation_top_k_oversize,
                    )
                    compare_started = time.time()
                    results, used_gpu = _compare_pairs(
                        descriptors,
                        pairs,
                        settings,
                        runtime,
                        executor,
                    )
                    low_light_pairs += sum(
                        1 for result in results.values() if result.low_light_pair
                    )
                    low_light_rejected_pairs += sum(
                        1
                        for result in results.values()
                        if result.low_light_pair and not result.low_light_guard_pass
                    )
                    compare_seconds = time.time() - compare_started
                    examined_pairs += len(pairs)
                    if pairs:
                        if used_gpu:
                            gpu_candidates += 1
                            gpu_pairs += len(pairs)
                        else:
                            cpu_numeric_candidates += 1

                    max_similarity = candidate.get("max_edge_similarity")
                    candidate_tier = (
                        "CONFIRMED"
                        if max_similarity is not None
                        and float(max_similarity) >= settings.reused_threshold
                        else "REVIEW"
                    )
                    components = _validated_components_legacy(
                        members,
                        results,
                        str(candidate["representative_file_uid"]),
                        candidate_tier,
                        settings.strict_group_validation,
                    )
                    accepted, quarantined = _enforce_size_limit(
                        components,
                        results,
                        settings.max_accepted_group_size,
                    )
                    duplicate_claims: set[str] = set()
                    duplicate_name_claims: set[str] = set()
                    unique_components: list[list[str]] = []
                    for component in accepted:
                        duplicate_claims.update(uid for uid in component if uid in claimed_uids)
                        remaining = []
                        local_names: set[str] = set()
                        for uid in component:
                            logical_name = descriptors[uid].file_name.strip().upper()
                            if uid in claimed_uids:
                                continue
                            if logical_name and (
                                logical_name in claimed_names or logical_name in local_names
                            ):
                                duplicate_name_claims.add(uid)
                                continue
                            remaining.append(uid)
                            if logical_name:
                                local_names.add(logical_name)
                        if len(remaining) < 2:
                            continue
                        if len(remaining) != len(component):
                            # Jangan sekadar mencabut hub yang sudah diklaim:
                            # anggota tersisa harus membentuk component valid lagi.
                            preferred_remaining = (
                                str(candidate["representative_file_uid"])
                                if str(candidate["representative_file_uid"]) in remaining
                                else None
                            )
                            unique_components.extend(
                                _validated_components_legacy(
                                    remaining,
                                    results,
                                    preferred_remaining,
                                    candidate_tier,
                                    settings.strict_group_validation,
                                )
                            )
                        else:
                            unique_components.append(remaining)
                    if settings.strict_group_validation:
                        minimum_mean = (
                            _MIN_COMPONENT_MEAN_REVIEW
                            if candidate_tier == "REVIEW"
                            else _MIN_COMPONENT_MEAN_CONFIRMED
                        )
                        # _enforce_size_limit dapat memecah component besar;
                        # setiap pecahan final wajib melewati mean gate lagi.
                        unique_components = [
                            component
                            for component in unique_components
                            if _component_mean_score(component, results) >= minimum_mean
                        ]

                    # Quality gate 0..100 yang diminta bisnis. Candidate SPARSE
                    # tetap dihitung dense untuk full+center embedding sehingga
                    # rantai A~B~C tidak lolos saat A berbeda dari C.
                    quality_by_component: dict[
                        tuple[str, ...], GroupEmbeddingQuality
                    ] = {}
                    strict_representatives: dict[tuple[str, ...], str] = {}
                    similarity_rejected_uids: set[str] = set()
                    quality_members = sorted(
                        {uid for component in unique_components for uid in component}
                    )
                    if len(quality_members) >= 2:
                        similarity_matrix = _exact_embedding_similarity_matrix(
                            descriptors,
                            quality_members,
                            runtime,
                        )
                        exact_embedding_pairs += (
                            len(quality_members) * (len(quality_members) - 1) // 2
                        )
                        quality_positions = {
                            uid: index for index, uid in enumerate(quality_members)
                        }
                        (
                            unique_components,
                            similarity_rejected_uids,
                            quality_by_component,
                        ) = _solidify_components(
                            unique_components,
                            quality_positions,
                            similarity_matrix,
                            settings.validation_group_mean_similarity,
                            settings.validation_member_mean_similarity,
                        )
                        (
                            unique_components,
                            star_rejected_uids,
                            quality_by_component,
                            strict_representatives,
                            star_pairs_compared,
                            star_used_gpu,
                        ) = _representative_star_guard(
                            unique_components,
                            descriptors,
                            results,
                            quality_positions,
                            similarity_matrix,
                            settings,
                            runtime,
                            executor,
                        )
                        similarity_rejected_uids.update(star_rejected_uids)
                        representative_star_rejected_images += len(star_rejected_uids)
                        representative_star_pairs += star_pairs_compared
                        examined_pairs += star_pairs_compared
                        if star_pairs_compared:
                            if star_used_gpu:
                                gpu_pairs += star_pairs_compared
                            else:
                                cpu_numeric_candidates += 1
                        similarity_rejected_images += len(similarity_rejected_uids)
                    accepted = unique_components
                    accepted_uids = {uid for component in accepted for uid in component}
                    quarantined_uids = {uid for component in quarantined for uid in component}
                    removed = set(members) - accepted_uids - quarantined_uids

                    group_rows = []
                    group_member_rows = []
                    for component in accepted:
                        quality = quality_by_component[tuple(sorted(component))]
                        preferred = (
                            strict_representatives.get(tuple(sorted(component)))
                        )
                        representative, validity = _representative(component, results, preferred)
                        pair_scores = [
                            result.pair_score
                            for (left, right), result in results.items()
                            if left in component and right in component
                        ]
                        signature = group_signature(component)
                        status_parts = ["VALIDATED", candidate_tier]
                        if sparse_validation:
                            status_parts.append("SPARSE")
                        if len(accepted) > 1:
                            status_parts.append("SPLIT")
                        status_parts.append("REPRESENTATIVE_STAR")
                        group_rows.append(
                            (
                                run_id,
                                next_validated_id,
                                source_group_upper,
                                signature,
                                representative,
                                len(component),
                                candidate_id,
                                "_".join(status_parts),
                                float(np.mean(pair_scores)) if pair_scores else None,
                                quality.mean_similarity,
                                quality.min_member_mean_similarity,
                            )
                        )
                        for uid in component:
                            group_member_rows.append(
                                (
                                    run_id,
                                    next_validated_id,
                                    uid,
                                    descriptors[uid].file_name,
                                    uid == representative,
                                    validity.get(uid),
                                    quality.member_means.get(uid),
                                )
                            )
                        claimed_uids.update(component)
                        claimed_names.update(
                            descriptors[uid].file_name.strip().upper()
                            for uid in component
                            if descriptors[uid].file_name.strip()
                        )
                        next_validated_id += 1

                    audit_rows = []
                    if settings.validation_max_audit_pairs_per_group > 0:
                        limited_results = itertools.islice(
                            results.items(),
                            settings.validation_max_audit_pairs_per_group,
                        )
                        for (left, right), result in limited_results:
                            audit_rows.append(
                                (
                                    run_id,
                                    candidate_id,
                                    left,
                                    right,
                                    result.full_emb_sim,
                                    result.center_emb_sim,
                                    result.phash_dist,
                                    result.edgehash_dist,
                                    result.edgevec_sim,
                                    result.hsvhist_sim,
                                    result.raw_matches,
                                    result.inliers,
                                    result.inlier_ratio,
                                    result.pair_score,
                                    result.support_count,
                                    result.color_texture_trap,
                                    result.same_object,
                                    result.raw_pair_score,
                                    result.low_light_pair,
                                    result.low_light_guard_pass,
                                    result.left_luma_mean,
                                    result.right_luma_mean,
                                    result.left_dark_fraction,
                                    result.right_dark_fraction,
                                )
                            )

                    exception_rows = []
                    for group in quarantined:
                        exception_rows.append(
                            (
                                run_id,
                                source_group_upper,
                                "VALIDATE",
                                "CANDIDATE_GROUP",
                                str(candidate_id),
                                "GROUP_STILL_OVER_500_AFTER_VALIDATION",
                                json.dumps({"member_count": len(group)}),
                            )
                        )
                    for uid in removed - duplicate_claims - duplicate_name_claims:
                        rejected_by_similarity = uid in similarity_rejected_uids
                        exception_rows.append(
                            (
                                run_id,
                                source_group_upper,
                                "VALIDATE",
                                "IMAGE",
                                uid,
                                (
                                    "GROUP_SIMILARITY_BELOW_THRESHOLD"
                                    if rejected_by_similarity
                                    else "NO_VALID_COMPONENT"
                                ),
                                (
                                    json.dumps(
                                        {
                                            "group_mean_threshold": settings.validation_group_mean_similarity,
                                            "member_mean_threshold": settings.validation_member_mean_similarity,
                                        }
                                    )
                                    if rejected_by_similarity
                                    else "{}"
                                ),
                            )
                        )
                    for uid in duplicate_claims:
                        exception_rows.append(
                            (
                                run_id,
                                source_group_upper,
                                "VALIDATE",
                                "IMAGE",
                                uid,
                                "DUPLICATE_FINAL_MEMBERSHIP",
                                "{}",
                            )
                        )
                    for uid in duplicate_name_claims:
                        exception_rows.append(
                            (
                                run_id,
                                source_group_upper,
                                "VALIDATE",
                                "IMAGE",
                                uid,
                                "DUPLICATE_LOGICAL_FILENAME_MEMBERSHIP",
                                json.dumps(
                                    {"file_name": descriptors[uid].file_name},
                                    ensure_ascii=False,
                                ),
                            )
                        )
                    for uid, error in decode_errors:
                        exception_rows.append(
                            (
                                run_id,
                                source_group_upper,
                                "VALIDATE",
                                "IMAGE",
                                uid,
                                "BYTEA_DECODE_FAILED",
                                json.dumps({"error": error}, ensure_ascii=False),
                            )
                        )

                    if group_rows:
                        execute_values(
                            write_cur,
                            """
                            INSERT INTO processing.validated_group
                                (run_id, validated_group_id, source_group, group_signature,
                                 representative_file_uid, usage_count, source_candidate_group_id,
                                 validation_status, mean_pair_score,
                                 mean_embedding_similarity, min_member_mean_similarity)
                            VALUES %s
                            ON CONFLICT (run_id, validated_group_id) DO NOTHING
                            """,
                            group_rows,
                        )
                        execute_values(
                            write_cur,
                            """
                            INSERT INTO processing.validated_group_member
                                (run_id, validated_group_id, file_uid, file_name,
                                 is_representative, member_validity,
                                 member_mean_similarity)
                            VALUES %s
                            ON CONFLICT (run_id, validated_group_id, file_uid) DO NOTHING
                            """,
                            group_member_rows,
                            page_size=2000,
                        )
                    if audit_rows:
                        execute_values(
                            write_cur,
                            """
                            INSERT INTO processing.validation_pair_audit
                                (run_id, candidate_group_id, file_uid_a, file_uid_b,
                                 full_emb_sim, center_emb_sim, phash_dist, edgehash_dist,
                                 edgevec_sim, hsvhist_sim, raw_matches, inliers, inlier_ratio,
                                 pair_score, support_count, color_texture_trap, same_object,
                                 raw_pair_score, low_light_pair, low_light_guard_pass,
                                 left_luma_mean, right_luma_mean,
                                 left_dark_fraction, right_dark_fraction)
                            VALUES %s
                            ON CONFLICT (run_id, candidate_group_id, file_uid_a, file_uid_b)
                            DO NOTHING
                            """,
                            audit_rows,
                            page_size=500,
                        )
                    if exception_rows:
                        execute_values(
                            write_cur,
                            """
                            INSERT INTO processing.pipeline_exception
                                (run_id, source_group, stage, entity_type, entity_key, reason, payload)
                            VALUES %s
                            """,
                            exception_rows,
                            template="(%s,%s,%s,%s,%s,%s,%s::jsonb)",
                            page_size=1000,
                        )
                    write_cur.execute(
                        """
                        INSERT INTO processing.validation_progress
                            (run_id, candidate_group_id, status, accepted_group_count,
                             quarantined_group_count, removed_image_count)
                        VALUES (%s, %s, 'COMPLETED', %s, %s, %s)
                        ON CONFLICT (run_id, candidate_group_id) DO NOTHING
                        """,
                        (
                            run_id,
                            candidate_id,
                            len(group_rows),
                            len(quarantined),
                            len(removed) + len(decode_errors),
                        ),
                    )
                    completed_this_call += 1
                    print(
                        f"[VALIDATE] candidate={candidate_id} tier={candidate_tier} "
                        f"members={len(members):,} mode="
                        f"{'SPARSE_EDGE' if sparse_validation else 'FULL_PAIR'} "
                        f"pairs={len(pairs):,} accepted={len(group_rows)} "
                        f"exact_pairs={len(quality_members) * (len(quality_members) - 1) // 2:,} "
                        f"similarity_removed={len(similarity_rejected_uids):,} "
                        f"removed={len(removed):,} device={'cuda' if used_gpu else 'cpu'} "
                        f"decode={decode_seconds:,.2f}s compare={compare_seconds:,.2f}s "
                        f"elapsed={time.time() - candidate_started:,.2f}s",
                        flush=True,
                    )

                write_conn.commit()
                batch_elapsed = time.time() - batch_started
                batch_durations.append(batch_elapsed)
                rolling_seconds = sum(batch_durations[-10:]) / min(
                    len(batch_durations), 10
                )
                remaining_seconds = max(0.0, len(batches) - batch_no) * rolling_seconds
                eta_hours = int(remaining_seconds // 3600)
                eta_minutes = int((remaining_seconds % 3600) // 60)
                print(
                    f"[VALIDATE] batch={batch_no:,}/{len(batches):,} committed "
                    f"completed_this_call={completed_this_call:,}/{len(pending_candidates):,} "
                    f"batch_progress={100.0 * batch_no / max(1, len(batches)):.1f}% "
                    f"elapsed={batch_elapsed:,.1f}s "
                    f"rolling_avg={rolling_seconds:,.1f}s "
                    f"eta={eta_hours:02d}h{eta_minutes:02d}m",
                    flush=True,
                )
    finally:
        executor.shutdown(wait=True, cancel_futures=False)
        if nas_executor is not None:
            nas_executor.shutdown(wait=True, cancel_futures=False)

    with connection(settings) as conn:
        totals = fetch_all_dict(
            conn,
            """
            SELECT
                (SELECT count(*) FROM processing.validated_group WHERE run_id=%s)
                    AS validated_groups,
                coalesce(sum(quarantined_group_count), 0) AS quarantined_groups,
                coalesce(sum(removed_image_count), 0) AS removed_images,
                (SELECT count(*) FROM processing.validation_pair_audit WHERE run_id=%s)
                    AS validation_pairs,
                count(*) AS completed_candidates
            FROM processing.validation_progress
            WHERE run_id=%s
            """,
            (run_id, run_id, run_id),
        )[0]
    metrics = {
        "validated_groups": int(totals["validated_groups"]),
        "quarantined_groups": int(totals["quarantined_groups"]),
        "validation_removed_images": int(totals["removed_images"]),
        "validation_pairs": int(totals["validation_pairs"]),
        "validation_pairs_examined_this_call": examined_pairs,
        "validation_low_light_pairs": low_light_pairs,
        "validation_low_light_rejected_pairs": low_light_rejected_pairs,
        "validation_low_light_guard": settings.low_light_guard,
        "validation_structural_duplicate_guard": settings.duplicate_require_structural_evidence,
        "validation_exact_group_similarity": True,
        "validation_group_mean_similarity_threshold": settings.validation_group_mean_similarity,
        "validation_member_mean_similarity_threshold": settings.validation_member_mean_similarity,
        "validation_exact_embedding_pairs": exact_embedding_pairs,
        "validation_similarity_rejected_images": similarity_rejected_images,
        "validation_representative_star_guard": True,
        "validation_representative_similarity_threshold": settings.validation_representative_similarity,
        "validation_representative_star_pairs": representative_star_pairs,
        "validation_representative_star_rejected_images": representative_star_rejected_images,
        "validation_completed_candidates": int(totals["completed_candidates"]),
        "validation_numeric_device": runtime.numeric_device.type,
        "validation_device_name": runtime.device_name,
        "validation_cpu_workers": runtime.cpu_workers,
        "validation_nas_io_workers": (
            settings.validation_nas_io_workers if nas_reader is not None else 0
        ),
        "validation_gpu_candidates": gpu_candidates,
        "validation_gpu_pairs": gpu_pairs,
        "validation_cpu_numeric_candidates": cpu_numeric_candidates,
        "validation_db_batches": len(batches),
        "validation_source_read_order": settings.validation_source_read_order,
        "validation_seconds_this_call": int(time.time() - started),
        "validation_legacy_rules": True,
        "validation_strict_group_mean": settings.strict_group_validation,
        **(
            nas_reader.metrics()
            if nas_reader is not None
            else {"validation_payload_mode": "DB_BYTEA"}
        ),
    }
    update_run(settings, run_id, stage="VALIDATE", metrics=metrics)
    return metrics
