# CLIENT_AS: read-only readiness for 2.6.9 — 27 October 2026

Target: 192.168.33.33 / CLIENT_AS, verified pinned hardware identity. Checked at 18:26 UTC (21:26 Asia/Aden). Current authorization: inspection and report only; no offer, enrollment, apply, SQL mutation, restart, backup generation or guard change.

## Decision

**Not ready for a direct OTA announcement/install in its current configuration.** This is a missing installation-specific controller/channel and deployment reconciliation, not a demonstrated defect in the release. Local sequence tests cover a representative installation, not this customer's current Compose layering and Core configuration.

## Live evidence

- Actual Web `sha256:5eda6a3450ddef586478153082395fbeb73f661be0fcf73b47bffd1e02559d38` and Core `sha256:7bf2baa71525ea79e866daeb063c35346f39054fbf81ae209107f632681c1a5e` match the documented 25 October 2.6.5 rollout images. Five services running healthy, restart count zero. Hardware and Core binding verified.
- Two natural-traffic samples about 36 seconds apart: NAS/SQL 195/196 initially and 201/201 subsequently. Initial mismatch means incomplete first-sample coverage; a single transient mismatch is not a confirmed outage. Natural Access-Accept increased by 30; 24 new accounting IDs, 35 advancing sampled sessions, no observed counter decrease, identity change or reopening. Both lease samples valid/fresh. No synthetic authentication or disconnect.
- Source is compiled protected modules. The initial probe incorrectly assumed config.py and was corrected to inspect compiled packaging; neither failed read changed runtime.
- No update-channel.private.json in Web storage, no MAX_UPDATE_SOCKET setting, no default controller socket. Running services contain no update-controller. Only old one-shot/guard unit files were found, disabled; guard inactive as requested. Do not reuse or reactivate them for OTA.
- A read-only execution of the accepted baseline attestation verified current license-auth/reservation SQL objects and absence of MAC policy tables: features267 baseline compatible. This is a schema baseline assertion, not a migration performed or full customer acceptance of 2.6.8.
- Core has modified query and license-postauth configuration inside its writable layer. These must be reconciled and preserved appropriately; a generic recreation could lose installation-specific behavior.
- Compose labels reference the production base plus multiple historical override files. A single generic compose-up is inappropriate without reconciling the effective configuration, project/service identity, storage, startup and protected dependencies.
- Disk: 28.26 GiB free of 47.93 GiB. Database table/index estimate 358,842,368 bytes across 71 tables. Existing storage backup metadata: 56 files, 1,060,648,663 total bytes, latest around 18:00 UTC. File count includes backup-related files; it is not a count of independently verified restore points. No new checksum/decrypt/restore test was performed.
- Historical previous Web/Core containers remain stopped. They are the fallback for the prior 2.6.5 rollout, not certification of rollback after new features or cleanup. All pre-existing container identities/configuration/runtime snapshots stayed unchanged during comparison.

## Required work before a separately authorized release

1. Prepare a dedicated local reader and installation-owned OTA controller with pinned trust, scoped identity, shared socket and effective Compose configuration; preserve Web/Core customization and storage. Do not reuse old one-shot execution/guard.
2. Verify normal app backup/restoration packaging and take a fresh verified backup before switching; preserve rollback images/configuration and current accounting. No DBrestore as automatic rollback.
3. Rehearse this customer's effective configuration locally with 2.6.5 → 2.6.6 → 2.6.7 → 2.6.8 → corrected 2.6.9, including schema-first Core startup and compatible rollback. Accept real RADIUS/SQL test evidence in the isolated reproduction; then observe natural traffic when any live installation is later authorized.
4. Only after acceptance, publish a client-specific offer. Do not target a hosted domain identity or rely on version/latest alone.

Scope limits: short live sample does not guarantee all financial transactions, every packet or future availability. No current customer-specific upgrade/rollback rehearsal or restore test was done in this read-only round. No monitor was resumed.

Evidence: `PRIVATE/client269-readiness-20261027-r1` (protected, no secrets/accounts/raw sessions in report). Related: [prior customer rollout](CLIENT_AS_UPDATE_20261025.md), [isolated local sequence acceptance](UPDATE_SEQUENCE_LOCAL_20261026.md), [corrected 2.6.9 acceptance](BACKUP_269_BUILD_LOCAL_20261027.md).
