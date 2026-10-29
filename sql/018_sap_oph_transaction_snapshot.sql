-- Populasi OPH SAP canonical untuk dashboard Validasi Panen vs Transport.
-- Satu source/company + satu OPH hanya boleh muncul satu kali. Foto KP/KT
-- menjadi atribut coverage dan tidak menentukan denominator transaksi.

CREATE TABLE IF NOT EXISTS gold.sap_oph_transaction_snapshot (
    source_group text NOT NULL,
    period_year integer NOT NULL,
    sap_month text,
    oph text NOT NULL,
    oph_key text NOT NULL,
    prfnr text,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_group, period_year, oph_key),
    CONSTRAINT ck_sap_oph_transaction_month
        CHECK (sap_month IS NULL OR sap_month ~ '^20[0-9]{2}-(0[1-9]|1[0-2])$')
);

CREATE INDEX IF NOT EXISTS ix_gold_sap_oph_transaction_scope
    ON gold.sap_oph_transaction_snapshot
       (period_year, source_group, sap_month, prfnr, oph_key);

COMMENT ON TABLE gold.sap_oph_transaction_snapshot IS
    'Snapshot satu company + satu OPH SAP untuk denominator dan coverage foto KP/KT dashboard Harmony.';
