# MAX RADIUS 2.6.3 — LAB snapshot, 22 October 2026

This release publishes the exact accepted LAB image artifacts under the immutable
tag `20261022-lab-v2.6.3-limit45`. It is a separately selected release, not an
automatic upgrade of existing installations. The general `release.json` and
Docker `latest` tags remain unchanged.

The release manifest records all five services: MAX Web, FreeRADIUS Core, L2TP,
MariaDB and Autoheal. MAX images are published to their existing repositories;
the two upstream images retain their verified original digest references. Runtime
volumes, databases, licenses, machine identities and credentials are not included.

Web includes the accepted 45,000,000-byte Telegram document/part limit,
size-dependent bounded upload timeout, the existing 5 GiB archive ceiling, and
exclusion of card-background media from newly generated engine backups. Core
includes the accepted accounting identity policy. L2TP and both upstream images
retain their accepted versions.

## Verification and limits

- All five running LAB identities, image identities, mounts, start times and
  restart counts were checked before publication. Publishing does not deploy or
  restart these services.
- Historical image layers were checked for private instance paths and selected
  credential patterns. This bounded scan is not proof against every secret type.
- The registry manifest, configuration and filesystem layer identities are
  checked against the accepted LAB images after upload.
- 132 isolated source tests passed: 48 Telegram, 26 updater, 20 container-binding,
  10 maintenance, 10 product protection, 9 runtime distribution and 9 feature
  entitlement tests. The binding fixture was adjusted to isolate ownership from
  entitlement decorators; the entitlement policy is tested separately.
- The source snapshot is recorded alongside the accepted compiled artifacts. The
  images are not claimed to be reproducible rebuilds from the publishing commit.
- No fresh live NAS traffic, financial operation, Telegram delivery, system
  migration or production rollback was executed for publication.

The built-in client updater is Web-only. Its failure path preserves the prior
container and does not restore an old database. However, its restore routine does
not repeat the complete post-restore health/license verification performed on a
new candidate. This remains a known limitation. Core/schema deployment and a
target-specific production update channel require separately reviewed workflows.

The runtime archive uses an explicit ten-file allowlist, with the existing host
clock tools remaining opt-in. No archived operational repair scripts are shipped.
