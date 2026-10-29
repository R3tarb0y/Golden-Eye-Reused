from __future__ import annotations

import json
import uuid
from typing import Any

from .config import Settings
from .context import sync_context
from .db import connection, create_run, fetch_all_dict, update_run
from .enrichment import repair_legacy_oph_context
from .publisher import publish_gold


_LEGACY_UNSAFE_METHODS = (
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE",
    "MATCH_BY_RAW_ACTOR_DATE_UNIQUE_ESTATE_NO_OPH",
)


def _preflight(
    settings: Settings,
    source_group: str,
    source_run_id: uuid.UUID,
) -> dict[str, Any]:
    with connection(settings) as conn:
        source = fetch_all_dict(
            conn,
            """
            SELECT run_id::text AS run_id, upper(source_group) AS source_group,
                   stage, status, metrics
            FROM processing.pipeline_run
            WHERE run_id=%s
            """,
            (source_run_id,),
        )
        if not source:
            raise RuntimeError(f"source run tidak ditemukan: {source_run_id}")
        if str(source[0]["source_group"]) != source_group:
            raise RuntimeError(
                f"source run adalah {source[0]['source_group']}, bukan {source_group}"
            )

        validated = fetch_all_dict(
            conn,
            """
            SELECT
                count(DISTINCT g.validated_group_id)::bigint AS groups,
                count(m.file_uid)::bigint AS members,
                count(*) FILTER (
                    WHERE upper(coalesce(c.match_method, ''))=ANY(%s)
                ) AS unsafe_members,
                count(*) FILTER (
                    WHERE nullif(processing.normalize_oph(c.oph), '') IS NULL
                ) AS missing_oph
            FROM processing.validated_group g
            JOIN processing.validated_group_member m
              ON m.run_id=g.run_id
             AND m.validated_group_id=g.validated_group_id
            LEFT JOIN processing.image_context c ON c.file_uid=m.file_uid
            WHERE g.run_id=%s
            """,
            (list(_LEGACY_UNSAFE_METHODS), source_run_id),
        )[0]
        validated_months = [
            str(row["month"])
            for row in fetch_all_dict(
                conn,
                """
                SELECT DISTINCT to_char(c.period_date, 'YYYY-MM') AS month
                FROM processing.validated_group_member m
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND c.period_date IS NOT NULL
                  AND nullif(btrim(coalesce(c.estate, '')), '') IS NOT NULL
                ORDER BY 1
                """,
                (source_run_id,),
            )
            if row.get("month")
        ]
        validated_month_detail = {
            str(row["month"]): {
                "members": int(row.get("members") or 0),
                "legacy_unsafe": int(row.get("legacy_unsafe") or 0),
            }
            for row in fetch_all_dict(
                conn,
                """
                SELECT
                    to_char(c.period_date, 'YYYY-MM') AS month,
                    count(*)::bigint AS members,
                    count(*) FILTER (
                        WHERE upper(coalesce(c.match_method, ''))=ANY(%s)
                    )::bigint AS legacy_unsafe
                FROM processing.validated_group_member m
                JOIN processing.image_context c ON c.file_uid=m.file_uid
                WHERE m.run_id=%s
                  AND c.period_date IS NOT NULL
                  AND nullif(btrim(coalesce(c.estate, '')), '') IS NOT NULL
                GROUP BY to_char(c.period_date, 'YYYY-MM')
                ORDER BY 1
                """,
                (list(_LEGACY_UNSAFE_METHODS), source_run_id),
            )
            if row.get("month")
        }
        current_active_months = [
            str(row["month"])
            for row in fetch_all_dict(
                conn,
                """
                SELECT DISTINCT to_char(tanggal, 'YYYY-MM') AS month
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s)
                  AND is_active=true
                  AND tanggal IS NOT NULL
                ORDER BY 1
                """,
                (source_group,),
            )
            if row.get("month")
        ]
        # Legacy OPH harus diperiksa ulang walaupun bulannya sudah terlihat di
        # Gold aktif. Logika lama hanya merecover bulan yang hilang sehingga
        # DIN Januari-Juli dapat tetap kehilangan ribuan member pada OPH gate:
        # bulan ada, tetapi provenance OPH-nya masih unsafe.
        unsafe_months = sorted(
            month
            for month, detail in validated_month_detail.items()
            if int(detail.get("legacy_unsafe") or 0) > 0
        )
        planned_recovery_months = unsafe_months
        active = fetch_all_dict(
            conn,
            """
            WITH detail AS (
                SELECT processing.normalize_oph(oph) AS oph_key
                FROM gold.reused_photo_detail
                WHERE upper(source_group)=upper(%s)
                  AND run_id=%s
            )
            SELECT
                count(*)::bigint AS usages,
                count(DISTINCT oph_key)
                    FILTER (WHERE nullif(oph_key, '') IS NOT NULL)
                    AS distinct_oph
            FROM detail
            """,
            (source_group, source_run_id),
        )[0]

    usages = int(active.get("usages") or 0)
    distinct_oph = int(active.get("distinct_oph") or 0)
    return {
        "source_run": source[0],
        "validated_groups": int(validated.get("groups") or 0),
        "validated_members": int(validated.get("members") or 0),
        "unsafe_members": int(validated.get("unsafe_members") or 0),
        "missing_oph": int(validated.get("missing_oph") or 0),
        "validated_months": validated_months,
        "validated_month_detail": validated_month_detail,
        "current_active_months": current_active_months,
        "unsafe_months": unsafe_months,
        "planned_recovery_months": planned_recovery_months,
        "active_usages": usages,
        "active_distinct_oph": distinct_oph,
        "active_duplicate_occurrences": max(0, usages - distinct_oph),
    }


