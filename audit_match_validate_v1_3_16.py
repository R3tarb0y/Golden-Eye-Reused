from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)
sys.path.insert(0, str(ROOT / "src"))

from golden_eye import __version__  # noqa: E402
from golden_eye.config import Settings  # noqa: E402
from golden_eye.db import connection  # noqa: E402
from golden_eye import validator  # noqa: E402


EXPECTED_VALIDATOR = {
    "_COMPONENT_EDGE_SCORE": 0.52,
    "_COMPONENT_EDGE_ALT_CENTER": 87.5,
    "_COMPONENT_EDGE_ALT_EDGEVEC": 0.70,
    "_MIN_COMPONENT_MEAN_CONFIRMED": 0.50,
    "_MIN_COMPONENT_MEAN_REVIEW": 0.52,
    "_MIN_REP_PAIR_SCORE": 0.50,
    "_MIN_MEMBER_TOP2_MEAN": 0.50,
    "_MIN_NEIGHBOR_COUNT": 1,
    "_STRONG_EDGE_SCORE": 0.62,
    "_PAIR_SCORE_GROUP2_MIN": 0.66,
    "_CENTER_SIM_GROUP2_MIN": 88.8,
    "_EDGE_VEC_GROUP2_MIN": 0.68,
    "_INLIERS_GROUP2_MIN": 5,
}


def one(cur, sql: str, params=()):
    cur.execute(sql, params)
    return cur.fetchone()


def all_rows(cur, sql: str, params=()):
    cur.execute(sql, params)
    return cur.fetchall()


