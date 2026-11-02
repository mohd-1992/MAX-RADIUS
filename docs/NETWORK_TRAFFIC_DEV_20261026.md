## Follow-up — top10 consumers,26October2026

Added a table of the ten largest recorded download+upload consumers, combining subscribers/cards in one ranking within the exact selected session-start period and NAS/protocol/internal-server filters. All sessions for an exact username are combined across selected points after archive deduplication. Stable ties use username; unavailable complete totals and blank account names are not invented as zero. Partial readings carry coverage badges. Current subscriber names/card types and permission-checked detail links are resolved only for the ranked ten; missing historical accounts remain visible as historical records. Duplicate current account identities stay unclassified and do not multiply bytes. Restricted reseller requests only resolve their voucher identities.

Verification: nine native virtual-accounting/HTTP cases now pass, including >20 virtual accounts/top10 limit, numeric ordering, filtering before ranking, cross-point aggregation, archive deduplication and unavailable/owner cases. Six pure network checks pass. The existing isolated clone's ten ranked usernames, totals and session counts match an independent SQL aggregation; about2.76s for the complete report in this sample. No seeds or accounting mutations. Browser empty-state screenshots intentionally avoid account lists.

# 2.6.6 point3 — session-start network consumption

Human accepted point2 and the subscriber/card/batch sorting additions on26October2026, then authorized point3. When asked about long sessions crossing dates, the human selected **lifetime consumption of sessions that started in the selected period, using existing history**. This is the implemented contract; it is not an interval-delta measurement.

## Implementation

The existing sales endpoint has a second report view, `report_view=traffic`, with independent period, NAS, packet service and internal-server filters. Today is the default. Custom end dates are exclusive at the following local midnight, converted to the existing storage timezone. Financial package/reseller/ledger filters do not determine network byte attribution; request-bound reseller ownership still restricts visible sessions and catalogs.

`network_traffic_service` reads the live, archived and imported ownership-aware accounting views in one consistent read-only transaction. Duplicate copies use the complete UID/username/session-ID/NAS/start identity and greatest nonnegative upload/download counters. Empty identities without a start use source/row fallback IDs instead of guessing that unrelated rows are the same session. Synthetic consolidated import sentinels are excluded. Conflicting packet source fields stay unknown. Hotspot bridge ports share an internal server; PPPoE service-name plus interface distinguish servers on the same NAS. No current package or later sale is used to infer historical traffic sources.

NAS names are resolved for display from the current device catalog, then `nas.shortname`, with IP fallback. Unknown sources retain their bytes in an unknown bucket scoped to NAS/protocol. Missing or negative counters stay unavailable, with partial-count badges; they do not become zero. All row totals reconcile to the same filtered session set. Open means no Accounting-Stop is recorded; it does not claim a live NAS connection.

The tab includes upload/download/total/session cards, a bar chart and a sortable table with last recorded counter timestamp. It shares the existing sales styles and Arabic theme/RTL system. Sort choice is browser-local per admin and report, with fixed allowed keys; all rows sort together. The chart shows the top12 server rows plus a remainder bucket when needed, and adapts byte units and theme colors. All rows remain in the table. The existing financial report contract and exports are unchanged.

## Verification

- Six pure network contracts plus15 existing sales and6 list-sort contracts pass (27).
- Seven native MariaDB/HTTP cases use process-local virtual accounting relations on the existing isolated synthetic DB: archive/import duplicate removal, reused UID identity, protocol/interface attribution, start-date boundaries, missing dates/counters, conflicting source evidence, sort/filter reconciliation, positive and empty owner scope, escaping, chart totals, invalid filters and deliberate unavailable503 handling. No new accounting seeds or RADIUS packet rounds.
- Four shared-list native HTTP cases remain passing after the shared sort-script parameterization.
- Existing isolated local clone: all-time102495 deduplicated sessions across7 report points; count and byte totals match an independent identity-grouped query. Report read took about1.82s in this local sample, not a latency guarantee. Today's clone contains no matching sessions, and returns an empty report consistently.
- Browser: tab navigation, PPPoE filter (two synthetic points), sorting and persistence, desktop/mobile390px, light/dark. Private screenshots use synthetic point data, no account lists.

The test runner disables workers, activation sync where applicable and automatic schema healing in its process. The report itself starts a read-only transaction and has no schema, financial, contract, NAS, license or session mutation. No customer/domain was contacted. Customer monitoring24 remains deleted.

## Delivery and limits

Available only on source-bound local development5122 with normal login: `/vouchers/sales?report_view=traffic`. Main5090 and compiled images were not replaced. No Registry/Git deployment. Private evidence is under PRIVATE/sales-history-266-20261025-r1/lab/NETWORK-TRAFFIC-* and ui-network-traffic-*.private.png.

Counters for an open session can increase after its start period has passed, because the selected contract is lifetime session counters. The report does not reconstruct per-day traffic inside a long session, measure router interface traffic outside RADIUS, or prove every router transaction. Retention is limited to accounting histories still available; comprehensive deletion/archive protection remains point4. Point4 is not started until human acceptance of this point.