def republish_clean_oph(
    settings: Settings,
    source_group: str,
    source_run_id: str,
    *,
    visual_only: bool = False,
    skip_context_sync: bool = False,
    apply: bool = False,
) -> dict[str, Any]:
    """Republish validated visual evidence with verified OPH kept unique.

    MATCH and VALIDATE are not repeated. A new run_id receives a clone of the
    immutable validated result, then the normal atomic publisher applies the
    current Context and OPH gates. The old Gold snapshot remains active unless
    the new publish succeeds.
    """
    source_group = source_group.upper().strip()
    source_uuid = uuid.UUID(str(source_run_id))
    preflight = _preflight(settings, source_group, source_uuid)

    print("=" * 78)
    print(
        f"CLEAN OPH REPUBLISH: {source_group} | source_run_id={source_uuid}"
    )
    print(f"mode={'VISUAL_ONLY' if visual_only else 'CONTEXT_REPAIR'}")
    print("=" * 78)
    print(
        f"validated_groups={preflight['validated_groups']:,} "
        f"validated_members={preflight['validated_members']:,}"
    )
    print(
        f"active_usages={preflight['active_usages']:,} "
        f"active_distinct_oph={preflight['active_distinct_oph']:,} "
        f"duplicate_occurrences={preflight['active_duplicate_occurrences']:,}"
    )
    print(
        f"legacy_unsafe_context_members={preflight['unsafe_members']:,} "
        f"missing_oph_members={preflight['missing_oph']:,}"
    )
    print(
        "validated_business_months="
        + ",".join(preflight["validated_months"])
    )
    print(
        "validated_members_by_month="
        + json.dumps(preflight["validated_month_detail"], sort_keys=True)
    )
    print(
        "current_active_months="
        + ",".join(preflight["current_active_months"])
    )
    print(
        "planned_legacy_recovery_months="
        + ",".join(preflight["planned_recovery_months"])
    )
    if not apply:
        print("DRY-RUN saja; database tidak diubah.")
        if visual_only:
            print(
                "VISUAL_ONLY: semua member visual valid tetap ditampilkan; "
                "OPH unsafe/missing menjadi NULL dan tidak masuk SAP/transport."
            )
        print("Tambahkan --apply setelah angka preflight diperiksa.")
        return {**preflight, "applied": False, "new_run_id": None}

    if preflight["validated_groups"] < 1 or preflight["validated_members"] < 2:
        raise RuntimeError("Validated source run kosong; republish dibatalkan.")

    if visual_only:
        print(
            "[CLEAN OPH] VISUAL_ONLY: Context/RAW recovery dilewati; "
            "OPH unsafe disanitasi saat atomic publish.",
            flush=True,
        )
        context_metrics = {"context_skipped_visual_only": 1}
        legacy_recovery_metrics = {
            "legacy_oph_recovery_skipped_visual_only": 1,
            "legacy_oph_recovered": 0,
        }
    else:
        if skip_context_sync:
            print(
                "[CLEAN OPH] Context sync dilewati; gunakan hanya bila sync_context "
                "pada percobaan sebelumnya sudah selesai/commit.",
                flush=True,
            )
            context_metrics = {"context_sync_skipped": 1}
        else:
            print("[CLEAN OPH] restore exact business evidence ke Context ...", flush=True)
            context_metrics = sync_context(settings, source_group)
        print(
            "[CLEAN OPH] repair OPH legacy tanpa membuang period_date ...",
            flush=True,
        )
        legacy_recovery_metrics = repair_legacy_oph_context(
            settings,
            source_group,
            source_uuid,
            recovery_months=preflight["planned_recovery_months"],
            allow_actor_date_allocation=False,
        )

    new_run_id = create_run(settings, source_group)
    with connection(settings) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO processing.validated_group
                (run_id, validated_group_id, source_group, group_signature,
                 representative_file_uid, usage_count,
                 source_candidate_group_id, validation_status, mean_pair_score,
                 mean_embedding_similarity, min_member_mean_similarity,
                 created_at)
            SELECT
                %s, validated_group_id, source_group, group_signature,
                representative_file_uid, usage_count,
                source_candidate_group_id, validation_status, mean_pair_score,
                mean_embedding_similarity, min_member_mean_similarity,
                created_at
            FROM processing.validated_group
            WHERE run_id=%s
            """,
            (new_run_id, source_uuid),
        )
        cloned_groups = int(cur.rowcount)
        cur.execute(
            """
            INSERT INTO processing.validated_group_member
                (run_id, validated_group_id, file_uid, file_name,
                 is_representative, member_validity, member_mean_similarity)
            SELECT
                %s, validated_group_id, file_uid, file_name,
                is_representative, member_validity, member_mean_similarity
            FROM processing.validated_group_member
            WHERE run_id=%s
            """,
            (new_run_id, source_uuid),
        )
        cloned_members = int(cur.rowcount)
        clean_metrics = {
            "oph_clean_republish": True,
            "oph_clean_visual_only": bool(visual_only),
            "oph_clean_source_run_id": str(source_uuid),
            "oph_clean_expected_months": preflight["validated_months"],
            "oph_clean_recovery_months": preflight["planned_recovery_months"],
            **legacy_recovery_metrics,
        }
        cur.execute(
            """
            UPDATE processing.pipeline_run target
            SET stage='VALIDATE',
                status='PAUSED',
                settings=source.settings,
                metrics=coalesce(source.metrics, '{}'::jsonb) || %s::jsonb,
                updated_at=now()
            FROM processing.pipeline_run source
            WHERE target.run_id=%s
              AND source.run_id=%s
            """,
            (json.dumps(clean_metrics), new_run_id, source_uuid),
        )

    if cloned_groups != preflight["validated_groups"]:
        raise RuntimeError(
            f"Clone group tidak lengkap: {cloned_groups:,}/"
            f"{preflight['validated_groups']:,}"
        )
    if cloned_members != preflight["validated_members"]:
        raise RuntimeError(
            f"Clone member tidak lengkap: {cloned_members:,}/"
            f"{preflight['validated_members']:,}"
        )

    print(
        f"[CLEAN OPH] cloned validated result ke run_id={new_run_id}; "
        "mulai atomic publish ...",
        flush=True,
    )
    try:
        publish_metrics = publish_gold(settings, new_run_id, source_group)
    except Exception as exc:
        update_run(
            settings,
            new_run_id,
            stage="PUBLISH",
            status="FAILED",
            metrics={"oph_clean_republish_failed": True},
            error_message=str(exc)[:4000],
        )
        raise

    with connection(settings) as conn:
        verification = fetch_all_dict(
            conn,
            """
            SELECT
                count(*)::bigint AS usages,
                count(DISTINCT processing.normalize_oph(oph))::bigint
                    AS distinct_oph,
                count(*) FILTER (
                    WHERE nullif(processing.normalize_oph(oph), '') IS NULL
                ) AS missing_oph
            FROM gold.reused_photo_detail
            WHERE run_id=%s AND is_active=true
            """,
            (new_run_id,),
        )[0]
        duplicate_verified_oph = int(
            fetch_all_dict(
                conn,
                """
                SELECT count(*) AS n
                FROM (
                    SELECT processing.normalize_oph(oph) AS oph_key
                    FROM gold.reused_photo_detail
                    WHERE run_id=%s AND is_active=true
                      AND nullif(processing.normalize_oph(oph), '') IS NOT NULL
                    GROUP BY processing.normalize_oph(oph)
                    HAVING count(*) > 1
                ) q
                """,
                (new_run_id,),
            )[0]["n"]
        )
        invalid_visual_groups = int(
            fetch_all_dict(
                conn,
                """
                SELECT count(*) AS n
                FROM (
                    SELECT group_id
                    FROM gold.reused_photo_detail
                    WHERE run_id=%s AND is_active=true
                    GROUP BY group_id
                    HAVING count(*) < 2
                ) q
                """,
                (new_run_id,),
            )[0]["n"]
        )

    usages = int(verification.get("usages") or 0)
    distinct_oph = int(verification.get("distinct_oph") or 0)
    missing_oph = int(verification.get("missing_oph") or 0)
    if duplicate_verified_oph or invalid_visual_groups:
        raise RuntimeError(
            "Post-publish visual/OPH verification gagal: "
            f"usages={usages:,} distinct_oph={distinct_oph:,} "
            f"unverified_visual={missing_oph:,} "
            f"duplicate_verified_oph={duplicate_verified_oph:,} "
            f"invalid_visual_groups={invalid_visual_groups:,}. "
            "Gunakan restore_gold_run.py untuk mengaktifkan snapshot lama."
        )

    print(
        f"OK clean Gold run_id={new_run_id} groups="
        f"{int(publish_metrics.get('gold_groups') or 0):,} "
        f"visual_usages={usages:,} verified_oph={distinct_oph:,} "
        f"unverified_visual={missing_oph:,}"
    )
    return {
        **preflight,
        "applied": True,
        "new_run_id": str(new_run_id),
        "context_metrics": context_metrics,
        "legacy_recovery_metrics": legacy_recovery_metrics,
        "publish_metrics": publish_metrics,
        "final_usages": usages,
        "final_distinct_oph": distinct_oph,
        "duplicate_verified_oph": duplicate_verified_oph,
        "invalid_visual_groups": invalid_visual_groups,
    }
