# National Library of Belarus (`elib.nlb.by`) to Wikimedia Commons Uploader

[![CI](https://github.com/vitaly-zdanevich/nlb/actions/workflows/ci.yml/badge.svg)](https://github.com/vitaly-zdanevich/nlb/actions/workflows/ci.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=coverage)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=vitaly-zdanevich_nlb&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=vitaly-zdanevich_nlb)
[![Production code](https://img.shields.io/badge/production%20code-1%2C477%20lines-blue)](upload.py)
[![Test code](https://img.shields.io/badge/test%20code-1%2C163%20lines-blue)](test_upload.py)

[`upload.py`](upload.py) iterates over open-access digitized scans from the Electronic Library of the National Library of Belarus (`https://elib.nlb.by`) and uploads them to Wikimedia Commons with full HTML and UNIMARC (`MARCXML`) metadata.

## Features

- **Default year boundary (`<= 1900`)**: By default, queries digitized records published up to 1900 and applies `{{PD-scan|PD-old-assumed-expired}}`.
- **Configurable year range and custom URLs**: Supports `--from-year`, `--to-year` (`--until-year`), or passing a custom `elib.nlb.by` search or record URL directly.
- **Original title extraction**: Extracts the full title from the record page heading (`<h1>` / `<h3 property="name">`) and UNIMARC `200$a` + `961$a` / `461$a` parent work fields so multi-volume child records retain the work's full title instead of just a volume label such as `T. 2`.
- **Single-file and multi-part (`METS`) support**: Resolves both direct `fileID` viewer links and multi-volume `metsID` document trees on `https://elib.nlb.by:8070`.
- **SHA-1 deduplication**: Checks Wikimedia Commons by SHA-1 hash before uploading (via `find_by_hash.sh` or the MediaWiki `allimages` API).
- **Resumable state**: Saves progress atomically in `state.json` after each item and page so interrupted runs continue from the exact item on restart.
- **Automatic cleanup**: Deletes each temporary PDF scan locally immediately after uploading or skipping.
- **Graceful `Ctrl-C` shutdown**: On `SIGINT` (`Ctrl-C`), finishes the current item in progress, updates `state.json`, prints the uploaded file count and uploaded megabytes, and exits cleanly.

## Duplicate Titles and SHA-1 Deduplication

1. **Unique filename suffix (`-- <marcID> -- <fileID>.pdf`)**:
   Every target Commons filename is formatted as:
   ```text
   <Title> (<Year>) -- <marcID> -- <fileID>.pdf
   ```
   Because both the NLB record ID (`marcID`) and file ID (`fileID`) are appended, two distinct NLB records or volumes that share the same book title will not collide on Wikimedia Commons.
2. **When the target Commons `File:` title already exists (`existing_title`)**:
   Before downloading the PDF scan, [`process_single_scan`](upload.py) calls `uploader.title_exists(commons_filename)`. If `File:<commons_filename>` already exists on Wikimedia Commons, the script logs `Skipping (title exists on Commons): File:<commons_filename>`, records `"status": "existing_title"` in `state.json`, and skips the item without downloading or overwriting it. If the title is created concurrently right before upload, `CommonsUploader.upload` also catches Pywikibot's `exists` status and skips the item.
3. **When the same PDF was already uploaded under a different title (`duplicate_sha1`)**:
   - If the item key `<marcID>:<fileID>` is already marked as completed in `state.json`, it is skipped before any network download.
   - If `state.json` does not have the item (or was reset), the script downloads the PDF to `--staging-dir` and checks its SHA-1 digest on Wikimedia Commons via `check_commons_by_hash`. When Commons reports an existing file with the same SHA-1 (even under an older or different title), the script logs `Skipping (duplicate SHA-1 on Commons: [...])`, records `"status": "duplicate_sha1"` in `state.json`, deletes the temporary PDF, and skips uploading.

## Usage

```bash
# Run with defaults (all open-access scans published up to 1900)
./upload.py

# Adjust publication year bounds
./upload.py --from-year 1850 --to-year 1900

# Process a specific search results URL or record URL
./upload.py 'https://elib.nlb.by/elib/Search/Results?type=AllFields&isElib=true&daterange%5B%5D=publishDate&publishDatefrom=&publishDateto=1917'

# Dry run (download and SHA-1 check without uploading)
./upload.py --dry-run --max-uploads 5
```

## Running Tests

```bash
pytest -v --cov=upload --cov-report=term-missing
# or
python3 -m unittest -v test_upload.py
```

## Related Documentation

- [Electronic Library of the National Library of Belarus (`elib.nlb.by`)](https://elib.nlb.by/)
- [VuFind Documentation](https://vufind.org/wiki/documentation)
- [IIIF Presentation API Specification](https://iiif.io/api/presentation/2.1/)
- [MARC 21 / UNIMARC XML Schema (Library of Congress)](https://www.loc.gov/standards/marcxml/)
- [MediaWiki API: `allimages` (SHA-1 lookup)](https://www.mediawiki.org/wiki/API:Allimages)
- [Pywikibot Documentation](https://doc.wikimedia.org/pywikibot/stable/)
- [Wikimedia Commons Licensing Policy](https://commons.wikimedia.org/wiki/Commons:Licensing)
