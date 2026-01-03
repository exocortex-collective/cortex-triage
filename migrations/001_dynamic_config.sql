-- Migration 001: Dynamic Config Management
-- Creates tables for database-driven configuration with hot reload capability
--
-- Implements:
-- - cortex-c2f: Config Storage Schema
-- - cortex-wlm: Worker Signal Queue
-- - cortex-ios: Triage Config Reload Handler

-- ============================================================================
-- Config Versioning and Storage
-- ============================================================================

-- Main config versions table
-- Stores snapshots of complete configuration with JSONB for complex objects
CREATE TABLE IF NOT EXISTS triage_config_versions (
    version SERIAL PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    config_hash TEXT NOT NULL UNIQUE,  -- SHA256 of YAML content for dedup

    -- Config metadata
    label_prefix TEXT NOT NULL DEFAULT 'Cortex',

    -- JSONB fields for complex config objects
    intents JSONB NOT NULL DEFAULT '{}'::jsonb,
    email_categories JSONB NOT NULL DEFAULT '{}'::jsonb,
    prompts JSONB NOT NULL DEFAULT '{}'::jsonb,
    body_extraction_prompts JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Ensure only one active config at a time
-- Uses exclusion constraint to enforce this atomically
CREATE UNIQUE INDEX IF NOT EXISTS idx_config_only_one_active
    ON triage_config_versions (is_active)
    WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_config_hash
    ON triage_config_versions(config_hash);

CREATE INDEX IF NOT EXISTS idx_config_created_at
    ON triage_config_versions(created_at DESC);

-- Trigger to auto-deactivate old versions when a new one is activated
CREATE OR REPLACE FUNCTION deactivate_old_config_versions()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.is_active = TRUE THEN
        UPDATE triage_config_versions
        SET is_active = FALSE
        WHERE version != NEW.version AND is_active = TRUE;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_deactivate_old_configs ON triage_config_versions;
CREATE TRIGGER trg_deactivate_old_configs
    BEFORE INSERT OR UPDATE ON triage_config_versions
    FOR EACH ROW
    WHEN (NEW.is_active = TRUE)
    EXECUTE FUNCTION deactivate_old_config_versions();

-- Email mappings (priority and fallback)
-- Normalized to lowercase for case-insensitive matching
CREATE TABLE IF NOT EXISTS triage_email_mappings (
    id BIGSERIAL PRIMARY KEY,
    config_version INT NOT NULL REFERENCES triage_config_versions(version) ON DELETE CASCADE,
    mapping_type TEXT NOT NULL CHECK (mapping_type IN ('priority', 'fallback')),
    email_address TEXT NOT NULL,  -- Stored as lowercase
    label TEXT NOT NULL,
    archive BOOLEAN,
    mark_read BOOLEAN,

    CONSTRAINT unique_email_per_version UNIQUE (config_version, mapping_type, email_address)
);

CREATE INDEX IF NOT EXISTS idx_email_mappings_version
    ON triage_email_mappings(config_version);
CREATE INDEX IF NOT EXISTS idx_email_mappings_email
    ON triage_email_mappings(email_address);

-- ============================================================================
-- Rule Chains (Linked List Structure)
-- ============================================================================

-- Chain metadata (container for rules)
CREATE TABLE IF NOT EXISTS triage_chains (
    id BIGSERIAL PRIMARY KEY,
    config_version INT NOT NULL REFERENCES triage_config_versions(version) ON DELETE CASCADE,
    chain_name TEXT NOT NULL,
    display_order INT NOT NULL DEFAULT 0,  -- For UI ordering

    CONSTRAINT unique_chain_per_version UNIQUE (config_version, chain_name)
);

CREATE INDEX IF NOT EXISTS idx_chains_version
    ON triage_chains(config_version);

-- Individual rules within chains
-- Uses doubly-linked list (prev_rule_id/next_rule_id) for O(1) insertion
CREATE TABLE IF NOT EXISTS triage_rules (
    id BIGSERIAL PRIMARY KEY,
    chain_id BIGINT NOT NULL REFERENCES triage_chains(id) ON DELETE CASCADE,
    config_version INT NOT NULL REFERENCES triage_config_versions(version) ON DELETE CASCADE,

    -- Linked list pointers
    prev_rule_id BIGINT REFERENCES triage_rules(id) ON DELETE SET NULL,
    next_rule_id BIGINT REFERENCES triage_rules(id) ON DELETE SET NULL,

    -- Rule content (JSONB for flexibility)
    match_condition JSONB NOT NULL DEFAULT '{}'::jsonb,
    variables JSONB,  -- Variable definitions
    action JSONB,  -- Action to take
    jump_to_chain TEXT,  -- Jump to another chain
    return_to_parent BOOLEAN DEFAULT FALSE,  -- Return to calling chain
    llm_config JSONB,  -- LLM classification config
    routes JSONB,  -- Route mapping for LLM results

    -- Metadata (optional)
    rule_name TEXT,
    description TEXT,

    -- Optimistic locking for concurrent edits
    row_version INT NOT NULL DEFAULT 1,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rules_chain
    ON triage_rules(chain_id);
CREATE INDEX IF NOT EXISTS idx_rules_version
    ON triage_rules(config_version);
CREATE INDEX IF NOT EXISTS idx_rules_prev
    ON triage_rules(prev_rule_id);
CREATE INDEX IF NOT EXISTS idx_rules_next
    ON triage_rules(next_rule_id);

-- Trigger to bump row_version on update
CREATE OR REPLACE FUNCTION bump_rule_row_version()
RETURNS TRIGGER AS $$
BEGIN
    NEW.row_version = OLD.row_version + 1;
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_bump_rule_version ON triage_rules;
CREATE TRIGGER trg_bump_rule_version
    BEFORE UPDATE ON triage_rules
    FOR EACH ROW
    EXECUTE FUNCTION bump_rule_row_version();

-- ============================================================================
-- Worker Signal Queue
-- ============================================================================

-- Control messages for worker management
-- Used for config reload, shutdown, pause/resume, log level changes
CREATE TABLE IF NOT EXISTS worker_signals (
    id BIGSERIAL PRIMARY KEY,
    signal_type TEXT NOT NULL CHECK (signal_type IN
        ('config_reload', 'shutdown', 'pause', 'resume', 'log_level')),
    target_worker TEXT NOT NULL DEFAULT 'all',  -- 'all', 'triage', 'labeling', etc.
    payload JSONB,  -- Optional signal parameters
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending', 'processed', 'expired')),

    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,  -- Optional expiration
    processed_at TIMESTAMPTZ,
    processed_by TEXT,  -- Worker ID that processed this signal

    CONSTRAINT valid_expiration CHECK (expires_at IS NULL OR expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_signals_pending
    ON worker_signals(created_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_signals_target
    ON worker_signals(target_worker, status);
CREATE INDEX IF NOT EXISTS idx_signals_processed_at
    ON worker_signals(processed_at)
    WHERE status = 'processed';

-- ============================================================================
-- Worker Status Tracking (for heartbeat and version monitoring)
-- ============================================================================

CREATE TABLE IF NOT EXISTS worker_status (
    id BIGSERIAL PRIMARY KEY,
    worker_id TEXT NOT NULL UNIQUE,
    worker_type TEXT NOT NULL,  -- 'triage', 'labeling', etc.

    -- Current state
    status TEXT NOT NULL CHECK (status IN ('running', 'idle', 'stopped')),
    config_version_id INT REFERENCES triage_config_versions(version),

    -- Heartbeat
    last_heartbeat TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ DEFAULT NOW(),

    -- Metadata
    hostname TEXT,
    pid INT,
    version TEXT  -- Worker binary version
);

CREATE INDEX IF NOT EXISTS idx_worker_status_type
    ON worker_status(worker_type);
CREATE INDEX IF NOT EXISTS idx_worker_status_heartbeat
    ON worker_status(last_heartbeat DESC);

-- ============================================================================
-- Update classifications table to track config version
-- ============================================================================

-- Add config_version_id column to classifications
-- This allows tracking which config version was used for classification
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'classifications'
        AND column_name = 'config_version_id'
    ) THEN
        ALTER TABLE classifications
        ADD COLUMN config_version_id INT REFERENCES triage_config_versions(version);

        CREATE INDEX idx_classifications_config_version
        ON classifications(config_version_id);
    END IF;
END $$;

-- ============================================================================
-- View: Emails Needing Convergence
-- ============================================================================

-- Emails that were classified with an old config version
-- These need to be re-triaged to converge to the latest config
CREATE OR REPLACE VIEW emails_needing_convergence AS
SELECT
    c.email_id,
    c.gmail_id,
    c.config_version_id AS old_version,
    (SELECT version FROM triage_config_versions WHERE is_active = TRUE) AS new_version,
    c.classified_at
FROM classifications c
WHERE c.config_version_id IS NOT NULL
  AND c.config_version_id != (
      SELECT version FROM triage_config_versions WHERE is_active = TRUE
  );

COMMENT ON VIEW emails_needing_convergence IS
'Emails classified with old config versions that need re-triaging';

-- ============================================================================
-- Comments
-- ============================================================================

COMMENT ON TABLE triage_config_versions IS
'Versioned configuration snapshots with full history';

COMMENT ON TABLE triage_email_mappings IS
'Email address to label mappings (priority and fallback)';

COMMENT ON TABLE triage_chains IS
'Rule chain containers (firewall-style chains)';

COMMENT ON TABLE triage_rules IS
'Individual rules stored as linked list for efficient editing';

COMMENT ON TABLE worker_signals IS
'Control messages for worker management (reload, shutdown, etc.)';

COMMENT ON TABLE worker_status IS
'Worker heartbeat and status tracking';
