# Hosted server resource inventory — 27 October 2026

Human requested inventory/report before any cleanup. Read-only pinned-host SSH inspection; no stop/delete/restart, prune, cache drop, swapoff, limit change, update, SQL mutation or customer export.

## Count and memory snapshot

At~22:16–22:17Asia/Aden: **131Docker containers:26running,105exited**. Running:22production tenant containers across AS/MAX/4K/M/MAX1/SHN,3historical lab containers,1shared L2TPtransport. Production services must be retained. Exited containers use no running application RAM and include historical fallback/test resources; deleting them would affect disk/rollback, not recover current working RAM.

Host RAM7930MiB; available4381MiB, free2882MiB, file cache1604MiB and reclaimable slab116MiB. Swapused~3051MiB. Instantaneous full/some memory pressure0. These are separate categories, not additive memory savings. Used swap alone does not prove current pressure; no swapoff needed.

Tenant running-container working memory totals (MiB rounded, variable): AS192,MAX279,4K195,M1180,MAX1485,SHN826. Do not sum memory limits as reserved usage. Snapshot is not a long-term peak or safe capacity reservation.

Shared host service cgroup memory: containerd~239MiB,Docker~207MiB,journald~75MiB,snapd~40MiB,cron~38MiB,control~25MiB,L2TPsystemd~24MiB,license~22MiB,multipathd~22MiB,nginx~7MiB. Accounting categories differ and can contain cache/children; do not double-count as a host-total reconstruction. These are shared/operational services, not proven removable waste. No recommendation to stop Docker, networking, cron, license or update services merely to reduce displayed memory.

## Historical lab candidate

Three running containers named max_final6_isolated_core/web/db, created/running since11October. DB has no Compose labels and therefore must be included by pinned name/ID, not excluded by project-only filters. Web is unhealthy; no production incident is inferred from that historical lab state.

Working memory at second read: Core1.219MiB,Web416KiB,DB19.2MiB, total~20.8MiB. Process VmSwap totals~196.6MiB (DB~117.8MiB,Core~78.7MiB,Web~0.1MiB). These are potential releases, not guaranteed exact free-MemAvailable deltas. Container configured ceilings are not allocated RAM.

Read-only dormancy/dependency evidence: no published ports in the three containers; no Nginx references to their names/IPs; no current TCPconnections involving their IPs; no environment references from other running containers; no shared critical production storage/database mounts found. DB last session update11October14:57:53 and last auth11October05:55:16 in its stored timezone. Web/Core log tail had no24hactivity. Configured2NASentries are historical configuration, not live traffic. No synthetic test was run. Inspection cannot prove every possible external/unrecorded dependency absent, but evidence supports a dormant-lab shutdown candidate.

Recommended first cleanup, if separately requested: stop the three exact pinned lab containers only, preserving images, volumes and data for reversible restart. Expected RAMsaving only~21MiB plus~197MiBswap: useful tidying, not a multi-GiB memory cure. Do not delete this database/volumes or replay financial fixtures. max_panel_l2tp is essential shared production transport and is excluded despite missing Compose labels.

## Conclusion

No large clearly-unused RAM consumer was found. Largest current resident processes are production MariaDB/Python. The main protection remains fixing large session PDF resource handling and adopting reviewed per-service limits, not deleting stopped containers or blindly shrinking active DBcache. Existing M emergency cap and best-effort monitoring remain intact.

Evidence: `PRIVATE/host-resource-inventory-20261027-r1/REPORT.private.json` and aggregate private samples. First process-list subprobe used an unsupported Docker topformat and was corrected without mutation. Related: [memory watch](HOST_MEMORY_WATCH_20261027.md), [confirmed OOM source](M_SESSION_EXPORT_OOM_20261027.md).
