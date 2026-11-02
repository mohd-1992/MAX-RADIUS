# 2.6.6 point4 — consumption history retention,26October2026

Human approved the plan after confirming that ordinary account/batch deletion already retains sales ledgers. The implemented change reuses that financial protection and adds report-only consumption retention. Source-bound local development5122 only; main5090, customer/domain services and remote images remain unchanged.

## Contract and integration

`wisp_traffic_history` stores session identity, original and logical account names/IDs, the reseller known when captured, packet NAS/protocol/internal-server fields, dates and counters, plus account display metadata. It has no cascading references to accounts/packages/NAS. A framed JSON identity hashed with SHA256 forms its unique key. Anonymous rows without UID/session-ID/start retain their source/row fallback identity rather than merging unrelated records.

`traffic_history_service` copies and verifies in the caller's transaction. Selected physical session rows stay locked until commit. Original identity/point fields must agree, and saved counters/timestamps must cover the originals. Repeated saves keep greatest counters without turning NULL into zero; metadata conflicts fail closed. There is no background telemetry collector or financial/RADIUS trigger change.

- Account deletion saves all current/archive/imported sessions before deleting account rows or RADIUS credentials. Invoice unlinking and financial ledger retention retain their prior behavior.
- Batch deletion saves cards in bounded groups of250, within the existing transaction and online/reservation guards. Existing refund semantics are unchanged.
- History cleanup saves and verifies each selected batch before removing original `radacct` rows. A failed batch rolls back; already successful earlier batches remain safely preserved.
- Archiving retains its strict original copy verification and also saves a report snapshot before removing source rows. Replayed archives now preserve NULL octet counters instead of inventing zeros. Sessions lacking every stable identity field stay in the source during archiving; normal protected cleanup can preserve them with their source fallback.
- Replacement database import captures previous session history in bounded reads inside its transaction before clearing current account/session tables. This is not a replay of an import or financial packet driver.

Network reports and the earlier session-traffic chart read live/archive/imported/retained histories together without duplicate sessions. Historical sales-source recovery can also read retained evidence. Current quota calculation and FreeRADIUS accounting still use their original physical accounting paths; the report history does not reintroduce expired-cycle usage into current quotas.

Reseller reads prefer the frozen owner for captured session identities. Current voucher ownership is used only when there is no captured identity; deleting or reassigning a username therefore does not transfer captured consumption to another reseller. Rename services update only logical ownership fields across all four stores, preserving original packet names and the stable identity. Retired account names/types are displayed without links to deleted account pages.

## Schema, backup and reset

The additive table is in bootstrap SQL and the accounting installer/healer path; its report view is rebuilt idempotently. The normal SQL backup enumerates all base tables and includes this history. Explicit restoration of an older backup without the table clears newer retained history, preventing cross-backup identity/data contamination. Factory reset explicitly includes the new table. These intentional restore/reset operations are distinct from ordinary retention protection.

## Evidence and limits

Sixteen MariaDB checks pass on the separately cloned `traffic266_history_fixture`: account/batch/subscriber deletion, invoice/sale preservation, global/reseller totals and server breakdown/top10/earlier traffic-chart equality, archive/prune/repetition, NULL replay, anonymous identity, corrupted metadata rejection, injected native INSERT failure/rollback, concurrent/idempotent saves, logical rename, artificial username reassignment isolation, repeated schema install, old-backup clear rollback and replacement-capture rollback. Virtual-import replacement capture is tested independently; a complete User Manager import, full backup restore and full factory reset were not replayed.

The initial native round found an ambiguous INSERT/UPDATE column reference and an ASCII/UTF8 key comparison; both were corrected and the completed native matrix passes. Existing report regression:42 pure cases (network/sales/source attribution/list ordering),9 virtual MariaDB/HTTP cases and5 package-grouping cases pass.

On the independent development clone, the new empty schema was installed after hashing16 original business/accounting tables. All full SHA256 row digests/counts remained identical after DDL. All-time session count remains102495. Sample global report took about2.56s and a reseller sample3.88s locally; these are observations, not latency guarantees. Private evidence: TRAFFIC-HISTORY-SCHEMA.private.json and TRAFFIC-RETENTION-LAB.private.json under PRIVATE/sales-history-266-20261025-r1/lab.

This preserves histories still present when protected operations run. It does not recreate counters already deleted before this change, infer unknown former reseller ownership, or protect against an external SQL administrator dropping data. No live router/CoA/authentication test was needed or performed for this report-retention change. No customer monitoring was resumed. Final implementation is ready for human review on5122; release image consolidation/deployment remains separate.
