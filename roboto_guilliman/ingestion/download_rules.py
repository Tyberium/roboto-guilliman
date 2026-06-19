"""Download public Warhammer 40,000 rules PDFs from Warhammer Community."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INDEX_URL = "https://www.warhammer-community.com/en-gb/downloads/warhammer-40000/"
DOWNLOADS_API_URL = "https://www.warhammer-community.com/api/search/downloads/"
DEFAULT_GAME_SYSTEM = "warhammer-40000"
DEFAULT_LANGUAGE = "english"
DEFAULT_OUTPUT_DIR = Path("data/rules_pdfs")
MANIFEST_NAME = "manifest.json"
USER_AGENT = (
    "roboto-guilliman/0.1 (+https://github.com/Tyberium/roboto-guilliman; rules-ingest-tool)"
)
DEFAULT_DELAY_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 120.0
PDF_HOST = "assets.warhammer-community.com"


@dataclass(frozen=True)
class DownloadEntry:
    title: str
    url: str
    filename: str
    category: str


@dataclass
class ManifestRecord:
    title: str
    url: str
    filename: str
    category: str
    sha256: str
    bytes: int
    downloaded_at: str


def _title_from_url(url: str) -> str:
    stem = Path(url.split("?", maxsplit=1)[0]).stem
    return stem.replace("_", " ").replace("-", " ")


def _safe_filename(title: str, url: str) -> str:
    url_name = Path(url.split("?", maxsplit=1)[0]).name
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", title.lower()).strip("_")
    slug = slug[:80] if slug else "download"
    if url_name.lower().endswith(".pdf"):
        return f"{slug}__{url_name}"
    return f"{slug}.pdf"


def parse_api_hits(hits: list[dict[str, Any]]) -> list[DownloadEntry]:
    seen_urls: set[str] = set()
    entries: list[DownloadEntry] = []

    for hit in hits:
        file_name = hit.get("id", {}).get("file")
        if not file_name:
            continue
        url = f"https://{PDF_HOST}/{file_name}"
        if url in seen_urls:
            continue
        seen_urls.add(url)

        categories = hit.get("download_categories") or ["uncategorised"]
        category = categories[0] if categories else "uncategorised"
        title = hit.get("title") or _title_from_url(url)
        entries.append(
            DownloadEntry(
                title=title,
                url=url,
                filename=_safe_filename(title, url),
                category=category,
            )
        )
    return entries


def discover_from_api(
    *,
    game_system: str,
    language: str,
    timeout: float,
) -> list[DownloadEntry]:
    payload = {
        "index": "downloads_v2",
        "searchTerm": "",
        "gameSystem": game_system,
        "language": language,
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        DOWNLOADS_API_URL,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)

    hits = data.get("hits", [])
    if not isinstance(hits, list):
        raise ValueError("Unexpected downloads API response: missing hits list")
    return parse_api_hits(hits)


def _request(url: str, *, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_manifest(path: Path) -> dict[str, ManifestRecord]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    records: dict[str, ManifestRecord] = {}
    for item in raw:
        if "category" not in item:
            item["category"] = "uncategorised"
        record = ManifestRecord(**item)
        records[record.url] = record
    return records


def _save_manifest(path: Path, records: dict[str, ManifestRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(record) for record in records.values()]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def download_pdf(
    entry: DownloadEntry,
    *,
    output_dir: Path,
    timeout: float,
    force: bool,
    manifest: dict[str, ManifestRecord],
) -> tuple[str, ManifestRecord]:
    output_path = output_dir / entry.filename
    existing = manifest.get(entry.url)
    if not force and output_path.exists() and existing and existing.sha256:
        logger.info("Skip (already downloaded): %s", entry.title)
        return "skipped", existing

    data = _request(entry.url, timeout=timeout)
    digest = _sha256(data)
    if (
        not force
        and existing
        and existing.sha256 == digest
        and output_path.exists()
    ):
        logger.info("Skip (unchanged): %s", entry.title)
        return "skipped", existing

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)
    record = ManifestRecord(
        title=entry.title,
        url=entry.url,
        filename=entry.filename,
        category=entry.category,
        sha256=digest,
        bytes=len(data),
        downloaded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    logger.info("Downloaded %s (%s bytes)", entry.title, len(data))
    return "downloaded", record


def download_all(
    *,
    game_system: str,
    language: str,
    output_dir: Path,
    delay_seconds: float,
    timeout: float,
    force: bool,
    dry_run: bool,
) -> tuple[int, int, int]:
    logger.info(
        "Querying downloads API (game_system=%s, language=%s)",
        game_system,
        language,
    )
    entries = discover_from_api(
        game_system=game_system,
        language=language,
        timeout=timeout,
    )
    logger.info("Found %s PDF links", len(entries))

    manifest_path = output_dir / MANIFEST_NAME
    manifest = _load_manifest(manifest_path)

    downloaded = 0
    skipped = 0
    failed = 0

    for index, entry in enumerate(entries, start=1):
        logger.info("[%s/%s] %s", index, len(entries), entry.title)
        if dry_run:
            logger.info(
                "Dry run: would download %s -> %s (%s)",
                entry.url,
                entry.filename,
                entry.category,
            )
            continue

        attempt = 0
        while True:
            try:
                action, record = download_pdf(
                    entry,
                    output_dir=output_dir,
                    timeout=timeout,
                    force=force,
                    manifest=manifest,
                )
                manifest[entry.url] = record
                if action == "downloaded":
                    downloaded += 1
                else:
                    skipped += 1
                break
            except urllib.error.HTTPError as exc:
                attempt += 1
                if exc.code in {429, 503} and attempt <= 5:
                    backoff = min(delay_seconds * (2**attempt), MAX_BACKOFF_SECONDS)
                    logger.warning(
                        "HTTP %s for %s; backing off %.0fs (attempt %s/5)",
                        exc.code,
                        entry.title,
                        backoff,
                        attempt,
                    )
                    time.sleep(backoff)
                    continue
                logger.error("Failed %s: HTTP %s", entry.title, exc.code)
                failed += 1
                break
            except urllib.error.URLError as exc:
                logger.error("Failed %s: %s", entry.title, exc.reason)
                failed += 1
                break

        if not dry_run and index < len(entries):
            time.sleep(delay_seconds)

    if not dry_run:
        _save_manifest(manifest_path, manifest)

    return downloaded, skipped, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Politely download Warhammer 40,000 rules PDFs from Warhammer Community. "
            "PDFs are saved locally and never committed to git."
        ),
    )
    parser.add_argument(
        "--game-system",
        default=DEFAULT_GAME_SYSTEM,
        help="Warhammer Community game system slug (default: warhammer-40000).",
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help="Download language (default: english).",
    )
    parser.add_argument(
        "--index-url",
        default=DEFAULT_INDEX_URL,
        help="Reference page for humans; discovery uses the downloads API.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for downloaded PDFs and manifest.json.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Seconds to wait between PDF downloads (default: 5).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout per request in seconds.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the file already exists.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List discovered PDFs without downloading.",
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args()
    downloaded, skipped, failed = download_all(
        game_system=args.game_system,
        language=args.language,
        output_dir=args.output_dir,
        delay_seconds=args.delay,
        timeout=args.timeout,
        force=args.force,
        dry_run=args.dry_run,
    )
    logger.info(
        "Done: downloaded=%s skipped=%s failed=%s (output=%s)",
        downloaded,
        skipped,
        failed,
        args.output_dir,
    )
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
