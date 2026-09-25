"""Collect potentially useful public web files for offline manual review."""

import asyncio
import hashlib
import html
import math
import uuid
from collections import Counter
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import httpx
from rich.console import Console

from fieldkit import __version__
from fieldkit.core.http import Fetcher, FetchResult, origin
from fieldkit.core.module import Module, Option
from fieldkit.core.storage import ArtifactStore, utc_now
from fieldkit.core.targets import read_targets, target_urls

DEFAULT_PATHS = (
    "/robots.txt",
    "/.well-known/security.txt",
    "/security.txt",
    "/secrets.txt",
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/JS",
    "/JS/",
    "/js/",
    "/.js",
    "/.bak",
    "/app.js",
    "/main.js",
    "/index.js",
    "/script.js",
    "/scripts.js",
    "/js/app.js",
    "/js/main.js",
    "/js/scripts.js",
    "/assets/app.js",
    "/assets/main.js",
    "/static/js/main.js",
    "/index.html.bak",
    "/index.php.bak",
    "/config.php.bak",
    "/web.config.bak",
)


class ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.base: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "base" and self.base is None and attributes.get("href"):
            self.base = attributes["href"]
        if tag == "script" and attributes.get("src"):
            self.references.append(attributes["src"])
        if tag == "link" and attributes.get("href"):
            href = attributes["href"]
            try:
                is_script = urlsplit(href).path.lower().endswith((".js", ".mjs"))
            except ValueError:
                return
            if is_script:
                self.references.append(href)


def discover_scripts(page: FetchResult, target: str) -> list[str]:
    parser = ScriptParser()
    parser.feed(page.body.decode("utf-8", errors="replace"))
    try:
        base = urljoin(page.url, parser.base) if parser.base else page.url
    except ValueError:
        base = page.url
    urls: list[str] = []
    for reference in parser.references:
        try:
            url = urldefrag(urljoin(base, reference))[0]
            parsed = urlsplit(url)
            if (
                origin(url) == origin(target)
                and parsed.username is None
                and parsed.password is None
                and url not in urls
            ):
                urls.append(url)
        except ValueError:
            continue
    return urls


def validate_path(path: str) -> str:
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.netloc
        or parsed.scheme
        or parsed.fragment
        or "\\" in path
        or any(c.isspace() for c in path)
    ):
        raise ValueError(f"Paths must be root-relative, e.g. /assets/site.js: {path}")
    return path


def backup_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(parsed._replace(path=parsed.path + ".bak"))


def fingerprint(result: FetchResult) -> str:
    """Recognize a common fallback body, including reflected missing paths."""
    body = result.body
    for url in dict.fromkeys([result.requested_url, result.url]):
        path = urlsplit(url).path
        for value in [url, html.escape(url), path, html.escape(path)]:
            if value and value != "/":
                body = body.replace(value.encode(), b"<REQUEST>")
    return hashlib.sha256(body).hexdigest()


