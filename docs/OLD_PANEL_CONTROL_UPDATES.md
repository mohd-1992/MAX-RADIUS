# Old-panel update button and control v1

Activated and accepted on 22 October 2026 at 21:43:51 Asia/Aden. Only the old
panel service restarted; 30 UI files, all 27 existing containers, tenant/controller
configuration files, control data/trust, shared workers and panel session stayed
unchanged. No tenant enrollment, release publication or update was executed.

The backend adapter is `deploy/hosted-max/panel_updates.py`; the exact old-panel
patch is `old-panel-control-updates.patch`. No templates or static files change.
Deployment status belongs in HANDOFF and the protected activation receipt.

The authenticated single-tenant and bulk buttons keep their existing background
job/host lock and response shape. They delegate to the new controller, rather
than pulling `latest`, running the old schema migration or recreating Compose
services directly. A missing/unavailable channel fails closed. No compatible
published release is a successful check with an explicit "no update started"
message. Uploading an image does not make a release available through this button.

## New and existing installations

New creations reuse their enrolled controller, private credential, socket and
per-release attempt receipts. They do not receive a second identity/controller.

Existing installations enroll lazily when a human explicitly requests an update.
The adapter verifies the canonical tenant path, stored installation identity,
actual Compose service ownership and current signed runtime/publisher. Image IDs
such as `sha256:...` are supported by checking the actual image/runtime, without
weakening the requirement that incoming releases use approved Registry digests.

The official target-registration service creates an encrypted reader credential,
or returns the existing active identity without rotating its credential. A claimed
or ambiguous enrollment is retained for review, not repeated. The host controller
is created/enabled only by that explicit click. Enrollment alone does not edit
tenant Compose, restart Web, take a backup, pull an image or install a release.
If no release exists after enrollment, publish a compatible release in the center
and explicitly request the update again.

The host controller is `panel_update_controller.py`, with a frozen copy of the
shared channel/runtime/executor/controller sources under `update_support` in the
old panel. It does not import the tenant application into the panel. Its Unix
protocol is the same as the native signed controller. A fixed systemd worker
survives panel/Web replacement; interrupted preparation cannot replay backup,
and interrupted switching enters the shared recovery path. No polling installer
or timer is added.

On the first successful upgrade to a control-capable version (>=2.6.4), the executor
adds the host socket only to the replacement Web and persists its environment and
read-only mount in Compose. Rollback retains the exact old Web and Compose. Later
updates reuse the matching socket. Existing Web/Core images and signed modules
are not altered by deployment of this panel code.

## Installation contract and boundary

The button checks the authenticated channel, submits only the signed release ID,
version and manifest hash with mandatory backup, then waits for the exact job's
terminal result. Queued/unknown/rolled-back/error states are never reported as a
successful installation. A prior attempt cannot be silently retried. Native and
panel entrypoints use the same controller locks and per-release receipts.

Only compatible published Web/Core releases without explicit migrations are
supported. DB images, shared services, NAS, tunnels and old accounting are outside
the update scope. Older versions requiring a data migration remain blocked until
that migration has its own supported contract. The obsolete rollback button is
blocked for enrolled installations so it cannot replay an unrelated historical
checkpoint; recovery follows the new engine's retained job evidence.

Activation must not invoke any tenant update/enrollment endpoint. The new service
template is installed dormant. Only `saas-manager.service` is restarted. Compare
UI hashes, all existing container IDs/images/start times/restarts/mounts, tenant
configuration hashes, controller state, control targets/releases/events/trust,
shared service identities and the panel session key across activation.

## Evidence

91 Linux checks include official encrypted target registration against frozen
control code with test-only storage/keys, reuse and disabled-target rejection,
real Unix HTTP routing, protected dispatch, no-release/no-fallback, ambiguous jobs,
candidate enrollment, rollback and unchanged creation behavior. Windows cannot
exercise `fcntl`; the Linux suite is the relevant lock/executor acceptance.

A fresh Docker-volume fixture exercised success, verified rollback and lost create
response. It checked the actual replacement's socket mount, unchanged DB identity,
synthetic 64-bit counter progress and repeat no-op; all 942 pre-existing local
container image/mount identities stayed unchanged. Runtime/license probes were
injected in this fixture; it is not a real tenant, NAS or license acceptance.
Fixture containers were stopped and their evidence retained.

Protected evidence: `PRIVATE/old-panel-control-button-20261022/`.
