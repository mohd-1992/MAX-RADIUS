# M update sequence offered — 2026-10-27

Scope: m.max-net.net only. Existing reader identity was reused. The human installs each stage.

Published sequence: **2.6.5 → 2.6.6 → 2.6.7 → 2.6.8 → 2.6.9**.
The 2.6.9 offer uses the corrected reports/backup image `sha256:c5ab9fe42359c867c6dedabaf607ff492b2652e8160fa5df987e34e645db098b` and approved Arabic release notes.

The actual M Web remained 2.6.5 and its signed check displayed 2.6.6. Four signed hypothetical feed checks verified the next stage for each required version/protocol/schema combination. Unprepared feature requests remained blocked. These checks do not prove successful installation or live authentication/accounting after an update.

M had a historical terminal preparation error targeting 2.6.5, at 0%, before service switching. Its progress was preserved; no apply, retry, cleanup, installation job, restart, or channel change was performed.

All pre-existing release records and reader targets remained unchanged. The four new records target M only, with automatic_install=false. M container images and start times and channel hash remained unchanged. Registry manifests for six immutable image references were verified. A consistent encrypted control-plane backup was verified off host before publication.

Evidence: `PRIVATE/m-sequence-269-offer-20261027-r1` (protected; do not replay publication scripts).
Public receipt: [release identities and delivery checks](../releases/release-20261027-m-sequence-through269.json).

Related: [MAX1 sequence contracts](MAX1_SEQUENCE_OFFER_20261027.md), [corrected 2.6.9 and rollback limits](FOUR_K_269_BACKUP_REISSUE_20261027.md).

## Arabic announcement correction — 18:15 UTC

The initial M 2.6.9 announcement title, notes and rollback description had been decoded as CP1256 instead of UTF-8 during local plan preparation. The signed feed and actual Web faithfully displayed that already-corrupted text. The image and application were unaffected.

Published announcements are immutable, so the malformed announcement `379507e9cefb64ba56b56f2d82ee498a` was withdrawn and replaced through the supported scoped reissue API by `942d18b06374b345a7fd1eda985c397d`. Version, image references and installation requirements are unchanged. Correct Arabic title and all eight release notes were verified in the signed feed and the actual M Web check. M remained 2.6.8; no installation, container restart or progress change. Other announcements and channels were preserved.

[Correction receipt](../releases/release-20261027-m269-notes-correction.json). Protected evidence: `PRIVATE/m269-notes-correction-20261027-r1`. Do not replay publication drivers. Always specify UTF-8 when loading publication plans; comparing only delivered text to a malformed input plan does not validate its encoding.
