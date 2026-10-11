---
name: max-release-deployment
description: Build and verify MAX RADIUS releases for an explicitly selected local or hosted environment.
---

Read [DEPLOYMENT](../../../docs/DEPLOYMENT.md) and the relevant CURRENT_STATE snapshot. Confirm target service/Compose project and existing image; protect security-module packaging when adding runtime files. Build immutable tags and record tested code/image identities. Deploy only within current user authorization, retaining rollback image/config and database backup if needed. Check container health plus runtime source/trigger behavior and customer data; healthy alone is insufficient. Tenant Docker control must stay scoped to the authorized controller. Do not push Git, Docker Hub, restart other tenants or alter NAS settings unless the task scope includes them.
