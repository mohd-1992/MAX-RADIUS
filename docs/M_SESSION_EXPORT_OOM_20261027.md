# M session-PDF OOM investigation — 27 October 2026

Scope: read-only investigation for the human-specified subscriber on M. No PDF export, HTML rendering, Chromium launch, synthetic authentication, restart, SQL mutation, channel change or deployment.

## Finding

The evidence strongly identifies the subscriber's full session-history PDF export as the workload behind the host memory exhaustion. This is supported by the account-specific access request, actual application failure stack and kernel cgroup/PID evidence; not merely the fact that the renderer uses Chromium. No request-to-renderer-PID correlation ID was instrumented, so absolute exclusion of every unlogged parallel task is not claimed.

## Timeline (Asia/Aden)

- **21:29:18:** M Nginx records the target subscriber's sessions.pdf request ending with499 and0response bytes. The explicit host is M. Only this one M session-PDF request was found in the inspected21:10–21:40window.499 means the client-side connection closed; it is not a successful completed download or proof of a user clicking cancel.
- **21:31:24:** kernel global OOM kills Chromium PID3523197. The preceding OOM cgroup maps exactly to current M Web4e5ddea7ab0e. Anonymous RSS5,902,632KiB (~5.63GiB).
- **21:31:25.936:** M Web records `Session PDF export failed`. Stack: session_export.py:14 → batch_export_service.render_pdf → Playwright Page.pdf. The failure is in PDF generation, after page content preparation.

The later failure is consistent with expensive rendering continuing after the HTTP client disconnected. No new export was used to reproduce this.

## Workload and source explanation

A bounded read-only aggregate found84rows in live retained accounting and11,632imported historical rows, with0archive/preserved-history rows for this username. The actual deduplicated history SQL counted **11,715sessions** at inspection. Counts are current inspection counts, not a frozen dump at incident time. No raw session/account data is published.

`web/session_export.py` explicitly requests `limit=None`, formats the complete history into one HTML document and calls the shared PDF renderer. The11column session table is printed as one Page.pdf operation. The renderer's load/content timeouts do not establish a bounded memory allocation or an explicit overall deadline for that PDF printing call. A process-local semaphore does not isolate memory from the host or provide a fleet-wide renderer limit.

This implicates the resource handling of large full-history PDF exports; the evidence does not show database corruption, accounting failure, or a general failure of the update channel. The subscriber's small recent/live history alone understates the imported-history print workload.

## Recommended fix (not implemented in this report)

Preserve exporting the complete history, but generate bounded chunks/pages and merge outputs, with independently limited renderer memory, bounded execution duration and reliable child cleanup/cancellation. Enforce appropriate cross-worker concurrency. Avoid another large live export until isolated tests validate complete output, Arabic layout, historical deduplication, forced timeout/worker failure cleanup and peak RAM bounds. Do not silently drop old sessions to reduce workload.

The previous M-only2GiBRAM/3GiBcombined cap remains emergency host containment, not this permanent fix. No additional live settings changed in this investigation.

Private evidence: `PRIVATE/m-session-export-oom-20261027-r1`. Related: [host incident and emergency containment](HOST_MEMORY_INCIDENT_20261027.md), [export handler](../web/session_export.py), [PDF renderer](../services/batch_export_service.py), [history read model](../services/session_history_service.py).
