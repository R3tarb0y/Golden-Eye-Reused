-- Read-only. Jalankan dari DBeaver/psql saat republish-clean-oph terlihat lama.
SELECT
    pid,
    now() - query_start AS elapsed,
    state,
    wait_event_type,
    wait_event,
    left(regexp_replace(query, E'[\\n\\r\\t]+', ' ', 'g'), 260) AS query
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND state <> 'idle'
ORDER BY query_start;
