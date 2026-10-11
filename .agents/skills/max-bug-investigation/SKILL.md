---
name: max-bug-investigation
description: Trace MAX RADIUS failures using routes, financial records, radacct and NAS evidence without repeating uncertain operations.
---

Start from one reproducible account/session/operation and its timestamp, with credentials kept private. Use [PROJECT_MAP](../../../docs/PROJECT_MAP.md) to trace handler to service and storage. Separate expected expiry/license rejection, tool failure, API reachability, RADIUS reply and accounting settlement. Compare router identity/counters to every session in the account cycle. A timeout can follow a committed financial operation: inspect its audit/journal before replaying it. Use [CURRENT_STATE](../../../docs/CURRENT_STATE.md) to avoid relabeling known faults as new discoveries. Report confirmed cause or bounded uncertainty and targeted correction; do not repair unrelated live systems automatically.
