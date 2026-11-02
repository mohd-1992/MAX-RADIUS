# Checked detail-file close

FreeRADIUS3.2.1's diagnostic detail writer ignores the final `fclose` result. A buffered ENOSPC can therefore go unreported. `detail-close.patch` checks that result, reports the native error, releases the exfile lock, and returns FAIL. The accounting site's separate detail failure override continues to mandatory SQL; SQL failures retain their original semantics.

`build-detail.sh` verifies the exact Debian package version, SHA256-pinned source and archive, applies the patch without fuzz, and compiles only this module with stack protection, FORTIFY, RELRO and immediate binding. Installed ABI headers take precedence; missing internal headers come from the pinned3.2.1 archive. Compilers and development dependencies stay in the build stage. The patched GPL source and patch are included in runtime. Changing the FreeRADIUS package version requires reviewing the source pin and rerunning native acceptance.

See [local2.6.9 acceptance](../../../docs/CORE_DETAIL_ACCOUNTING_FIX_269_LOCAL_20261031.md) for the artifact identity, failure cases, rollback and coverage limits. No production action is authorized by this file.
