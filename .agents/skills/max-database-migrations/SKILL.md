---
name: max-database-migrations
description: Change MAX RADIUS schemas or accounting triggers while preserving history, import compatibility and concurrency.
---

Read [DATABASE](../../../docs/DATABASE.md) and relevant decision records. Locate bootstrap SQL, healer and specialized migration installers; update active creation paths together. Validate against MariaDB when locks/triggers are involved, including repeated migration and concurrent operations. Preserve renewal sale rows: voucher_id is not unique. Handle NULL metadata and existing markers idempotently. Do not rewrite the historical full_db.sql dump as a migration. Before an authorized live migration capture backup and image/trigger identities, then compare original financial fields. DDL may commit implicitly. Keep live credentials and dumps outside Git.
