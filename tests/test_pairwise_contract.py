from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def _load_pairwise_functions() -> dict[str, Any]:
    source = (ROOT / "src" / "golden_eye" / "validator.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    wanted = {
        "GroupEmbeddingQuality",
        "_embedding_quality",
        "_pairwise_cliques",
    }
    selected = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in wanted:
            selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            selected.append(node)
    namespace: dict[str, Any] = {
        "np": np,
        "dataclass": __import__("dataclasses").dataclass,
        "Any": Any,
    }
    module = ast.Module(body=selected, type_ignores=[])
    exec(compile(module, "validator_pairwise", "exec"), namespace)
    return namespace


PAIRWISE = _load_pairwise_functions()
_embedding_quality = PAIRWISE["_embedding_quality"]
_pairwise_cliques = PAIRWISE["_pairwise_cliques"]


def test_pairwise_cliques_break_transitive_chain() -> None:
    members = ["A", "B", "C"]
    positions = {uid: index for index, uid in enumerate(members)}
    matrix = np.array(
        [
            [100.0, 95.0, 88.0],
            [95.0, 100.0, 94.0],
            [88.0, 94.0, 100.0],
        ],
        dtype=np.float32,
    )

    cliques, rejected = _pairwise_cliques(members, positions, matrix, 93.0)

    assert max(map(len, cliques), default=0) == 2
    assert all(
        matrix[positions[left], positions[right]] >= 93.0
        for clique in cliques
        for i, left in enumerate(clique)
        for right in clique[i + 1 :]
    )
    assert len({uid for clique in cliques for uid in clique} | rejected) == 3


def test_embedding_quality_exposes_real_minimum_pair() -> None:
    members = ["A", "B", "C"]
    positions = {uid: index for index, uid in enumerate(members)}
    matrix = np.array(
        [
            [100.0, 98.0, 91.0],
            [98.0, 100.0, 97.0],
            [91.0, 97.0, 100.0],
        ],
        dtype=np.float32,
    )

    quality = _embedding_quality(members, positions, matrix)

    assert quality.min_pair_similarity == 91.0
    assert quality.mean_similarity > quality.min_pair_similarity
