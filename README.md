# National Library of Belarus (`elib.nlb.by`) to Wikimedia Commons Uploader

[![CI](https://github.com/vitaly-zdanevich/nlb/actions/workflows/ci.yml/badge.svg)](https://github.com/vitaly-zdanevich/nlb/actions/workflows/ci.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=coverage)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Production code](https://img.shields.io/badge/production%20code-3%2C623%20lines-blue)](upload.py)
[![Test code](https://img.shields.io/badge/test%20code-5%2C972%20lines-blue)](#running-tests)

[`upload.py`](upload.py) iterates over open-access digitized scans from the Electronic Library of the National Library of Belarus (`https://elib.nlb.by`), archives the source responses, and uploads eligible PDFs to Wikimedia Commons with their useful catalog metadata readable on the file page.

![Screenshot of the website](screenshot.webp)

## Features

- **Publication selection and rights review**: The default search ends at 1900, including custom URLs without an upper year bound. Explicit URL bounds or `--to-year` change selection, while a separate evidence-based rights gate decides whether to upload. Unknown, conflicting or uncertain evidence remains pending for review; public access alone does not select a license.
- **Configurable year range and custom URLs**: Supports `--from-year`, `--to-year` (`--until-year`), or passing a custom `elib.nlb.by` search or record URL directly.
- **Original title extraction**: Extracts the full title from the record page heading (`<h1>` / `<h3 property="name">`) and UNIMARC `200$a`, using parent titles from `461` (embedded `200` or standard `$t`) and matching NLB `961` display fields so multi-volume child records retain the work's full title instead of just a volume label such as `T. 2`.
- **Alternate indexing terms and place names**: Preserves NLB `908` genre variants and `926` geographic name variants, including translations, synonyms and historical names. Variants are grouped with their `608` / `620` headings using authority IDs; unlinked variants remain visible. Repeated aliases are deduplicated, and geographic subdivisions stay attached to the appropriate name.
- **Subject synonyms and related terms**: Preserves NLB `906` references linked to their `606` subjects by authority ID. Distinguishes `450` synonyms from `550` related subjects, retaining synonyms of a related heading with that heading. Embedded `430`/`530` title references, qualifiers, subdivisions, vocabulary codes and relationship codes remain visible. Unknown or unmatched references survive, exact duplicates are removed, and related terms do not become additional subjects or authors. For example, the Polish uprising record retains “Лістападаўскае паўстанне 1830—1831” and “Ноябрьское восстание 1830—1831”.
- **Geographic subject names**: Preserves `907` historical and multilingual names linked to `607` geographic subjects, including “Вильна”, “Вильно”, “Вильня” and “Vilnius”. Retains `415` variants and `515` related places, their subdivisions and relationship codes. Variants of related places remain attached to those places. These references describe geographic subjects and do not change publication places.
- **Organization-name variants and relationships**: Preserves `910`/`911`/`912` references linked respectively to `710`/`711`/`712`, keeping `410` name variants separate from `510` related organizations. Historical names, subordinate bodies, meeting qualifiers and source relationship codes remain visible without creating additional authors or publishers. Printer aliases such as “Друкарня базыльян” and “Типография базилиан” survive with their source role codes.
- **Publication prices and binding qualifications**: Preserves `010$b/$d`, such as “У пераплёце” and “35 к.”, in a dedicated catalog section. Repeated qualifications stay with their field's price and ISBN when present; no ISBN is inferred for older publications. These are publication terms from the catalog and do not replace the physical description or describe current market prices.
- **Contributor roles**: Preserves UNIMARC `700`–`702` / `710`–`712` contributors with all `$4` role codes and `$5` copy associations. Authors, translators, editors, compilers, publishers, issuing bodies and former owners of a physical copy are distinguished in the description; unknown codes remain visible. The Commons author field uses explicit author role `070`, or primary/equal responsibility (`700` / `701` / `710` / `711`) when no role is recorded. Uncoded secondary contributors remain visible with an unspecified role. HTML author lists and responsibility statements remain preserved, but only provide an author fallback when there are no structured contributors.
- **Complete personal and family names**: Retains initials, expanded given names, repeated qualifiers, numerals and dates in contributors and subjects. NLB `990`/`991`/`992`/`900` personal-name references and `902` family-name references retain their corresponding headings, authority IDs, variants and relationships. Variants do not create additional authors.
- **Ownership scope**: Roles are attributed to the NLB catalog. Former-owner entries explicitly state that their association with the scanned copy has not been established. Copy references in contributor fields and MARC `3xx` notes remain attached to their respective entries; the uploader does not infer a contributor's copy from a separate note or match catalog copies to scans.
- **Printers and manufacturing statements**: Preserves UNIMARC `210$e/$f/$g/$h` (place, address, printer/manufacturer and date) in a dedicated description section. Each field's manufacturing details remain together in source order, including repeated subfields; only identical complete statements are deduplicated. Printing dates do not replace publication dates, and printers are not inferred to be publishers.
- **Parent and bound-with links**: Preserves typed `461` parent and `481`/`482` bound-with relationships with clickable NLB record IDs, titles, volume designations and copy qualifiers. Catalog links in HTML metadata tables also survive; sidebar recommendations are excluded. A matching local `961` can supply the parent's display title, but does not establish parenthood on its own.
- **Parent publication details with provenance**: Missing MARC publication places or publishers can come from a single unambiguous direct `461` parent and are labeled with the source parent record. The child's own fields take precedence; other parent fields are not inherited. Cached parent responses retain their original capture time and are associated with each child archive. Transient, malformed or mismatched parent responses leave the child pending; an explicit HTTP 404 retains the relationship without claiming inherited metadata.
- **Single-file and multi-part (`METS`) support**: Resolves direct `fileID` links and `metsID` trees. Each structure is fetched once per record, and a direct file also found in METS keeps its part label.
- **SHA-1 deduplication**: Computes the digest with Python's `hashlib` and queries the MediaWiki `allimages` API directly. No shell helper is required. Timeouts, broken connections, invalid JSON and HTTP 500/502/503/504 receive up to five attempts with 5, 10, 20 and 40 seconds between attempts; failed or invalid lookups leave the scan pending without uploading.
- **Resumable state**: Saves progress atomically after each item and page. Pending work keeps the earliest affected page. Dry runs and metadata-only runs leave the production state file unchanged.
- **Record and page counters**: Progress includes `[record 345/9743; page 4/98]`. Counters show the record's position in the selected catalog results and the search-page position, including completed or skipped records. Multiple PDFs from one record share its counter. Resuming or revisiting deferred pages uses the corresponding search position; unavailable totals are omitted.
- **Catalog validation and retries**: Search, HTML, MARCXML, TOC, access, METS and IIIF manifest reads validate their expected structures before use, including record IDs and boolean access decisions. HTTP 200 database/error pages, malformed bodies, interrupted responses, timeouts and HTTP 500/502/503/504 share five attempts with 5/10/20/40-second delays. Exhausted metadata remains pending; failed search pages retain their restart position. Explicit 404s remain allowed for optional MARCXML/TOC/parent exports. Certificate and permanent HTTP errors still stop the run.
- **Temporary-file cleanup**: Removes each staging PDF after processing. Metadata snapshots remain in the archive; `--keep-pdfs` also retains verified original PDF bytes there.
- **Download size in progress output**: Shows the server's advertised size before streaming, such as `Downloading scan (54.4 MB):`, using decimal MB with one decimal place. If the size is unavailable or invalid, it is omitted. The subsequent SHA-1 check message does not repeat the size.
- **Download validation and retries**: Unexpected HTML, malformed PDFs, interrupted transfers, timeouts and HTTP 500/502/503/504 retry within the bounded download policy. Each retry discards the partial file. `pypdf` checks PDF structure and page contents in strict mode without rewriting bytes, and compares page counts with IIIF canvases when available. Exhausted scans remain `download_failed`; they are not permanently excluded as non-PDFs.
- **Skip rejected downloads**: HTTP 400 during a download, including `Security alert! Some conditions are missed!`, is logged and saved as `download_rejected` with the library's response. The uploader deletes the temporary file and continues to the next scan, without retrying, hashing or uploading the rejected download. Other errors retain their existing handling.
- **Upload retries**: When Commons reports `stashfailed: Could not acquire lock` while publishing an assembled PDF, retries publication using the same stored file instead of uploading all chunks again. This also handles failures returned by asynchronous status polls. Chunk and publication failures share a limit of ten failed attempts, with 60 seconds between retries. Before retrying or deferring, refreshes title existence to detect uploads that completed despite an error. Persistent transient failures are saved as `upload_failed` with the error, attempt count, title, SHA-1 and size; the temporary PDF is deleted and processing continues. Authentication, permissions and local I/O errors still stop the run.
- **Revisit deferred scans**: Download, metadata, upload and verification failures, title conflicts and rights-review cases remain pending. Older `not_pdf` state entries become `download_failed` and rewind to page 1 because earlier releases did not retain their source page. Changing the search URL or year filters also resets the cursor. Completed files with recorded verification can skip another PDF transfer after their metadata has been captured.
- **CSRF token recovery**: Each Commons upload request uses the current cached token, including later chunks, status polls and final publication. An expired token is refreshed once per request; persistent `badtoken` errors stop the upload and leave the item pending for a later run.
- **Graceful `Ctrl-C` shutdown**: The first `Ctrl-C` finishes the current item in progress, updates `state.json`, prints the uploaded file count and uploaded megabytes, and exits cleanly. A second `Ctrl-C` aborts the current operation, removes the temporary PDF and exits with code 130. Unfinished items remain pending; restarting checks Commons again before uploading.

## Metadata preservation and readability

Commons descriptions include the bibliographic details, contributors and their roles, subjects and variants, contents, copy and storage information, related works, and useful IIIF metadata. Remaining MARC values appear in a readable catalog supplement with field/subfield labels; unfamiliar codes retain their source codes instead of receiving guessed meanings. Embedded contributor names and dates in linked records also remain visible. Exact repeated display values are deduplicated without dropping distinct copy-specific statements.

Literal catalog punctuation is escaped consistently: `[[...]]`, `{{...}}`, pipes and HTML cannot turn into unintended Commons links, templates or categories. Intentional catalog links remain links. Publication dates retain their original uncertainty and ranges, such as `[1850?]` or `1890–1910`; separate date evidence drives filtering and rights decisions. Arbitrary years in a title do not establish publication dates.

Every run writes an archive, defaulting to `archive/` beside the state file. It contains:

- Content-addressed original response bodies from search, record HTML, MARCXML, TOC, used parent exports, access, METS and IIIF manifests, including error responses received before retries. These are the payload bytes after Requests removes HTTP transfer/content encoding, not packet captures.
- A snapshot manifest for each visited record, with source URLs, capture times, HTTP status, selected public headers, SHA-256 checksums, byte counts and a fingerprint of the uploader's Python source files. Cached parent responses retain their original retrieval time.
- Parsed metadata, proposed Commons descriptions, rights decisions, record-to-file relationships, and available source/Commons verification details. Metadata is preserved before an existing or duplicate PDF can be skipped.
- Original PDFs when `--keep-pdfs` is supplied. PDFs are copied without recompression or repair, verified against their SHA-256 digest before publishing the archive object, and checked again before reuse.

Metadata archives are retained by default and grow with repeated snapshots; keeping PDFs requires substantially more storage. Choose `--archive-dir` on suitable storage and back up the whole directory, including `objects/` and `records/`, together with `state.json`. A local archive on the same disk is not an independent backup. Cookies, authorization headers and viewer-session creation responses are excluded. The default archive path is ignored by Git; exclude custom archive locations as appropriate.

The archive preserves the responses actually retrieved. It cannot reconstruct metadata absent from those responses or prove that the library scanned every physical leaf. Strict PDF parsing and a matching IIIF page count detect structural problems, but do not certify visual readability, image completeness or the correctness of catalog descriptions. A PDF requiring repair stays pending; the uploader does not rewrite it to make validation pass.

## License selection

Search limits and access permission are separate from license eligibility. Automatic selection uses compatible publication-date evidence and a conservative policy:

- With no recorded creative-contributor death dates, the latest possible publication year must be more than 120 years ago and meet the US expired-publication cutoff. The selected template is `PD-scan|PD-old-assumed-expired`.
- When creative-contributor dates are recorded, all relevant deaths must be unambiguous and more than 100 years ago, with publication also meeting the US cutoff. The selected template is `PD-scan|PD-old-auto-expired|deathyear=...`.
- Unknown, conflicting, unbounded or approximate dates, unresolved creative contributors, and other unsupported evidence remain `rights_review`. The archive and state record the reason and evidence.

This is the tool's upload policy, not a determination of every applicable country's copyright rules. `--license 'TEMPLATE|parameters'` (without surrounding template braces) explicitly records the operator's license attestation and bypasses automatic eligibility selection; use it only after checking the applicable [Commons licensing requirements](https://commons.wikimedia.org/wiki/Commons:Licensing). Supplying the default template explicitly is also an attestation. Template requirements are documented at [PD-old-assumed-expired](https://commons.wikimedia.org/wiki/Template:PD-old-assumed-expired), [PD-old-auto-expired](https://commons.wikimedia.org/wiki/Template:PD-old-auto-expired) and [PD-US-expired](https://commons.wikimedia.org/wiki/Template:PD-US-expired).

## Duplicate titles and binary verification

Target filenames retain the NLB identifiers:

```text
<Title> (<Year>) -- <marcID> -- <fileID>.pdf
```

Commons' query-like title blacklist rule (`.*[?&]+[^=]+=[^&]+.*`) is handled by replacing `=` with `-` only in matching filenames. For example, `Piano & Violon = Ноты` becomes `Piano & Violon - Ноты`. The original title remains readable in the description.

An existing title alone is not proof of identical content. The uploader downloads and validates the scan, computes its SHA-1 and SHA-256, and checks Commons for the actual bytes. Matching content is recorded as `existing_title` or `duplicate_sha1`, with its Commons association. A title holding different bytes remains `title_conflict`; the uploader does not overwrite it. Previous completed state without verification is checked again when encountered.

After uploading, the uploader reads Commons file information and compares the stored SHA-1 and byte count with the source. A failed or mismatched check remains `verification_failed`, even when the upload itself may have succeeded. Available Commons revision/file details and source digests are retained together. No existing description is silently replaced during deduplication; metadata updates use the separate reconciliation workflow below.

## Usage

Install the runtime dependencies in your Python environment and configure [Pywikibot authentication](https://www.mediawiki.org/wiki/Manual:Pywikibot/Installation#Configure_Pywikibot) before an upload run:

```bash
python -m pip install requests beautifulsoup4 'pywikibot>=11.6' 'pypdf>=6,<7' mwparserfromhell
```

The former `--find-by-hash-script` option has been removed; SHA-1 checks always use the Python API client.

If downloads work in your signed-in browser but the uploader receives HTTP 400, export the cookies for `nlb.by` and its subdomains in Netscape `cookies.txt` format after opening the library viewer. Pass that local file with `--viewer-cookies`. It must include a current `elib_session_id` for the viewer and the cookies from your signed-in session. The uploader imports cookies applicable to `elib.nlb.by`, including cookies explicitly shared across NLB subdomains, and preserves their expiry, path and secure attributes along with the viewer session. Re-export the file after signing in again if the session expires.

```bash
chmod 600 ~/.config/nlb/cookies.txt
./upload.py --viewer-cookies ~/.config/nlb/cookies.txt
```

```bash
# Process selected scans up to 1900; upload only after the rights checks pass
./upload.py

# Adjust publication year bounds
./upload.py --from-year 1850 --to-year 1900

# Process a specific search results URL or record URL
./upload.py 'https://elib.nlb.by/elib/Search/Results?type=AllFields&isElib=true&daterange%5B%5D=publishDate&publishDatefrom=&publishDateto=1917'

# Dry run: capture metadata, download/validate/hash, and leave upload state unchanged
./upload.py --dry-run --max-uploads 5
```

## Refreshing metadata and reviewing existing Commons descriptions

A metadata-only run starts at page 1 of the selected catalog, captures metadata and descriptions, and leaves upload progress unchanged. It performs catalog/manifest reads but does not download PDFs, upload files or edit Commons descriptions. For previously completed items, their existing file associations are carried into the new snapshot.

```bash
# Capture updated metadata without changing upload state or Commons
./upload.py --metadata-only --archive-dir archive --report run-report.json

# Revisit page 1 while also processing eligible pending/new uploads
./upload.py --refresh-metadata --archive-dir archive

# Optionally keep the original verified PDFs as well as metadata
./upload.py --keep-pdfs --archive-dir /path/to/nlb-archive
```

`--refresh-metadata` revisits completed files to archive their current metadata; verified completed PDFs can skip another transfer. It is a normal upload run and can upload new/pending items. Neither refresh mode edits existing Commons descriptions.

The separate reconciliation command builds a reviewable plan from the latest snapshots with completed, hash-identified Commons associations. It reads current Commons pages anonymously, verifies the binary SHA-1, and includes a diff for each proposed edit. Items without an established matching Commons file are reported under `skipped`. An incomplete latest snapshot is reported there too; refresh that record before planning again. Earlier snapshots remain preserved in the archive.

```bash
python reconcile_metadata.py --archive-dir archive --plan metadata-plan.json

# Read each entry's diff and new_text in metadata-plan.json before applying.
# This explicitly selected command writes metadata changes to Commons:
python reconcile_metadata.py --apply metadata-plan.json --pywikibot-dir ~/.pywikibot
```

Applying a plan checks the current binary hash, page revision and original text again. Changed files/pages require a fresh plan. Additions use a dedicated block for each NLB record/file pair, preserving the surrounding description and licensing. An already applied entry is skipped when resuming a partially applied plan. Generating a plan does not perform those edits.

## Run reports and exit status

The uploader prints a JSON count summary. `--report run-report.json` also writes the counts and completion time as JSON after a normal or partial run. Exit status `0` means the run reported no deferred/error/review outcomes; `2` means at least one search, metadata, download, upload, verification, title-conflict or rights-review outcome remains unresolved. These counts describe this selected run, not an independent completeness audit of the entire library. Unhandled errors still terminate the process; an immediate second interrupt exits with `130`.

Dry runs and metadata-only runs can create archive snapshots and reports, but do not change the production state file. They require network access to the catalog; dry runs also download eligible PDFs and perform anonymous Commons hash lookups.

## Running Tests

The upload integration tests use Pywikibot's real request and chunk-upload code with mocked HTTP responses and an isolated configuration directory. They do not use your credentials or upload files.

```bash
python -m pip install pytest pytest-cov requests beautifulsoup4 'pywikibot>=11.6' 'pypdf>=6,<7' mwparserfromhell
pytest -v --cov=upload --cov=catalog_requests --cov=catalog_validation --cov=preservation --cov=publication_rights --cov=pdf_validation --cov=reconcile_metadata --cov-report=term-missing
# or
python3 -m unittest discover -v
```

## Related Documentation

- [Electronic Library of the National Library of Belarus (`elib.nlb.by`)](https://elib.nlb.by/)
- [UNIMARC 210: publication and manufacturing metadata (IFLA)](https://www.ifla.org/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u-b_210_update.pdf)
- [UNIMARC linking fields: embedded records and standard subfields (IFLA)](https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u-b_4-_linking_entry_block_update.pdf)
- [VuFind Documentation](https://vufind.org/wiki/documentation)
- [IIIF Presentation API 2.1](https://iiif.io/api/presentation/2.1/) and [3.0](https://iiif.io/api/presentation/3.0/)
- [pypdf strict parsing and robustness](https://pypdf.readthedocs.io/en/stable/user/robustness.html)
- [mwparserfromhell wikitext parser](https://mwparserfromhell.readthedocs.io/en/latest/usage.html)
- [MARC 21 / UNIMARC XML Schema (Library of Congress)](https://www.loc.gov/standards/marcxml/)
- [UNIMARC contributor role codes: IFLA Appendix B](https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u_b_appb_update2020_online_final.pdf)
- [UNIMARC personal-name subfields: initials, expansions and qualifiers (IFLA)](https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u-b_700_update.pdf)
- [UNIMARC related personal-name headings (IFLA)](https://www.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/AUTHORITIES/u_a_500_update2019_online_final.pdf)
- [UNIMARC topical subjects and subdivisions (IFLA)](https://cdn.ifla.org/wp-content/uploads/files/assets/uca/unimarc_updates/BIBLIOGRAPHIC/u_b_606_update2019_online_final.pdf)
- [UNIMARC authority references, including 450/550 (IFLA)](https://www.ifla.org/files/assets/uca/unimarc-authorities-format.pdf)
- [Python cookie handling and Netscape cookie files](https://docs.python.org/3/library/http.cookiejar.html#http.cookiejar.MozillaCookieJar)
- [Requests: HTTP retries](https://requests.readthedocs.io/en/latest/user/advanced/#example-automatic-retries)
- [MediaWiki API: `allimages` (SHA-1 lookup)](https://www.mediawiki.org/wiki/API:Allimages)
- [MediaWiki API: chunked uploads and publishing stored files](https://www.mediawiki.org/wiki/API:Upload#Example_3:_Upload_file_in_chunks)
- [Pywikibot Documentation](https://doc.wikimedia.org/pywikibot/stable/)
- [Wikimedia Commons Licensing Policy](https://commons.wikimedia.org/wiki/Commons:Licensing)
