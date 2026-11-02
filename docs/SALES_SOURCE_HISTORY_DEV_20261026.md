## Shared list heading sorting —26 October2026

User extended sales-style sorting to subscriber, card-subscriber and voucher-batch lists as a separate UI request. Shared implementation in services/list_sort_service.py, components/list_sort.html and list-sort.js. Card SQL fields use fixed expressions and stable IDs before paging; derived fields reuse existing full-filter hydration and only then sort/slice. Browser preferences are independent of sales and scoped to identity/list. Six pure and four native HTTP cases pass;15 sales contracts remain passing. Browser checks show headings on all three lists and persistence on refresh/bare navigation. Only source-bound5122 restarted; no image/customer/domain deployment. Screenshot ui-list-sort-light.private.png is an empty synthetic list to avoid exposing accounts.

## NAS names and default period —26 October2026

Tables and exports now resolve the current device name by the saved NAS address, falling back to nas shortname, saved label and finally IP. This is a read-only display lookup; financial rows and source snapshots are unchanged. Server NAS sorting uses the resolved name. Bare sales URLs default to today in the configured timezone; explicit periods still apply. Buttons: today, yesterday, this week, this month, last month, this year, all time, custom. Catalog choices retain all-time permission scope. Fifteen pure and five read-only native checks pass, including virtual current-name/shortname/IP fallback and unchanged grouping totals. Applied only to source-bound development5122.

# Historical sales sources — local development preview, 26 October 2026

Point 2 remains under human review. After the request to shorten development iterations, the historical-source changes run from the current source in an isolated Docker development application at http://127.0.0.1:5122/vouchers/sales. Normal login is required. Its database is an independent clone of the local 25 October 20:10 UTC snapshot. Main local5090 remains on Web eceab63004be/Core a691a09bc91a; no consolidated image build, Registry/Git push, domain or customer update was performed for this change.

## Evidence contract

`services/sales_source_service.py` resolves service type separately from internal server. Existing confirmed snapshots remain fixed. Historical sessions must cover the operation time, belong to the account (including stable rename ownership), and contain usable NAS/source evidence. A later login is never substituted for the sale. Conflicting protocol or point evidence remains ambiguous. PPPoE interface participates in point identity; physical Hotspot ports do not create separate servers.

Packet protocol and recorded snapshots establish service type. A matching package reference can suggest it, but is explicitly marked inferred, including exports and the quality filter. An inferred type is not proof of historical configuration. Changing today's package does not rewrite an already captured inference. Missing fields stay unknown; a known type or NAS is retained independently. Future application captures prefer packet evidence to the current package; missing packet fields can still leave a future source incomplete. Native activation trigger SQL was not changed in this round, so universal future coverage is not claimed.

The resumable background worker processes bounded batches, skips import maintenance, shares the factory-reset job lock, and retries incomplete/inferred records. Version and review timestamps reside in metadata. `wisp_sale_source_audit` records actual classification transitions with before/after evidence. A per-database advisory lock and row locks prevent duplicate concurrent work; audit insert failures roll back the metadata transaction. There are no ledger, balance, contract or accounting updates. The additive schema is included in bootstrap/healer, backup compatibility and factory reset.

## Local snapshot result

All 26,647 sale metadata records were examined. For 26,636 voucher operations, 193 service types came from recorded snapshots and 26,443 were explicitly inferred from package references. Session evidence supplied a point for 2,524 voucher operations; 24,112 remain without a reliable point. Eleven paid subscriber invoices comprise two recorded confirmed sources and nine inferred types; six have a point. All 21 original business-table multiset hashes were identical before and after enrichment. These figures describe this frozen local snapshot only.

## Verification and limits

- Fifteen pure source-resolution cases pass.
- Fourteen isolated MariaDB cases: thirteen initially passed; the late-evidence retry boundary failed, was corrected from `<` to `<=`, and its focused recheck passed. Cases cover ownership, ambiguity, repeatability, audit rollback and quality/money partitions.
- Six full-factory HTTP/export checks pass, including scopes, invalid filters, anonymous access, CSV/XLSX totals and read-only GET behavior.
- Four additional cases pass: packet precedence for new application sales, mixed-protocol ambiguity, atomic rollback on source failure, and two-worker concurrency after removal of the old content-hash unique constraint. The audit now permits repeated genuine A-to-B transitions.
- Desktop and 390×844 mobile previews checked in both themes; quality filter retained, themed badges and horizontally scrollable tables inspected. The development banner distinguishes the clone. License isolation uses a synthetic local facade, not enrollment or validation against a customer.

Evidence is stored outside Git in PRIVATE/sales-history-266-20261025-r1, including ACTUAL-BUSINESS-PRESERVED, HISTORY-NATIVE-RECHECK, HISTORY-HTTP, HISTORY-FUTURE-CAPTURE and theme screenshots. Completed seed/clone/enrichment drivers have markers and must not be replayed blindly. Keep the development app and its isolated DB for review; stop only this round's synthetic preview and source workers. No new FreeRADIUS packet round or physical RouterOS test was performed. Points 3 and 4 remain pending point 2 acceptance.

## Human UI follow-up

