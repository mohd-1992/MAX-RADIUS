-- ============================================================================
-- Migration: 20261003_cycle_baselines.sql
-- Purpose: Introduce wisp_session_baselines for reliable cycle boundaries.
-- Eliminates double-charging of sessions spanning package renewal or voucher recharge.
-- ============================================================================

CREATE TABLE IF NOT EXISTS `wisp_session_baselines` (
  `radacctid` bigint(21) NOT NULL,
  `username` varchar(64) NOT NULL,
  `baseline_input_bytes` bigint(20) unsigned NOT NULL DEFAULT 0,
  `baseline_output_bytes` bigint(20) unsigned NOT NULL DEFAULT 0,
  `baseline_bytes` bigint(20) unsigned NOT NULL DEFAULT 0,
  `baseline_seconds` int(10) unsigned NOT NULL DEFAULT 0,
  `renewed_at` datetime NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`radacctid`, `renewed_at`),
  KEY `idx_username_renewed` (`username`, `renewed_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
