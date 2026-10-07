"""Official migration-document retrieval with a local cache."""

from __future__ import annotations

import hashlib
import html
import re
import urllib.error
import urllib.request
from pathlib import Path


OFFICIAL_DOCUMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "pydantic": (
        ("Migration guide", "https://docs.pydantic.dev/latest/migration/"),
        ("Pydantic changelog", "https://docs.pydantic.dev/latest/changelog/"),
    ),
    "sqlalchemy": (
        ("SQLAlchemy 2.0 migration guide", "https://docs.sqlalchemy.org/en/20/changelog/migration_20.html"),
        ("SQLAlchemy changelog", "https://docs.sqlalchemy.org/en/20/changelog/"),
    ),
}


def _plain_text(document: str) -> str:
    document = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", document)
    document = re.sub(r"(?s)<[^>]+>", " ", document)
    document = html.unescape(document)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", document)).strip()


def fetch_official_documents(
    package: str,
    cache_dir: Path,
    *,
    timeout_seconds: int = 8,
) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    cache_dir.mkdir(parents=True, exist_ok=True)
    for title, url in OFFICIAL_DOCUMENTS.get(package.lower(), ()):
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        cache_path = cache_dir / f"{package.lower()}-{digest}.txt"
        status = "cached" if cache_path.is_file() else ""
        error = ""
        if not status:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "codenova-migration/0.1"})
                with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                    raw = response.read(3_000_000).decode("utf-8", errors="replace")
                cache_path.write_text(_plain_text(raw), encoding="utf-8")
                status = "fetched"
            except (OSError, urllib.error.URLError, ValueError) as exc:
                status = "unavailable"
                error = str(exc)
        records.append({
            "title": title,
            "url": url,
            "status": status,
            "cache_path": str(cache_path) if cache_path.is_file() else "",
            "error": error,
        })
    return records


def document_links(package: str) -> list[dict[str, str]]:
    return [
        {"title": title, "url": url, "status": "not-fetched", "cache_path": "", "error": ""}
        for title, url in OFFICIAL_DOCUMENTS.get(package.lower(), ())
    ]


__all__ = ["document_links", "fetch_official_documents"]
