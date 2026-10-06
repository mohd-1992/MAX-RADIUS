"""Persistent progress for maintenance operations; no accounting data."""
MAINTENANCE_JOBS_DDL = """
CREATE TABLE IF NOT EXISTS wisp_maintenance_jobs (
 job_id VARCHAR(32) PRIMARY KEY,
 active_key VARCHAR(64) NULL UNIQUE,
 state VARCHAR(20) NOT NULL,
 parameters LONGTEXT NOT NULL,
 progress LONGTEXT NOT NULL,
 created_by VARCHAR(64) NOT NULL,
 created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
 INDEX idx_maintenance_updated (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""
