-- Migration: V6.6.0 — origin binding for agent memory
-- Date: 2026-10-30
--
-- Adds the provenance marker that the 2026 memory-security literature says
-- is the ONLY defence that holds.
--
-- Why this exists
-- ---------------
-- Persistent agent memory is an attack channel, and the numbers are stark:
--   * a malicious instruction planted via one untrusted tool call fires in
--     later sessions with 55.5% success against OpenClaw and 81.7% against
--     Claude Code (cross-session attack success);
--   * a sleeper implant from one tool call fires with 85-100% success and
--     STILL FIRES after 100 ordinary sessions;
--   * prompt-level filters cut poisoning by at most 3.2 percentage points,
--     while tool permissions plus provenance tracking take it from 94% to
--     11%, and binding the origin AT WRITE TIME takes it to 0%.
--
-- Cortex already had a `memory_history` audit trail (11961 rows over 4317
-- memories on 127.0.0.1) recording action + content. But it records
-- *what changed*, not *where the claim came from* — and it is not carried
-- on the record itself. A fact rewritten by consolidation arrives with no
-- trace of where the original observation came from, which is exactly the
-- "provenance laundering" mechanism: an external observation is rewritten
-- as if it were part of the user's own history.
--
-- So the marker is bound to the RECORD, not to the log.
--
-- Columns
-- -------
--   origin       VARCHAR(32)  -- 'user' | 'agent' | 'tool' | 'external' | ...
--   origin_trust VARCHAR(16)  -- 'user' | 'derived' | 'external'
--   origin_ref   TEXT         -- concrete handle (url / message id / tool call id)
--
-- Backfill
-- --------
-- Existing rows are 'agent' + 'derived'. They were all written by this
-- service's own consolidation pipeline from the user's own messages, so
-- 'derived' is the honest default; 'user' would over-claim, and
-- 'external' would under-claim for rows that did come from tools.
-- The distinction only becomes load-bearing for NEW writes, which is
-- where the attack surface is.

ALTER TABLE memory_chunks
    ADD COLUMN IF NOT EXISTS origin VARCHAR(32) DEFAULT 'agent';
ALTER TABLE memory_chunks
    ADD COLUMN IF NOT EXISTS origin_trust VARCHAR(16) DEFAULT 'derived';
ALTER TABLE memory_chunks
    ADD COLUMN IF NOT EXISTS origin_ref TEXT;

-- Backfill (idempotent).
UPDATE memory_chunks SET origin = 'agent' WHERE origin IS NULL;
UPDATE memory_chunks SET origin_trust = 'derived' WHERE origin_trust IS NULL;

-- Provenance lookups: "everything that came from outside the trust
-- boundary", and "audit one fact's origin".
CREATE INDEX IF NOT EXISTS memory_chunks_origin_idx
    ON memory_chunks(origin_trust) WHERE archived = 0;

-- The DERIVED facts need the marker too — this is the anti-laundering
-- mechanism. A fact extracted from a source chunk inherits that chunk's
-- origin and may never be MORE trusted than it, so an observation from
-- outside the trust boundary stays 'external' after consolidation instead
-- of being rewritten into something that reads like the user's own words.
ALTER TABLE memory_extracted_facts
    ADD COLUMN IF NOT EXISTS origin VARCHAR(32) DEFAULT 'agent';
ALTER TABLE memory_extracted_facts
    ADD COLUMN IF NOT EXISTS origin_trust VARCHAR(16) DEFAULT 'derived';
ALTER TABLE memory_extracted_facts
    ADD COLUMN IF NOT EXISTS origin_ref TEXT;

UPDATE memory_extracted_facts SET origin = 'agent' WHERE origin IS NULL;
UPDATE memory_extracted_facts SET origin_trust = 'derived' WHERE origin_trust IS NULL;

-- Down migration (commented for reference, do not run unless rolling back):
--   DROP INDEX IF EXISTS memory_chunks_origin_idx;
--   ALTER TABLE memory_chunks DROP COLUMN IF EXISTS origin;
--   ALTER TABLE memory_chunks DROP COLUMN IF EXISTS origin_trust;
--   ALTER TABLE memory_chunks DROP COLUMN IF EXISTS origin_ref;
