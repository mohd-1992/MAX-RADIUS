# Product brand point5 —26October2026

Human approved the supplied MAX RADIUS mark and local implementation. Development5122 only; no image rebuild, release signing, customer/domain provisioning or remote deployment in this step. Point6 session-log changes remain separate.

## Bilingual aligned asset follow-up

Human replaced the initial mark with the supplied bilingual aligned package, superseding the intermediate bilingual revision. Both horizontal transparent SVGs now contain MAX RADIUS / ماكس راديوس; their native viewBox is1093×245 and shared markup preserves that ratio. Icon/favicon/Apple/PWA files match the aligned package byte-for-byte and remain the same symbols. Shared image URLs carry aligned-r3 cache revision so existing browsers request the updated artwork. No font dependencies or embedded raster/script elements were introduced.

Ten isolated product checks and eight existing HTTP/render checks pass again. Real admin login reviewed at desktop and390px mobile in light/dark; the horizontal macro also supplies portal login/register and the license publisher card. Existing tenant replacement checks pass. Previous PDF credit check remains applicable to the unchanged icon; PDF layout was not rerun merely for a horizontal logo asset replacement. Local5122 only. Screenshot BRAND-ALIGNED-LIGHT.private.png records the new artwork; no business/settings writes, customer access or release build.

## Display contract

- Release-owned vector assets live in `web/static/brand`, outside tenant uploads. Four transparent SVGs provide icon/horizontal × light/dark versions; favicon light/dark and ICO, Apple and PWA-size PNG assets are included. Only one copy of the supplied package is used. Outlined letters require no external font.
- Administration login/sidebar and subscriber portal header/login/register use the approved default mark when the existing permitted tenant `logo_url` is absent. An existing tenant logo continues to occupy its main slot. Existing white-label licensing rules are unchanged; no branding settings or database fields were added.
- Publisher credits remain independent of tenant art in the admin/portal footer, admin login footer, and standalone license footer. Publisher card in the actual license page shows the horizontal logo on the left at desktop width and above the text on mobile. No new About page was created.
- Browser SVG icons follow the browser's light/dark color preference. In-page marks follow the existing `html.theme-light` preference; printing forces the version suitable for white paper. Login now also synchronizes Tailwind's `dark` class with the stored theme, fixing previously low contrast on dark login.
- Inherited report pages include the publisher footer. Invoice print popup and wallet receipt contain explicit publisher credits that survive their print layouts. Card artwork/credential exports and CSV/XLSX data formats are not watermarked or reshaped by this change. Session PDF export is implemented subsequently in [point6](SESSION_HISTORY_DEV_20261026.md), including repeating publisher credits.
- Assets/templates are included by the existing Docker `COPY web/` and signed-runtime packaging; a later approved build carries them to new installs/domains. No installation or tenant creation was run here. There is no remove/replace publisher control in settings; this is not a claim that a server owner cannot alter source files.

## Verification

Eleven affected template parses pass. Ten existing isolated product protection checks pass; the license fixture now loads its shared brand macro and Flask-like global URL helper. Read-only factory HTTP checks on the existing isolated `history266_preview` clone pass for administration login, portal login/register, license and sales. Three rendered tenant-logo checks verify main-logo replacement while publisher credits remain. No account, settings, license, NAS or business-record changes were made.

Visual checks: real admin and portal login at desktop/mobile390 in both themes; sanitized license publisher-card preview at desktop/mobile390 in both themes. The preview contained only the publisher card, not license identifiers or accounts, and temporary static previews were removed after review. One synthetic A4 PDF through the existing offline renderer was rendered to PNG and visually checked: Arabic credit, icon and light-paper version are legible. This is not a replay of a financial operation or a verification of every existing print design.

Private evidence: `BRAND-HTTP.private.json`, synthetic `BRAND-PRINT.private.pdf`/PNG and login screenshots in the existing development evidence directory. Development preview helper was corrected to leave streamed static HTML responses untouched by its banner; this helper is private and outside application source.

## License card vertical alignment follow-up

Publisher logo/text block is now vertically centered against the entire rights/contact card content at desktop width, rather than against its heading row. On mobile it stays above the content. Ten product tests and eight existing HTTP/render checks pass; sanitized desktop/mobile card reviewed and temporary preview removed. Local5122 only.

## Official website link in UI credits

Human approved the existing official website link in administration/portal footers and the administration login credit. Those three callers pass the release-owned product website into the shared optional credit macro; visible attribution text is طُوّر بواسطة ماكس راديوس (the URL is not printed), opens a separate tab with noopener/noreferrer. Default report/PDF/standalone-license credit stays unchanged, existing license contact card is untouched, and card artwork/exports are untouched. Ten isolated product checks and scoped macro rendering pass.

## Installer-source default name

Human limited this change to installation source only, leaving domain provisioning unchanged. MySQL settings seed network_name/company_name/isp_name and SQLite isp_name default use ماكس راديوس | MAX RADIUS. Runtime fallbacks/current settings are untouched. Isolated settings-only SQLite seeding verifies default and preservation of a customized existing name; no database initialization or installation was run.