On 26 October the human requested removing the source-coverage/inference explanation panel and the session-counter explanation paragraph. Both were removed from the source-bound5122 template; quality filters, row provenance, exports and calculation semantics remain unchanged. Jinja syntax and the mounted template were checked. No image build or main5090/customer update.

The human subsequently removed inferred wording from page rows, package badges and the quality dropdown. Internal provenance and export metadata remain intact; no financial behavior changed. Mounted Jinja template parses successfully.

The human requested removal of the serial-number column from the on-page sales log. Header/cells removed and empty-state colspan adjusted to10. Export contract unchanged; mounted template syntax verified.

## Table navigation follow-up

The three existing tables now share an accessible tab list: package (default), internal server, and operations log. Exactly one panel is visible; filters, totals and charts remain shared. A validated table_view query parameter preserves selection across refresh, filter forms, record-kind links and log pagination; ordinary anchor navigation also works without JavaScript. Client switching avoids a page reload and supports RTL arrow keys/Home/End. Mobile shows all three tabs in one row; current theme colors and table scrolling are retained. No reporting query or amount formula changed.

Verified in the existing synthetic preview: default panel, switching all three, reload persistence, Hotspot filter retaining log selection, keyboard navigation, desktop both themes and390px mobile tab widths without overflow. Jinja syntax passes; selected light-theme specificity corrected after visual review. Screenshots ui-table-tabs*.private.png are in the private lab. Synthetic helper stopped again after verification;5122 source preview remains available.

## Table spacing and typography follow-up

Applied the remaining human-approved display refinements in the5122 source template: wider package/reseller/server cells, compact badges, shared padding and72px log rows, left-aligned numeric columns, smaller isolated currency labels, two-line date/time, and an em dash for unknown table fields. Financial values, ranges, filters and exports retain their original semantics. A shared escaped Jinja money macro preserves zero and unknown values. Verified mounted syntax, range/zero/null/escaping examples, equal log-row heights in the synthetic dataset, numeric alignment/date strings, desktop light/dark and390px mobile light/dark. Mobile page has no horizontal overflow; tables retain their own horizontal scroll. Screenshots ui-table-polish*.private.png are private. No schema, business-data, customer or image deployment.

## Persistent column sorting

All24 visible headers are sortable:8 package,6 server and10 operations columns. Clicking toggles ascending/descending with an arrow and aria-sort. Each table has its own default-reset link. Preferences persist in this browser's localStorage, namespaced by signed-in admin identity and table; other browsers/devices do not inherit them. URL parameters override stored preferences; unavailable browser storage leaves ordinary sorting links functional. Restoring a preference without URL sort parameters performs one navigation so SQL applies it before pagination. Filters/tabs/pagination/export links and forms retain all table choices. A changed log sort starts at page1.

services/sales_reporting_service.py validates each column/direction against fixed SQL expressions. Ordering happens inside the same read-only snapshot; the log sorts all matching records before LIMIT/OFFSET. Numeric values sort as database numbers, not formatted text; package price/share ranges sort by minimum then maximum. Null numeric/date values stay last in both directions, with stable tie breakers. Aggregate queries use outer ordering over grouped derived tables, avoiding MariaDB's aggregate-alias expression restriction found and corrected during native verification. No amounts, ownership predicates, schema or business data changed.

Verification:14 pure reporting cases (11 existing plus3 sort contracts);48 native column/direction combinations with identical totals;4 isolated actual-clone pagination checks each comparing100 expected rows across2 pages without duplication; restricted-owner scope; invalid sorts return400; CSV/XLSX match full username order and original totals. Browser verified repeated header toggle, independent preferences, reload and bare-page restoration, per-table reset, filters retaining log order, both themes and390px mobile without page overflow. Evidence SORT-NATIVE, SORT-PAGINATION, SORT-EXPORT and ui-sorting*.private.png is under the private lab. Source development5122 restarted with the new service/template; main5090 and remote environments remain unchanged. Synthetic preview stopped again.

## One row per package

The human approved combining Hotspot and PPPoE sales of the same historically recorded package name into one package row. GROUP BY now uses package_name only, after the existing ownership/date/service/NAS/server/quality filters. Distinct service kinds appear as compact badges within that row; unknown source types retain a dash. Counts, gross, network share and commissions sum once; recorded price/share ranges retain minimum/maximum semantics. Package service sorting uses the combined kind string; other sorting and detailed exports remain available. No ledger or schema writes.

run_sales_package_grouping_lab.py --run is restricted to the existing sales266_fixture DB. Four native read-only cases cover independent original-ledger aggregation, a process-local virtual mixed-protocol ledger with varied100–200 prices/shares and single-row rendered HTML, service filter partitions,16 package sort combinations and owner scope. No fixture rows are modified or financial seeds replayed. Existing14 pure reporting tests pass. Actual isolated local clone reconciles26636 voucher operations to19 package rows and original ledger totals. Browser verifies unique package rows and unchanged badge styling in both themes; the mixed-row HTML is checked in the virtual test rather than relabeling persisted fixture records. PACKAGE-MERGE and PACKAGE-MERGE-CLONE receipts and ui-package-merge*.private.png are in the private lab. Development5122 restarted; main5090/remote environments untouched.
