# Hosted domains memory incident — 27 October 2026

Human authorized urgent service recovery. Initial SSH connections timed out/reset; hosted pages and control API timed out. No change was possible during that interval. Connectivity subsequently returned before containment.

## Confirmed cause and recovery

Kernel journal at 18:31:24 UTC (21:31:24 Asia/Aden) records global OOM and killing Chromium PID3523197 with 5,902,632 KiB anonymous RSS (~5.63 GiB). Its cgroup maps exactly to `max_radius_m_web`, image c5ab9fe42359. This was real RAM/swap pressure, not merely Linux cache or disk fullness. Web remained running with restart count0; OOMKilled=true indicates the child-process event, not a full Web restart. All hosted production containers inspected were healthy. An older isolated lab Web was unhealthy separately and was left untouched.

Chromium appears in the source PDF renderer in services/batch_export_service.py. The precise triggering user/export payload has not been established. No claim that all 2.6.9 processes leak or that an update installation itself caused the event.

## Emergency containment applied

Pinned container ID/image/project and healthy/running state; prior Memory=0/MemorySwap=0 saved privately. Applied `docker update --memory 2g --memory-swap 3g` to M Web alone. New 2 GiB RAM / 3 GiB combined RAM+swap limits verified, image/StartedAt/restart count unchanged. No restart, database modification, image/channel update, cache flush or swapoff. Runtime change is reversible and survives for this container; replacement/recreation can lose it because Compose has not been edited. It protects host capacity but can cause a large render to fail within M; it does not fix PDF generation or guarantee Web availability under future cgroup OOM.

## Post-containment evidence

At18:33:46UTC: host available RAM5526MiB, M Web218.2MiB, memory pressure trending down. In a short M SQL comparison, one new accounting ID and8advancing rows, zero decreases; lease valid/fresh. No new successful authentication in that short window, so no new-auth acceptance claim. Subsequent requests with a normal browser User-Agent returned HTTP200 login pages for M/SHN/4K/MAX1 in0.74–0.99sec. Initial host-side urllib requests got HTTP errors (filtering/user-agent path), not timeouts; not mistaken for a recovered authentication test.

## Remaining work

Identify the exact PDF/cards export workload, reproduce safely in isolation, bound renderer memory/work size/concurrency and execution time, and retain reviewed limits in effective deployment. Do not trigger another large live export as a test. No recurring monitor created. Backup/runtime/old releases remain intact. Exact rollback to old unrestricted memory settings is recorded privately; do not remove containment without reviewing host risk.

[Public incident receipt](../releases/incident-20261027-host-memory-containment.json). Protected evidence: `PRIVATE/host-memory-audit-20261027-r1`; remote containment journal under its separately named manual operation. Do not replay mutation script.

## SHN/MAX1 follow-up — 18:41 UTC

Human prioritized these two domains before investigating the suspected subscriber session export. Read-only checks: both login pages HTTP200 in0.54–0.62sec; all eight production Web/Core/DB/controller containers healthy/running, restart0 and no OOM flag. SHN actual version2.6.7; MAX1actual2.6.9. Both runtime leases valid/fresh.

Two SQL samples25sec apart: SHN+2natural Access-Accept,+2accounting IDs,18advancing rows; MAX1no new acceptance/accounting IDs,10advancing rows. No counter decrease in either bounded recent500row comparison. Lack of new MAX1auth is a coverage limit, not an outage. Global instantaneous memory pressure had fallen to0in the separate host recheck.

The deeper NAS collector transport did not complete and was canceled locally; no NAS/SQL matching claim for this round. A small independent read-only SQL check supplied the accepted evidence. No changes, restarts, update installs or recurring monitors in either domain, and no subscriber export triggered or inspected. Evidence: `PRIVATE/shn-max1-post-memory-20261027-r1/REPORT.private.json` and associated protected files. Do not run the stalled transport again without debugging it.

## Session export attribution completed

[Read-only correlation report](M_SESSION_EXPORT_OOM_20261027.md): target subscriber sessions.pdf request499/0bytes21:29:18Aden, kernel Chromium OOM21:31:24, then actual session-export Page.pdf failure21:31:25.936. Current history11,715unique sessions, mostly imported. No export repeated or Chromium started. Bounded/chunked and isolated renderer fix remains unimplemented.