class WebArtifacts(Module):
    name = "web/artifacts"
    description = "Collect robots, security, secrets, sitemaps, JavaScript, and backup files."
    accepts_targets = True
    options = (
        Option("TARGETS", list, [], "Hostnames, IPs, or HTTP(S) origins", "--target"),
        Option(
            "TARGETS_FILE", str, "", "File of targets (one per line; # comments)", "--targets-file"
        ),
        Option("OUTPUT", str, "artifacts", "Parent directory for a unique run folder", "--output"),
        Option(
            "SCHEME", str, "auto", "auto, https, or http for targets without a scheme", "--scheme"
        ),
        Option("PATHS", list, [], "Extra root-relative paths (repeat this flag)", "--path"),
        Option("PATHS_FILE", str, "", "File of extra root-relative paths", "--paths-file"),
        Option(
            "ONLY_CUSTOM", bool, False, "Skip built-in paths; use supplied paths", "--only-custom"
        ),
        Option(
            "DISCOVER_JS", bool, True, "Discover scripts from the target homepage", "--discover-js"
        ),
        Option("BACKUPS", bool, True, "Also probe .bak variants of candidate files", "--backups"),
        Option(
            "SOFT_404", bool, True, "Compare successful bodies against a missing page", "--soft-404"
        ),
        Option(
            "CONCURRENCY", int, 8, "Maximum simultaneous requests across targets", "--concurrency"
        ),
        Option(
            "TIMEOUT",
            float,
            10.0,
            "Wall-clock seconds per request, including redirects",
            "--timeout",
        ),
        Option("MAX_BYTES", int, 5242880, "Maximum decoded bytes per response", "--max-bytes"),
        Option("MAX_JS", int, 100, "Maximum discovered script URLs per target", "--max-js"),
        Option("INSECURE", bool, False, "Disable TLS certificate verification", "--insecure"),
    )

    def run(self, settings: dict[str, Any], console: Console) -> int:
        targets = read_targets(settings["TARGETS"], settings["TARGETS_FILE"])
        if settings["SCHEME"] not in {"auto", "http", "https"}:
            raise ValueError("SCHEME must be auto, http, or https")
        for name in ("CONCURRENCY", "TIMEOUT", "MAX_BYTES", "MAX_JS"):
            if not math.isfinite(settings[name]) or settings[name] <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        # Validate all inputs before any request or output-directory creation.
        normalized: list[tuple[str, list[str]]] = []
        seen: set[tuple[str, ...]] = set()
        for target in targets:
            urls = target_urls(target, settings["SCHEME"])
            try:
                key = tuple(str(httpx.URL(url)) for url in urls)
            except httpx.InvalidURL as exc:
                raise ValueError(f"Invalid target: {target}: {exc}") from exc
            if key not in seen:
                normalized.append((target, list(key)))
                seen.add(key)
        paths = list(settings["PATHS"])
        if settings["PATHS_FILE"]:
            from pathlib import Path

            paths.extend(
                line.strip()
                for line in Path(settings["PATHS_FILE"]).expanduser().read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        paths = list(dict.fromkeys(validate_path(path) for path in paths))
        if settings["ONLY_CUSTOM"] and not paths:
            raise ValueError("--only-custom requires --path or --paths-file")
        store = ArtifactStore(settings["OUTPUT"])
        report: dict[str, Any] = {
            "schema_version": 1,
            "module": self.name,
            "started_at": utc_now(),
            "completed": False,
            "settings": settings,
            "targets": [],
        }
        console.print(f"Run directory: {store.root}", markup=False)
        interrupted = False
        try:
            asyncio.run(self._scan(normalized, paths, settings, store, report, console))
            report["completed"] = True
        except KeyboardInterrupt:
            interrupted = True
            console.print("Scan interrupted; saving partial report.")
        finally:
            report["finished_at"] = utc_now()
            manifest = store.write_report(report)
            console.print(f"Report: {manifest}", markup=False)
        if interrupted:
            return 130
        return 1 if any(t["had_errors"] for t in report["targets"]) else 0

    async def _scan(
        self,
        targets: list[tuple[str, list[str]]],
        paths: list[str],
        settings: dict[str, Any],
        store: ArtifactStore,
        report: dict,
        console: Console,
    ) -> None:
        async with httpx.AsyncClient(
            verify=not settings["INSECURE"],
            timeout=settings["TIMEOUT"],
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": f"Fieldkit/{__version__} (web artifact collector)"},
            limits=httpx.Limits(max_connections=settings["CONCURRENCY"]),
        ) as client:
            fetcher = Fetcher(
                client,
                settings["CONCURRENCY"],
                settings["MAX_BYTES"],
                settings["TIMEOUT"],
            )
            # Limit target workers too: a large targets file does not spawn unbounded tasks.
            queue: asyncio.Queue = asyncio.Queue()
            for target in targets:
                queue.put_nowait(target)

            async def worker() -> None:
                while not queue.empty():
                    target, urls = queue.get_nowait()
                    await self._scan_target(
                        target,
                        urls,
                        paths,
                        settings,
                        fetcher,
                        store,
                        report,
                        console,
                    )
                    queue.task_done()

            await asyncio.gather(
                *(worker() for _ in range(min(settings["CONCURRENCY"], len(targets))))
            )

    async def _scan_target(
        self,
        target: str,
        urls: list[str],
        custom_paths: list[str],
        settings: dict[str, Any],
        fetcher: Fetcher,
        store: ArtifactStore,
        report: dict,
        console: Console,
    ) -> None:
        entry: dict[str, Any] = {
            "input": target,
            "origin": None,
            "completed": False,
            "had_errors": False,
            "probes": [],
            "results": [],
            "notes": [],
        }
        report["targets"].append(entry)
        home = None
        for base in urls:
            home = await fetcher.get(base, base)
            entry["probes"].append(self._record(home, "homepage"))
            if home.error != "network_error":
                break
        assert home is not None
        entry["origin"] = base
        if home.error == "network_error" and len(urls) > 1:
            entry["had_errors"] = True
            entry["completed"] = True
            console.print(f"[error] {target}: {home.detail}", markup=False)
            return
        if len(entry["probes"]) > 1:
            entry["notes"].append("HTTPS failed; used HTTP fallback.")
        if home.error:
            entry["notes"].append(f"Homepage probe: {home.error}; explicit paths still checked.")
        console.print(f"[scan] {base}", markup=False)

        baseline = None
        if settings["SOFT_404"]:
            missing = urljoin(base, f"/.fieldkit-missing-{uuid.uuid4().hex}")
            baseline = await fetcher.get(missing, base)
            entry["probes"].append(self._record(baseline, "missing-page baseline"))
            if baseline.error:
                entry["notes"].append(
                    "Missing-page baseline unavailable; soft-404 detection limited."
                )
        paths = custom_paths if settings["ONLY_CUSTOM"] else [*DEFAULT_PATHS, *custom_paths]
        candidates = dict.fromkeys(urljoin(base, path) for path in paths)
        if settings["DISCOVER_JS"] and home.status and 200 <= home.status < 300 and not home.error:
            discovered = discover_scripts(home, base)
            candidates.update(dict.fromkeys(discovered[: settings["MAX_JS"]]))
            entry["discovered_js"] = min(len(discovered), settings["MAX_JS"])
            if len(discovered) > settings["MAX_JS"]:
                entry["notes"].append(f"Script discovery limited to {settings['MAX_JS']} URLs.")
        if settings["BACKUPS"]:
            candidates.update(
                dict.fromkeys(
                    backup_url(url)
                    for url in list(candidates)
                    if not urlsplit(url).path.endswith(("/", ".bak"))
                )
            )
        baseline_hash = (
            fingerprint(baseline) if baseline and not baseline.error and baseline.body else None
        )

        async def collect(url: str) -> None:
            result = await fetcher.get(url, base)
            record = self._record(result, "candidate")
            if result.error:
                verdict = result.error
                entry["had_errors"] = True
            elif result.status in {404, 410}:
                verdict = "missing"
            elif result.status is not None and 200 <= result.status < 300:
                if baseline_hash and result.body and fingerprint(result) == baseline_hash:
                    verdict = "soft_404"
                else:
                    verdict = "saved"
                    record.update(store.save(base, url, result.body))
                    if "html" in result.content_type.lower():
                        record["note"] = (
                            "HTML response; manually check for login or fallback pages."
                        )
                    console.print(f"[saved] {url} ({len(result.body)} bytes)", markup=False)
            else:
                verdict = "http_error"
                entry["had_errors"] = True
            record["verdict"] = verdict
            entry["results"].append(record)

        # Fixed workers prevent large path lists from creating a task per candidate.
        candidate_queue: asyncio.Queue[str] = asyncio.Queue()
        for url in candidates:
            candidate_queue.put_nowait(url)

        async def collect_worker() -> None:
            while not candidate_queue.empty():
                await collect(candidate_queue.get_nowait())
                candidate_queue.task_done()

        await asyncio.gather(
            *(collect_worker() for _ in range(min(settings["CONCURRENCY"], len(candidates))))
        )
        entry["results"].sort(key=lambda item: item["requested_url"])
        counts = dict(Counter(r["verdict"] for r in entry["results"]))
        entry["counts"] = counts
        entry["completed"] = True
        console.print(f"[done] {base} {counts}", markup=False)

    @staticmethod
    def _record(result: FetchResult, purpose: str) -> dict:
        return {
            "purpose": purpose,
            "requested_url": result.requested_url,
            "final_url": result.url,
            "status": result.status,
            "content_type": result.content_type,
            "error": result.error,
            "detail": result.detail,
        }
