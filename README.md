# Fieldkit

A modular Python toolkit for team assessments, with a direct CLI and a
Metasploit-style interactive terminal console. The first module, `web/artifacts`,
collects web files for manual review. Python 3.11 or newer is required.

## Download and install

Install Git and Python 3.11 or newer, including Python's `venv` support. Then run:

```bash
git clone https://github.com/SNO2207/CPTC-Fieldkit.git
cd CPTC-Fieldkit
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
fieldkit --help
```

## Run

From the cloned repository, with the virtual environment activated:

```bash
# Start the interactive terminal console.
fieldkit console

# Or collect files directly from your targets.
fieldkit collect app.example.test 192.0.2.10

# Or use a target list.
fieldkit collect --targets-file targets.txt --output artifacts
```

Replace the example targets with your competition targets. Downloads and the JSON
report are saved in a unique subdirectory of `artifacts/` by default. See below
for the target-list format, console commands, and collection options.

In a new terminal, return to the cloned repository and run
`source .venv/bin/activate` again. Alternatively, use `.venv/bin/fieldkit` directly
without activating the environment. Run `deactivate` when finished.

To update an existing checkout:

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install -e .
```

## Collect files

```bash
# Multiple names, IPs, and explicit origins; use your competition targets.
fieldkit collect app.example.test 192.0.2.10 http://192.0.2.11:8080

# Load a team target list; select HTTP for targets without an explicit scheme.
fieldkit collect --targets-file targets.txt --scheme http --output artifacts/round1

# Include known application paths and a path wordlist.
fieldkit collect http://localhost:8080 \
  --path /assets/custom.js --path /settings.ini.bak --paths-file paths.txt

# Focus on exactly the supplied paths.
fieldkit collect http://localhost:8080 --only-custom \
  --path /robots.txt --path /app.js --no-discover-js --no-backups

# Generic module invocation uses the same options.
fieldkit run web/artifacts http://localhost:8080
```

Targets can also be repeated with `--target`. Target lists accept one target per
line, whitespace-separated or comma-separated entries, blank lines, and full-line
`#` comments. URLs must be HTTP(S) origins without paths, queries, credentials, or
fragments. IPv6 accepts bare addresses or bracketed addresses with ports:
`::1`, `[::1]:8080`, `http://[::1]:8080`. CIDR expansion is not implemented.
Extra path files accept one root-relative path per line and full-line `#` comments.

```text
# targets.txt
app.example.test
192.0.2.10
http://192.0.2.11:8080
```

## Interactive console

Run `fieldkit` or `fieldkit console`. The console provides tab completion,
in-memory command history, and formatted module settings.

```text
fieldkit > modules
fieldkit > use web/artifacts
fieldkit (web/artifacts) > set TARGETS app.example.test 192.0.2.10
fieldkit (web/artifacts) > set TARGETS_FILE targets.txt
fieldkit (web/artifacts) > set OUTPUT artifacts/round1
fieldkit (web/artifacts) > set INSECURE true
fieldkit (web/artifacts) > options
fieldkit (web/artifacts) > run
fieldkit (web/artifacts) > back
fieldkit > exit
```

`TARGETS` and `TARGETS_FILE` are combined. `set` replaces an option's previous
value; `unset OPTION` restores its default. Quote values containing spaces.
`search`, `info`, `show options`, and `help` are also available. Ctrl-C interrupts
a scan and writes a partial report; the console remains available.

## What the first module checks

- `robots.txt`, `security.txt`, `.well-known/security.txt`, and `secrets.txt`.
- `sitemap.xml` and `sitemap_index.xml`.
- Literal `/JS`, `/JS/`, `/js/`, `/.js`, and `/.bak` paths, plus a small built-in
  list of common JavaScript and backup filenames.
- Same-origin `<script src>` references and `.js` / `.mjs` `<link href>` references
  from the homepage, including references resolved through HTML `<base>`.
- `.bak` suffixes on candidate file paths, including discovered script paths.
- Additional paths supplied with `--path` and `--paths-file`.

An extension such as `.js` or `.bak` is not a wildcard URL. This module probes
literal and known filenames and discovers homepage script URLs. It does not
enumerate every possible filename, execute JavaScript, recurse into script
imports, parse sitemaps, or crawl links from robots files. Collected content is
saved for manual review; a filename does not establish that a secret exists.

## Network behavior and limits

| Setting | Default | Behavior |
| --- | --- | --- |
| `--scheme` | `auto` | Try HTTPS, then HTTP if the homepage HTTPS request fails at the network/TLS layer. Explicit URL schemes take precedence. |
| `--concurrency` | `8` | Maximum simultaneous requests across all targets. |
| `--timeout` | `10` | Total seconds per request, including redirects and body transfer. |
| `--max-bytes` | `5242880` | Decoded response body limit; oversized bodies are recorded without saving a partial file. |
| `--max-js` | `100` | Maximum homepage-discovered script URLs per target. |
| `--insecure` | off | Opt in to unverified TLS for lab certificates. |
| `--discover-js` | on | Use `--no-discover-js` to skip homepage script discovery. |
| `--backups` | on | Use `--no-backups` to skip generated `.bak` variants. Built-in backup paths still run unless `--only-custom` is used. |
| `--soft-404` | on | Compare successful bodies to one random missing-page response. |

Redirects are limited to five hops and to the selected origin (scheme, hostname,
and port). Cross-origin redirects are reported; supply the destination origin as
another target if needed. A homepage redirect or non-success status does not
prevent checks of explicit paths. With an explicit scheme, even a homepage network
failure allows candidate checks to continue; automatic scheme selection skips a
target if both homepage requests fail. The scanner issues GET requests, does not retry,
and ignores environment proxy settings. Cookies are managed by the HTTP client
using standard cookie scope; authentication and custom headers are not yet exposed.

Soft-404 detection compares response bodies after replacing reflected request
URLs and paths. It is a heuristic: dynamic errors, login pages, and application
fallbacks can still be saved. HTML responses receive a review note. Disable the
heuristic with `--no-soft-404` to save all successful responses. This implementation
does not claim to prove file existence based on HTTP status alone.

## Output

```text
artifacts/
  20260925T020000Z-a1b2c3d4/
    report.json
    http_localhost_8080-<target-hash>/
      robots.txt--<url-hash>
      app.js--<url-hash>
```

Every invocation creates a unique run directory. Names are sanitized and include
URL hashes to distinguish hosts, paths, and query strings. Response bodies are
saved after HTTP decompression. The run directory is private and files are owner
read/write on POSIX systems. File extensions are followed by a hash; the report
maps each file to its exact requested and final URL.

`report.json` includes settings, timestamps, completion flags, homepage and
missing-page probes, candidate status codes, content types, verdicts, saved paths,
byte counts, and SHA-256 hashes. Paths are relative to the report directory.
No response headers containing cookies are included. Verdicts include `saved`,
`missing`, `soft_404`, `http_error`, `network_error`, `blocked_redirect`,
`redirect_error`, and `too_large`.

Exit statuses: `0` = scan completed without candidate errors (missing files and
soft 404s are normal), `1` = unreachable target or candidate request/HTTP error,
`2` = invalid input or filesystem failure, `130` = interrupted. A `403` indicates
access was denied; it is recorded without assuming the file exists or is missing.
Reports from interrupted or failed runs have `completed: false`.

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check .
python -m ruff format --check .
```

See [architecture and module authoring](docs/architecture.md). Tests use local
HTTP servers and mock transports; they do not contact external targets.

Web collection is the first implemented capability. The module interface is
intended to accommodate Linux assessment modules next; none are included yet.
