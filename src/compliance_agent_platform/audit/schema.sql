-- Append-only audit trail for the sanctions-screening agent.
--
-- This table is deliberately separate from LangGraph's checkpoint tables: it is the
-- compliance system-of-record and must be immutable. The application role is granted
-- INSERT/SELECT only (see grants at the bottom); corrections are new rows that point
-- at the superseded row via `supersedes_id`.

CREATE TABLE IF NOT EXISTS screening_audit (
    id             BIGSERIAL   PRIMARY KEY,
    alert_id       TEXT        NOT NULL,
    thread_id      TEXT        NOT NULL,
    checkpoint_id  TEXT,
    step           TEXT        NOT NULL,   -- intake | enrich | evaluate | escalate | dispose
    actor          TEXT        NOT NULL,   -- agent | analyst | system
    event          TEXT        NOT NULL,   -- node_completed | disposition | interrupt_raised | resumed
    payload_hash   TEXT,                   -- sha256 of the node input state
    llm_request    JSONB,
    llm_response   JSONB,
    model_id       TEXT,
    prompt_version TEXT,
    detail         JSONB,
    supersedes_id  BIGINT      REFERENCES screening_audit (id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_screening_audit_alert ON screening_audit (alert_id, created_at);
CREATE INDEX IF NOT EXISTS ix_screening_audit_thread ON screening_audit (thread_id, created_at);

-- Hash chain (Week 2): each row commits to the previous row's record_hash, so a
-- deleted or reordered row breaks the hash of everything after it. Nullable at the
-- DB level because rows written before this migration have neither value; every row
-- written through `write_audit` populates both, starting from a fixed genesis hash.
ALTER TABLE screening_audit ADD COLUMN IF NOT EXISTS prev_hash TEXT;
ALTER TABLE screening_audit ADD COLUMN IF NOT EXISTS record_hash TEXT;

-- Immutability guard: block UPDATE/DELETE even for roles that hold those privileges.
CREATE OR REPLACE FUNCTION screening_audit_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'screening_audit is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS screening_audit_no_mutate ON screening_audit;
CREATE TRIGGER screening_audit_no_mutate
    BEFORE UPDATE OR DELETE ON screening_audit
    FOR EACH ROW EXECUTE FUNCTION screening_audit_immutable();

-- Least-privilege grants for the application role (adjust role name per environment).
-- REVOKE ALL ON screening_audit FROM cap_app;
-- GRANT INSERT, SELECT ON screening_audit TO cap_app;
-- GRANT USAGE, SELECT ON SEQUENCE screening_audit_id_seq TO cap_app;
