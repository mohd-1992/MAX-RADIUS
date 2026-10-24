# Update bootstrap for new domains created by the old panel

Later button integration is documented in [OLD_PANEL_CONTROL_UPDATES](OLD_PANEL_CONTROL_UPDATES.md).
It preserves this creation hook and reuses its native controller. The original
"new creations only" deployment boundary below describes the 21:11 activation.

Activated and accepted on 22 October 2026 at 21:11:43 Asia/Aden. The live old
panel loaded the new backend code; 30 UI file hashes and 23 existing container
identities stayed unchanged. Control keys, service identity and zero production
targets/releases/events stayed unchanged. Only saas-manager was reloaded; its
session key and the shared tunnel/NAS worker identities stayed unchanged.

Scope: backend code only, new creations only. No template, static asset, existing
tenant update button, existing tenant Compose, tunnel, license or accounting change.
The human creates the next real domain after this path is ready; the deployment
task itself never creates a domain or registers a production targeting identity.

## Owners and creation sequence

- `deploy/hosted-max/new_domain_updates.py` is deployed as the old panel's
  `new_domain_updates.py` module, outside MAX tenant images.
- `deploy/hosted-max/old-panel-new-domain-updates.patch` contains the small change
  to the old panel's live `central_provision.py`. All functions except `create`
  remain identical. Existing login, CSRF, background jobs and host lock apply.
- The current source has the compiled provisioner/controller inside the published
  2.6.4 image. No new Docker build or latest update is needed for this hook.

The creator first performs a read-only preflight against the existing control
backend contract and storage. It refuses missing keys, missing tables or changed
adapter source; it never generates replacement production trust.

After the original image-pull/digest verification and schema extraction, before
starting any tenant service, the hook binds the new tenant directory, metadata,
instance identity and pinned Web image. It refuses existing project containers,
failed creation markers, previous bootstrap receipts, source overrides or images
without the approved control protocol, publisher trust and version >= 2.6.4.

## Target registration and confidentiality

The old authenticated creation job delegates one local, root-only registration to
control's existing `ReleaseCenter.target` service. It uses the existing Store
encryption, transaction, idempotency receipt and audit action. No control server or
worker is imported or started, no Store constructor/schema initialization is run,
and no administrative password or private signing key is copied or rotated.

The registered identity is the canonical new domain. Its idempotency key is tied
to the new installation identity. The credential stays encrypted in control and
is delivered through subprocess stdin to the provisioner, then stored in the
host-only controller configuration. It never enters image layers, tenant metadata,
Compose, ordinary operation errors or public bootstrap receipts. No release is
published as part of registration.

## Controller and readiness

The image's signed compiled provisioner adds the controller, its private storage,
the Web Unix-socket mount and startup dependency. The old-panel adapter assigns an
explicit per-tenant controller container name for compatibility with the panel's
service cleanup conventions. It uses Docker's existing bridge for outbound HTTPS;
the controller has no TCP management listener. DB/Core definitions are preserved.

The original creator then starts its DB/Web/Core and completes its normal checks.
Before announcing successful creation, the hook checks the actual Web version and
reads the authenticated targeted channel through the real Web-to-controller Unix
bridge. Controller progress must be idle: this step does not apply an update.
A private creation-ready receipt records the verified connection.

An uncertain bootstrap is retained for review and is not repeated automatically.
The existing creation exception handler stops only that new Compose project on
failure. Its root-protected bootstrap/target history is retained; no old DB restore
or automatic erasure of historical identities is performed.

## Verification and operational boundary

13 isolated tests cover read-only preflight, missing trust, changed contracts,
existing/foreign tenants, immutable images, protected credentials, ambiguous
registration, no replay and readiness failure. They passed on Windows and Linux.

A fresh Docker-volume integration fixture used the frozen live control code with
test-only vault/signing keys. It exercised official target registration/idempotency,
encrypted credentials, the actual compiled 2.6.4 provisioner in nested Docker,
actual Compose validation and a signed targeted manifest accepted by the compiled
consumer and rejected for another identity. No tenant services, real domain,
production control storage, NAS or live license were used in that fixture.

Activation is guarded by the old host-operation lock and preserved source backups.
Only the old panel service is reloaded. Acceptance checks the login page, live code
hashes, unchanged UI file hashes, unchanged pre-existing container identities,
unchanged control data/trust/service identity, unchanged shared worker identities
and unchanged panel session key. It does not invoke the real creation endpoint.

Protected deployment/evidence reference:
`PRIVATE/new-domain-update-bootstrap-20261022/`. The actual next domain, its
license and its NAS/accounting acceptance remain for the human-requested follow-up.