def flag(ok: bool) -> str:
    return "OK  " if ok else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit MATCH/VALIDATE/Gold v1.3.16")
    parser.add_argument("--source-group", default="PWP")
    parser.add_argument(
        "--run-id",
        help="Opsional: audit hasil VALIDATE run baru sebelum PUBLISH.",
    )
    args = parser.parse_args()
    source = args.source_group.upper().strip()

    settings = Settings()
    settings.validate()
    failures: list[str] = []
    warnings: list[str] = []

    print("=" * 78)
    print(f"MATCH / VALIDATE / ACTIVE GOLD AUDIT: {source}")
    print("=" * 78)

    version_ok = __version__ == "1.3.16"
    print(f"{flag(version_ok)} repository version: {__version__}")
    if not version_ok:
        failures.append(f"repository bukan 1.3.16 ({__version__})")

    config_checks = {
        "REVIEW_THRESHOLD": (settings.review_threshold, 90.0),
        "REUSED_THRESHOLD": (settings.reused_threshold, 93.0),
        "CENTER_CROP_RATIO": (settings.center_crop_ratio, 0.72),
        "PAIR_SCORE_EDGE": (settings.pair_score_edge, 0.54),
        "PAIR_SCORE_STRONG": (settings.pair_score_strong, 0.77),
        "MIN_MODAL_SUPPORT": (settings.min_modal_support, 2),
        "MATCH_REQUIRE_MUTUAL_KNN": (settings.match_require_mutual_knn, True),
        "MATCH_COMPONENT_MAX_SIZE": (settings.match_component_max_size, 500),
        "MAX_ACCEPTED_GROUP_SIZE": (settings.max_accepted_group_size, 500),
        "EXCLUDE_FILENAME_TOKENS": (tuple(settings.exclude_filename_tokens or ()), ("AI",)),
        "LOW_LIGHT_GUARD": (settings.low_light_guard, True),
        "DUPLICATE_REQUIRE_STRUCTURAL_EVIDENCE": (
            settings.duplicate_require_structural_evidence,
            True,
        ),
        "DUPLICATE_REQUIRE_HASH_SUPPORT": (
            settings.duplicate_require_hash_support,
            True,
        ),
        "VALIDATION_GROUP_MEAN_SIMILARITY": (
            settings.validation_group_mean_similarity,
            90.0,
        ),
        "VALIDATION_MEMBER_MEAN_SIMILARITY": (
            settings.validation_member_mean_similarity,
            93.0,
        ),
        "VALIDATION_REPRESENTATIVE_SIMILARITY": (
            settings.validation_representative_similarity,
            93.0,
        ),
    }
    for name, (actual, expected) in config_checks.items():
        ok = actual == expected
        print(f"{flag(ok)} {name}: actual={actual!r} expected={expected!r}")
        if not ok:
            failures.append(f"konfigurasi {name} berbeda")

    for name, expected in EXPECTED_VALIDATOR.items():
        actual = getattr(validator, name, None)
        ok = actual == expected
        print(f"{flag(ok)} validator {name}: actual={actual!r} expected={expected!r}")
        if not ok:
            failures.append(f"validator {name} berbeda dari image_utils")

    with connection(settings) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        active = one(
            cur,
            """
            SELECT
                count(*)::bigint AS groups,
                coalesce(sum(usage_count), 0)::bigint AS usages,
                coalesce(max(usage_count), 0)::bigint AS max_group,
                count(DISTINCT run_id)::bigint AS active_runs
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s) AND is_active=true
            """,
            (source,),
        )
        detail = one(
            cur,
            """
            SELECT
                count(*)::bigint AS details,
                count(*) FILTER (
                    WHERE upper(image_name) ~ '(^|[^A-Z0-9])AI([^A-Z0-9]|$)'
                )::bigint AS ai_names,
                count(*) FILTER (WHERE tanggal IS NULL)::bigint AS missing_month,
                count(*) FILTER (
                    WHERE nullif(btrim(coalesce(estate, '')), '') IS NULL
                )::bigint AS missing_estate
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s) AND is_active=true
            """,
            (source,),
        )
        duplicates = one(
            cur,
            """
            SELECT count(*)::bigint AS duplicate_memberships
            FROM (
                SELECT file_uid
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s) AND is_active=true
                GROUP BY file_uid
                HAVING count(DISTINCT group_id) > 1
            ) x
            """,
            (source,),
        )
        duplicate_names = one(
            cur,
            """
            SELECT count(*)::bigint AS duplicate_names
            FROM (
                SELECT upper(btrim(image_name)) AS logical_name
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s) AND is_active=true
                GROUP BY upper(btrim(image_name))
                HAVING count(DISTINCT group_id) > 1
            ) x
            """,
            (source,),
        )
        bad_representatives = one(
            cur,
            """
            SELECT count(*)::bigint AS bad_groups
            FROM (
                SELECT group_id
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s) AND is_active=true
                GROUP BY group_id
                HAVING count(*) FILTER (WHERE is_representative=1) <> 1
            ) x
            """,
            (source,),
        )
        size_bins = all_rows(
            cur,
            """
            SELECT
                CASE
                    WHEN usage_count=2 THEN '2'
                    WHEN usage_count BETWEEN 3 AND 5 THEN '3-5'
                    WHEN usage_count BETWEEN 6 AND 10 THEN '6-10'
                    WHEN usage_count BETWEEN 11 AND 50 THEN '11-50'
                    WHEN usage_count BETWEEN 51 AND 500 THEN '51-500'
                    ELSE '>500'
                END AS bucket,
                count(*)::bigint AS groups,
                sum(usage_count)::bigint AS usages
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s) AND is_active=true
            GROUP BY 1
            ORDER BY min(usage_count)
            """,
            (source,),
        )
        statuses = all_rows(
            cur,
            """
            SELECT validation_status, count(*)::bigint AS groups,
                   sum(usage_count)::bigint AS usages,
                   round(avg(mean_pair_score)::numeric, 4) AS avg_pair_score,
                   round(avg(mean_embedding_similarity)::numeric, 2)
                       AS avg_embedding_similarity,
                   round(min(min_member_mean_similarity)::numeric, 2)
                       AS min_member_similarity
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s) AND is_active=true
            GROUP BY validation_status
            ORDER BY groups DESC
            """,
            (source,),
        )
        first_month_growth = all_rows(
            cur,
            """
            WITH per_group AS (
                SELECT group_id,
                       min(date_trunc('month', tanggal))::date AS first_month,
                       count(*)::bigint AS usages
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s)
                  AND is_active=true
                  AND tanggal IS NOT NULL
                GROUP BY group_id
            )
            SELECT to_char(first_month, 'YYYY-MM') AS first_month,
                   count(*)::bigint AS new_groups,
                   sum(usages)::bigint AS usages
            FROM per_group
            GROUP BY first_month
            ORDER BY first_month
            """,
            (source,),
        )
        active_monthly = all_rows(
            cur,
            """
            SELECT to_char(date_trunc('month', tanggal), 'YYYY-MM') AS month,
                   count(DISTINCT group_id)::bigint AS groups_touching_month,
                   count(*)::bigint AS usages
            FROM gold.reused_photo_detail
            WHERE upper(source_group)=upper(%s)
              AND is_active=true
              AND tanggal IS NOT NULL
            GROUP BY date_trunc('month', tanggal)
            ORDER BY date_trunc('month', tanggal)
            """,
            (source,),
        )
        low_mean_samples = all_rows(
            cur,
            """
            SELECT group_id, usage_count, validation_status,
                   round(mean_pair_score::numeric, 6) AS mean_pair_score
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s)
              AND is_active=true
              AND (
                    mean_pair_score IS NULL
                 OR (upper(validation_status) LIKE '%%REVIEW%%' AND mean_pair_score < 0.52)
                 OR (upper(validation_status) NOT LIKE '%%REVIEW%%' AND mean_pair_score < 0.50)
              )
            ORDER BY mean_pair_score NULLS FIRST, usage_count DESC
            LIMIT 12
            """,
            (source,),
        )
        low_mean = one(
            cur,
            """
            SELECT count(*)::bigint AS bad_groups,
                   min(mean_pair_score) AS min_score
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s)
              AND is_active=true
              AND (
                    mean_pair_score IS NULL
                 OR (upper(validation_status) LIKE '%%REVIEW%%' AND mean_pair_score < 0.52)
                 OR (upper(validation_status) NOT LIKE '%%REVIEW%%' AND mean_pair_score < 0.50)
              )
            """,
            (source,),
        )
        low_embedding = one(
            cur,
            """
            SELECT count(*)::bigint AS bad_groups,
                   min(mean_embedding_similarity) AS min_group_similarity,
                   min(min_member_mean_similarity) AS min_member_similarity
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s)
              AND is_active=true
              AND (
                    mean_embedding_similarity IS NULL
                 OR min_member_mean_similarity IS NULL
                 OR mean_embedding_similarity < %s
                 OR min_member_mean_similarity < %s
              )
            """,
            (
                source,
                settings.validation_group_mean_similarity,
                settings.validation_member_mean_similarity,
            ),
        )
        active_run = one(
            cur,
            """
            SELECT run_id::text AS run_id
            FROM gold.reused_photo_group
            WHERE upper(source_group)=upper(%s) AND is_active=true
            ORDER BY published_at DESC
            LIMIT 1
            """,
            (source,),
        )

        run_metrics = None
        audit_pairs = None
        low_light_samples = []
        validated_summary = None
        validated_duplicates = None
        validated_bad_representatives = None
        validated_ai = {"ai_names": 0}
        candidate_summary = None
        validated_size_bins = []
        validated_statuses = []
        validated_monthly = []
        fragmentation = None
        publishable_summary = None
        baseline_gold = None
        baseline_monthly = []
        validation_is_active = False
        validation_run_id = args.run_id or (active_run["run_id"] if active_run else None)
        if validation_run_id:
            try:
                validation_run_id = str(uuid.UUID(str(validation_run_id)))
            except ValueError as exc:
                raise SystemExit(f"run_id tidak valid: {validation_run_id!r}") from exc
            run_metrics = one(
                cur,
                """
                SELECT source_group, stage, status, metrics, error_message,
                       coalesce(
                           (metrics->>'validation_low_light_guard')::boolean,
                           (settings->>'low_light_guard')::boolean,
                           false
                       ) AS low_light_guard_applied,
                       coalesce(
                           (metrics->>'validation_exact_group_similarity')::boolean,
                           false
                       ) AS exact_group_similarity_applied,
                       coalesce(
                           (metrics->>'validation_structural_duplicate_guard')::boolean,
                           false
                       ) AS structural_duplicate_guard_applied,
                       coalesce(
                           (metrics->>'validation_representative_star_guard')::boolean,
                           false
                       ) AS representative_star_guard_applied
                FROM processing.pipeline_run
                WHERE run_id=%s::uuid
                """,
                (validation_run_id,),
            )
            if not run_metrics:
                raise SystemExit(f"run_id tidak ditemukan: {validation_run_id}")
            if str(run_metrics["source_group"]).upper() != source:
                raise SystemExit(
                    f"run_id adalah source_group={run_metrics['source_group']}, bukan {source}"
                )
            validation_is_active = bool(
                active_run
                and str(active_run.get("run_id")) == str(validation_run_id)
            )
            if validation_is_active:
                baseline_gold = one(
                    cur,
                    """
                    SELECT run_id::text AS run_id,
                           count(*)::bigint AS groups,
                           coalesce(sum(usage_count), 0)::bigint AS usages,
                           coalesce(max(usage_count), 0)::bigint AS max_group,
                           max(published_at) AS published_at
                    FROM gold.reused_photo_group
                    WHERE upper(source_group)=upper(%s)
                      AND run_id<>%s::uuid
                    GROUP BY run_id
                    ORDER BY max(published_at) DESC
                    LIMIT 1
                    """,
                    (source, validation_run_id),
                )
            elif active_run:
                baseline_gold = {
                    "run_id": str(active_run["run_id"]),
                    "groups": active["groups"],
                    "usages": active["usages"],
                    "max_group": active["max_group"],
                    "published_at": None,
                }
            if baseline_gold:
                baseline_monthly = all_rows(
                    cur,
                    """
                    SELECT to_char(date_trunc('month', tanggal), 'YYYY-MM') AS month,
                           count(DISTINCT group_id)::bigint AS groups_touching_month,
                           count(*)::bigint AS usages
                    FROM gold.reused_photo_detail
                    WHERE upper(source_group)=upper(%s)
                      AND run_id=%s::uuid
                      AND tanggal IS NOT NULL
                    GROUP BY date_trunc('month', tanggal)
                    ORDER BY date_trunc('month', tanggal)
                    """,
                    (source, baseline_gold["run_id"]),
                )
            audit_pairs = one(
                cur,
                """
                SELECT count(*)::bigint AS pairs,
                       count(*) FILTER (WHERE same_object)::bigint AS same_object,
                       round(avg(pair_score)::numeric, 4) AS avg_pair_score,
                       count(*) FILTER (WHERE low_light_pair)::bigint AS low_light_pairs,
                       count(*) FILTER (
                           WHERE low_light_pair AND low_light_guard_pass=false
                       )::bigint AS low_light_rejected,
                       round(avg(raw_pair_score) FILTER (
                           WHERE low_light_pair AND low_light_guard_pass=false
                       )::numeric, 4) AS rejected_raw_score
                FROM processing.validation_pair_audit
                WHERE run_id=%s::uuid
                """,
                (validation_run_id,),
            )
            low_light_samples = all_rows(
                cur,
                """
                SELECT file_uid_a, file_uid_b,
                       round(raw_pair_score::numeric, 4) AS raw_score,
                       round(full_emb_sim::numeric, 2) AS full_sim,
                       round(center_emb_sim::numeric, 2) AS center_sim,
                       phash_dist, edgehash_dist,
                       round(edgevec_sim::numeric, 4) AS edgevec_sim,
                       inliers,
                       round(left_luma_mean::numeric, 2) AS left_luma,
                       round(right_luma_mean::numeric, 2) AS right_luma
                FROM processing.validation_pair_audit
                WHERE run_id=%s::uuid
                  AND low_light_pair=true
                  AND low_light_guard_pass=false
                ORDER BY raw_pair_score DESC NULLS LAST
                LIMIT 10
                """,
                (validation_run_id,),
            )
            validated_ai = one(
                cur,
                """
                SELECT count(*)::bigint AS ai_names
                FROM processing.validated_group_member
                WHERE run_id=%s::uuid
                  AND upper(file_name) ~ '(^|[^A-Z0-9])AI([^A-Z0-9]|$)'
                """,
                (validation_run_id,),
            )
            validated_summary = one(
                cur,
                """
                SELECT count(*)::bigint AS groups,
                       coalesce(sum(usage_count), 0)::bigint AS usages,
                       coalesce(max(usage_count), 0)::bigint AS max_group,
                       min(mean_pair_score) AS min_mean,
                       min(mean_embedding_similarity) AS min_embedding_similarity,
                       min(min_member_mean_similarity) AS min_member_similarity,
                       count(*) FILTER (
                           WHERE mean_pair_score IS NULL
                              OR (upper(validation_status) LIKE '%%REVIEW%%' AND mean_pair_score < 0.52)
                              OR (upper(validation_status) NOT LIKE '%%REVIEW%%' AND mean_pair_score < 0.50)
                       )::bigint AS low_mean_groups,
                       count(*) FILTER (
                           WHERE mean_embedding_similarity IS NULL
                              OR min_member_mean_similarity IS NULL
                              OR mean_embedding_similarity < %s
                              OR min_member_mean_similarity < %s
                       )::bigint AS low_embedding_groups
                FROM processing.validated_group
                WHERE run_id=%s::uuid
                """,
                (
                    settings.validation_group_mean_similarity,
                    settings.validation_member_mean_similarity,
                    validation_run_id,
                ),
            )
            validated_duplicates = one(
                cur,
                """
                SELECT
                    (SELECT count(*) FROM (
                        SELECT file_uid
                        FROM processing.validated_group_member
                        WHERE run_id=%s::uuid
                        GROUP BY file_uid
                        HAVING count(DISTINCT validated_group_id) > 1
                    ) x)::bigint AS duplicate_uids,
                    (SELECT count(*) FROM (
                        SELECT upper(btrim(file_name)) AS logical_name
                        FROM processing.validated_group_member
                        WHERE run_id=%s::uuid
                        GROUP BY upper(btrim(file_name))
                        HAVING count(DISTINCT validated_group_id) > 1
                    ) y)::bigint AS duplicate_names
                """,
                (validation_run_id, validation_run_id),
            )
            validated_bad_representatives = one(
                cur,
                """
                SELECT count(*)::bigint AS bad_groups
                FROM (
                    SELECT validated_group_id
                    FROM processing.validated_group_member
                    WHERE run_id=%s::uuid
                    GROUP BY validated_group_id
                    HAVING count(*) FILTER (WHERE is_representative) <> 1
                ) x
                """,
                (validation_run_id,),
            )
            candidate_summary = one(
                cur,
                """
                SELECT count(*)::bigint AS groups,
                       coalesce(sum(usage_count), 0)::bigint AS usages,
                       coalesce(max(usage_count), 0)::bigint AS max_group
                FROM processing.candidate_group
                WHERE run_id=%s::uuid
                """,
                (validation_run_id,),
            )
            fragmentation = one(
                cur,
                """
                WITH split AS (
                    SELECT source_candidate_group_id,
                           count(*)::bigint AS output_groups,
                           sum(usage_count)::bigint AS output_usages
                    FROM processing.validated_group
                    WHERE run_id=%s::uuid
                      AND source_candidate_group_id IS NOT NULL
                    GROUP BY source_candidate_group_id
                )
                SELECT count(*)::bigint AS source_candidates,
                       coalesce(sum(output_groups), 0)::bigint AS output_groups,
                       count(*) FILTER (WHERE output_groups > 1)::bigint
                           AS split_candidates,
                       coalesce(max(output_groups), 0)::bigint AS max_splits,
                       round(avg(output_groups)::numeric, 2) AS avg_splits
                FROM split
                """,
                (validation_run_id,),
            )
            eligible_cte = """
                WITH quality_group AS (
                    SELECT g.validated_group_id, g.validation_status,
                           g.mean_pair_score, g.mean_embedding_similarity,
                           g.min_member_mean_similarity
                    FROM processing.validated_group g
                    WHERE g.run_id=%s::uuid
                      AND g.mean_pair_score IS NOT NULL
                      AND (
                          (upper(g.validation_status) LIKE '%%REVIEW%%'
                           AND g.mean_pair_score >= %s)
                          OR
                          (upper(g.validation_status) NOT LIKE '%%REVIEW%%'
                           AND g.mean_pair_score >= %s)
                      )
                      AND g.mean_embedding_similarity IS NOT NULL
                      AND g.min_member_mean_similarity IS NOT NULL
                      AND g.mean_embedding_similarity >= %s
                      AND g.min_member_mean_similarity >= %s
                ), visible_group AS (
                    SELECT q.validated_group_id,
                           q.validation_status,
                           count(*)::bigint AS visible_usage
                    FROM quality_group q
                    JOIN processing.validated_group_member m
                      ON m.run_id=%s::uuid
                     AND m.validated_group_id=q.validated_group_id
                    LEFT JOIN processing.image_context c ON c.file_uid=m.file_uid
                    WHERE nullif(btrim(coalesce(c.estate, '')), '') IS NOT NULL
                    GROUP BY q.validated_group_id, q.validation_status
                    HAVING count(*) >= 2
                )
            """
            eligible_params = (
                validation_run_id,
                settings.min_publish_mean_review,
                settings.min_publish_mean_confirmed,
                settings.validation_group_mean_similarity,
                settings.validation_member_mean_similarity,
                validation_run_id,
            )
            publishable_summary = one(
                cur,
                eligible_cte
                + """
                SELECT count(*)::bigint AS groups,
                       coalesce(sum(visible_usage), 0)::bigint AS usages,
                       coalesce(max(visible_usage), 0)::bigint AS max_group
                FROM visible_group
                """,
                eligible_params,
            )
            validated_size_bins = all_rows(
                cur,
                eligible_cte
                + """
                SELECT CASE
                           WHEN visible_usage=2 THEN '2'
                           WHEN visible_usage BETWEEN 3 AND 5 THEN '3-5'
                           WHEN visible_usage BETWEEN 6 AND 10 THEN '6-10'
                           WHEN visible_usage BETWEEN 11 AND 50 THEN '11-50'
                           WHEN visible_usage BETWEEN 51 AND 500 THEN '51-500'
                           ELSE '>500'
                       END AS bucket,
                       count(*)::bigint AS groups,
                       sum(visible_usage)::bigint AS usages
                FROM visible_group
                GROUP BY 1
                ORDER BY min(visible_usage)
                """,
                eligible_params,
            )
            validated_statuses = all_rows(
                cur,
                eligible_cte
                + """
                SELECT validation_status,
                       count(*)::bigint AS groups,
                       sum(visible_usage)::bigint AS usages
                FROM visible_group
                GROUP BY validation_status
                ORDER BY groups DESC
                """,
                eligible_params,
            )
            validated_monthly = all_rows(
                cur,
                eligible_cte
                + """
                SELECT to_char(date_trunc('month', c.period_date), 'YYYY-MM') AS month,
                       count(DISTINCT v.validated_group_id)::bigint
                           AS groups_touching_month,
                       count(*)::bigint AS usages
                FROM visible_group v
                JOIN processing.validated_group_member m
                  ON m.run_id=%s::uuid
                 AND m.validated_group_id=v.validated_group_id
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE c.period_date IS NOT NULL
                  AND nullif(btrim(coalesce(c.estate, '')), '') IS NOT NULL
                GROUP BY date_trunc('month', c.period_date)
                ORDER BY date_trunc('month', c.period_date)
                """,
                (*eligible_params, validation_run_id),
            )

    print("-" * 78)
    print(
        f"ACTIVE GOLD groups={active['groups']:,} usages={active['usages']:,} "
        f"details={detail['details']:,} max_group={active['max_group']:,} "
        f"active_runs={active['active_runs']}"
    )

    guards = {
        "active Gold hanya berasal dari satu run": active["active_runs"] <= 1,
        "usage group sama dengan detail": active["usages"] == detail["details"],
        "tidak ada group >500": active["max_group"] <= 500,
        "tidak ada filename token AI di active Gold": detail["ai_names"] == 0,
        "tidak ada filename token AI di validated run": validated_ai["ai_names"] == 0,
        "tidak ada file masuk lebih dari satu active group": duplicates["duplicate_memberships"] == 0,
        "tidak ada logical filename masuk lebih dari satu active group": duplicate_names["duplicate_names"] == 0,
        "setiap group punya tepat satu representative": bad_representatives["bad_groups"] == 0,
        "active Gold tidak punya legacy pair_score rendah/NULL": low_mean["bad_groups"] == 0,
        "seluruh active group memenuhi exact group/member threshold": low_embedding["bad_groups"] == 0,
        "tidak ada bulan kosong": detail["missing_month"] == 0,
    }
    for name, ok in guards.items():
        print(f"{flag(ok)} {name}")
        if not ok:
            failures.append(name)

    if detail["missing_estate"]:
        warnings.append(f"{detail['missing_estate']:,} detail tidak punya estate dan disembunyikan dashboard")

    print("-" * 78)
    print("GROUP SIZE DISTRIBUTION")
    for row in size_bins:
        print(f"  {row['bucket']:>6}: groups={row['groups']:,} usages={row['usages']:,}")

    print("VALIDATION STATUS")
    for row in statuses:
        print(
            f"  {row['validation_status']}: groups={row['groups']:,} "
            f"usages={row['usages']:,} avg_pair_score={row['avg_pair_score']} "
            f"avg_similarity={row['avg_embedding_similarity']} "
            f"min_member_similarity={row['min_member_similarity']}"
        )

    print("GROUP PERTAMA MUNCUL PER BULAN")
    for row in first_month_growth:
        print(
            f"  {row['first_month']}: new_groups={row['new_groups']:,} "
            f"usages={row['usages']:,}"
        )
    print("ACTIVE GOLD USAGE PER BULAN")
    for row in active_monthly:
        print(
            f"  {row['month']}: groups_touching={row['groups_touching_month']:,} "
            f"usages={row['usages']:,}"
        )
    if low_mean_samples:
        print("ACTIVE GROUP DENGAN LEGACY PAIR_SCORE RENDAH/NULL")
        for row in low_mean_samples:
            print(
                f"  group={row['group_id']} usage={row['usage_count']} "
                f"status={row['validation_status']} mean={row['mean_pair_score']}"
            )

    if run_metrics:
        print("VALIDATION RUN")
        print(
            f"  run_id={validation_run_id} stage={run_metrics['stage']} "
            f"status={run_metrics['status']} "
            f"low_light_guard_applied={run_metrics['low_light_guard_applied']} "
            f"exact_group_similarity_applied={run_metrics['exact_group_similarity_applied']} "
            f"structural_duplicate_guard_applied="
            f"{run_metrics['structural_duplicate_guard_applied']} "
            f"representative_star_guard_applied="
            f"{run_metrics['representative_star_guard_applied']}"
        )
        metrics = run_metrics.get("metrics") or {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        selected = {
            key: metrics.get(key)
            for key in (
                "match_images",
                "match_effective_top_k",
                "candidate_groups",
                "validated_groups",
                "validation_removed_images",
                "validation_exact_embedding_pairs",
                "validation_similarity_rejected_images",
                "gold_groups",
                "gold_detail_rows",
            )
            if key in metrics
        }
        print(f"  metrics={json.dumps(selected, ensure_ascii=False)}")
    if audit_pairs:
        print(
            f"VALIDATION AUDIT pairs={audit_pairs['pairs']:,} "
            f"same_object={audit_pairs['same_object']:,} "
            f"avg_pair_score={audit_pairs['avg_pair_score']}"
        )
        print(
            f"LOW-LIGHT pairs={audit_pairs['low_light_pairs']:,} "
            f"rejected={audit_pairs['low_light_rejected']:,} "
            f"rejected_raw_avg={audit_pairs['rejected_raw_score']}"
        )
        if low_light_samples:
            print("LOW-LIGHT REJECTED SAMPLES")
            for row in low_light_samples:
                print(
                    f"  {row['file_uid_a']} <> {row['file_uid_b']} "
                    f"raw={row['raw_score']} full/center="
                    f"{row['full_sim']}/{row['center_sim']} "
                    f"p/e={row['phash_dist']}/{row['edgehash_dist']} "
                    f"edge={row['edgevec_sim']} inliers={row['inliers']} "
                    f"luma={row['left_luma']}/{row['right_luma']}"
                )
    if validated_summary:
        print(
            f"VALIDATED groups={validated_summary['groups']:,} "
            f"usages={validated_summary['usages']:,} "
            f"max_group={validated_summary['max_group']:,} "
            f"min_pair_mean={validated_summary['min_mean']} "
            f"min_group_similarity={validated_summary['min_embedding_similarity']} "
            f"min_member_similarity={validated_summary['min_member_similarity']}"
        )
        run_state = "RUN SUDAH ACTIVE" if validation_is_active else "RUN BELUM ACTIVE"
        print(f"GROWTH DIAGNOSIS (READ-ONLY, {run_state})")
        if baseline_gold:
            print(
                f"  BASELINE run_id={baseline_gold['run_id']} "
                f"groups={baseline_gold['groups']:,} "
                f"usages={baseline_gold['usages']:,} "
                f"max_group={baseline_gold['max_group']:,}"
            )
            print("  BASELINE USAGE PER BULAN")
            for row in baseline_monthly:
                print(
                    f"    {row['month']}: groups_touching="
                    f"{row['groups_touching_month']:,} usages={row['usages']:,}"
                )
        else:
            print("  BASELINE tidak ditemukan; delta historis tidak dapat dihitung")
        print(
            f"  MATCH components={candidate_summary['groups']:,} "
            f"members={candidate_summary['usages']:,} "
            f"max_component={candidate_summary['max_group']:,}"
        )
        print(
            f"  VALIDATE raw_groups={validated_summary['groups']:,} "
            f"raw_usages={validated_summary['usages']:,}"
        )
        print(
            f"  PUBLISH eligible_groups={publishable_summary['groups']:,} "
            f"eligible_usages={publishable_summary['usages']:,} "
            f"max_group={publishable_summary['max_group']:,}"
        )
        print(
            f"  FRAGMENTATION source_candidates={fragmentation['source_candidates']:,} "
            f"split_candidates={fragmentation['split_candidates']:,} "
            f"output_groups={fragmentation['output_groups']:,} "
            f"avg/max_splits={fragmentation['avg_splits']}/"
            f"{fragmentation['max_splits']:,}"
        )
        print("  ELIGIBLE GROUP SIZE")
        for row in validated_size_bins:
            print(
                f"    {row['bucket']:>6}: groups={row['groups']:,} "
                f"usages={row['usages']:,}"
            )
        print("  ELIGIBLE VALIDATION STATUS")
        for row in validated_statuses:
            print(
                f"    {row['validation_status']}: groups={row['groups']:,} "
                f"usages={row['usages']:,}"
            )
        print("  ELIGIBLE USAGE PER BULAN")
        for row in validated_monthly:
            print(
                f"    {row['month']}: groups_touching="
                f"{row['groups_touching_month']:,} usages={row['usages']:,}"
            )
        baseline_month_set = {str(row['month']) for row in baseline_monthly}
        candidate_month_set = {str(row['month']) for row in validated_monthly}
        added_months = sorted(candidate_month_set - baseline_month_set)
        group_factor = (
            float(publishable_summary['groups']) / float(baseline_gold['groups'])
            if baseline_gold and baseline_gold['groups'] else None
        )
        usage_factor = (
            float(publishable_summary['usages']) / float(baseline_gold['usages'])
            if baseline_gold and baseline_gold['usages'] else None
        )
        eligible_size_two = next(
            (int(row['groups']) for row in validated_size_bins if row['bucket'] == '2'),
            0,
        )
        eligible_groups = int(publishable_summary['groups'] or 0)
        size_two_pct = (
            100.0 * eligible_size_two / eligible_groups if eligible_groups else 0.0
        )
        group_factor_text = (
            f"{group_factor:.2f}x" if group_factor is not None else "N/A"
        )
        usage_factor_text = (
            f"{usage_factor:.2f}x" if usage_factor is not None else "N/A"
        )
        print(
            f"  DELTA baseline_run="
            f"{baseline_gold['run_id'] if baseline_gold else 'N/A'} "
            f"group_factor={group_factor_text} "
            f"usage_factor={usage_factor_text} "
            f"size2={size_two_pct:.1f}% added_months={added_months}"
        )
        if added_months:
            warnings.append(
                "lonjakan sebagian dijelaskan perluasan bulan: "
                + ", ".join(added_months)
            )
        if size_two_pct > 70.0:
            warnings.append(
                f"{size_two_pct:.1f}% eligible group berukuran 2; lonjakan utama "
                "mengarah ke fragmentasi MATCH/VALIDATE"
            )
        validation_guards = {
            "validated run dibuat dengan low-light guard": bool(run_metrics["low_light_guard_applied"]),
            "validated run memakai exact all-pair group similarity": bool(run_metrics["exact_group_similarity_applied"]),
            "validated run memakai structural duplicate guard": bool(run_metrics["structural_duplicate_guard_applied"]),
            "validated run memakai representative-star guard v1.3.16": bool(run_metrics["representative_star_guard_applied"]),
            "validated run tidak punya similarity di bawah threshold v1.3.16": validated_summary["low_embedding_groups"] == 0,
            "validated run tidak punya group >500": validated_summary["max_group"] <= 500,
            "validated run tidak punya duplicate file_uid": validated_duplicates["duplicate_uids"] == 0,
            "validated run tidak punya duplicate logical filename": validated_duplicates["duplicate_names"] == 0,
            "validated run punya tepat satu representative per group": validated_bad_representatives["bad_groups"] == 0,
        }
        for name, ok in validation_guards.items():
            print(f"{flag(ok)} {name}")
            if not ok:
                failures.append(name)
        if validated_summary["low_mean_groups"]:
            warnings.append(
                f"{validated_summary['low_mean_groups']:,} validated group mempunyai "
                "legacy pair_score rendah/NULL; tidak menggagalkan audit karena "
                "exact group/member similarity telah diperiksa"
            )

    size_two = next((row["groups"] for row in size_bins if row["bucket"] == "2"), 0)
    if active["groups"] and size_two / active["groups"] > 0.70:
        warnings.append("lebih dari 70% group hanya berisi dua gambar; periksa fragmentasi KNN")

    print("-" * 78)
    for warning in warnings:
        print(f"WARN {warning}")
    if failures:
        print("RESULT: FAIL")
        for item in failures:
            print(f"  - {item}")
        return 1

    print("RESULT: PASS")
    print("Guard teknis v1.3.16, representative-star 93%, exact group, low-light,")
    print("AI exclusion, dan integritas Gold lulus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
