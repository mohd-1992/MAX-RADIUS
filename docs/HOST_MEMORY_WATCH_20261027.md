## Monitoring paused by the human

The human requested stopping the checks. App automation `automation` is PAUSED; no more scheduled samples or automated interventions are authorized until a new human request. Existing M Web memory containment is unchanged. No host-side guard was installed by this watch.

# Hosted domains memory watch — 27 October 2026

User requested live measurement and ongoing monitoring with intervention for confirmed service-threatening faults. Scope is the pinned hosted server only, domains AS/MAX/4K/M/MAX1/SHN and existing license/control systemd services. No access to CLIENT_AS192.168.33.33; old paused monitors remain paused.

Private runtime: `PRIVATE/host-memory-watch-20261027-r1/check.private.py`. It invokes a read-only collector via existing protected SSH, validates HOST-LOCK, samples host memory/PSI, running Docker containers/stats, bounded latest100session counters and latest1500auth-ID evidence, license lease, HTTPlogin pages/control csrf endpoint, systemd unit health/memory, last3minute kernel OOM and live Chromium PID/RSS/container binding. No app imports, NAS queries, auth tests, exports, Docker mutations or SQL writes. Private sample rows contain IDs/counters, not usernames/credentials. All published results are aggregates. Peak tracker records observed working-memory maxima; sparse samples are not true peak guarantees and historical cgroup peak may be unavailable.

Three initial samples around22:08–22:11Aden: all six login pages and control csrfHTTP200; both systemd services active with0restarts. No new OOM, instantaneous memory pressure0, available RAMroughly4.4GiB. Natural acceptance/session IDs and sampled counters advanced inM/MAX1/SHN without decreases. AS/MAX/4Khad no new movement in these windows; this is not a failure. All leases valid/fresh. Existing zombie Chromium processes have0RSS and are excluded from live-renderer alarms; no live Chromium in final sample.

Initial working memory totals across each domain's running containers (MiB, rounded, variable): AS187, MAX276,4K192,M1095,MAX1481,SHN824. Corresponding Web/DBworking MiB: AS91/93,MAX191/83,4K87/93,M645/428,MAX1109/354,SHN130/592. Shared control~25MiB and license~22MiB from systemd accounting; those figures do not include every proxy/transport/system expense. Free/available RAM is the host metric, not summed container subtraction.

No new memory caps or Compose changes applied. M Web retains intentional2GiBRAM/3GiBcombined emergency cap. Other production containers remain as measured. These short baseline figures are not enough to select safe permanent caps; continue observing ordinary workload and backups/peaks without live stress exports.

## Automation and intervention scope

App heartbeat automation `automation`, name مراقبة ذاكرة خادم الدومينات واستمرار الخدمة, created ACTIVE with requested one-minute cadence, thread destination. This is a best-effort app-scheduled observation, not a resident host guard or guaranteed immediate protection. No constant host-side service was installed. Quiet on stable state, deduplicate alerts using private LAST-NOTIFIED. Notify meaningful faults, monitoring loss or any intervention/result.

The collector never repairs. Any agent intervention requires two independent indicators, a confirming reading, pinned current identity and evidence before/after. First distinguish human-driven updates/migrations from failure. Authorized minimal containment can target only the attributed live runaway Chromium export with oneSIGTERM after rechecking PID/starttime/cgroup, or one start/restart of the same existing failed tenant Web/Core after excluding an intentional switch. No repeated/blind actions, no container recreation or composeup. Unknown cause/failed recovery escalates. Never restart shared license/control/DB automatically, alter SQL/data/NAS/trust/network/channel/images/limits or run DBrestore/CoA/test authentication/exports. Preserve evidence and verify observed normal accounting and new authentication when available; state any absent traffic/coverage.

## Reproduction and evidence

Run the private check once per wake and inspect SUMMARY/LAST plus prior private observations. Updated scripts compiled and the final live invocation succeeded. First sample establishes identity baseline; subsequent invocations validate it. No old automation resumed. Evidence stays outside Git; no credentials/raw account logs in this document. Related: [incident](HOST_MEMORY_INCIDENT_20261027.md), [confirmed session-PDF attribution](M_SESSION_EXPORT_OOM_20261027.md).
