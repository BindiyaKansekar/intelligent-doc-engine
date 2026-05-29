"""
publish_to_wiki.py
==================
Publishes the latest generated .docx file to an Azure DevOps Wiki page as an attachment.

Usage (in Azure DevOps Pipeline):
    python scripts/publish_to_wiki.py

Required environment variables:
    SYSTEM_ACCESSTOKEN   — built-in pipeline token (set via env: SYSTEM_ACCESSTOKEN: $(System.AccessToken))
    ADO_ORG_URL          — e.g. https://dev.azure.com/myorg
    ADO_PROJECT          — e.g. MyProject
    ADO_WIKI_ID          — wiki identifier (from the wiki URL in Azure DevOps)
    ADO_WIKI_PAGE_PATH   — wiki page path to create/update, e.g. /Documentation/AUD

Optional:
    OUTPUT_DIR           — root output directory (default: output)
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

API_VERSION = "7.1"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        logger.error("Required environment variable '%s' is not set.", name)
        sys.exit(1)
    return value


def find_latest_docx(output_dir: Path) -> Path | None:
    """Return the most recently modified .docx under output_dir, or None."""
    candidates = sorted(output_dir.rglob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def upload_attachment(session: requests.Session, base_url: str, wiki_id: str, file_path: Path) -> str:
    """Upload file_path as a wiki attachment. Returns the attachment download URL."""
    url = f"{base_url}/_apis/wiki/wikis/{wiki_id}/attachments"
    params = {"fileName": file_path.name, "api-version": API_VERSION}

    with file_path.open("rb") as fh:
        content = fh.read()

    resp = session.put(url, params=params, data=content, headers={"Content-Type": "application/octet-stream"})
    resp.raise_for_status()

    data = resp.json()
    attachment_url = data.get("path", file_path.name)
    logger.info("Attachment uploaded: %s", attachment_url)
    return attachment_url


def _build_wiki_page_content(docx_name: str, generated_at: str) -> str:
    """Return the markdown content for the wiki page."""
    return (
        "# Automated Documentation (AUD)\n\n"
        f"> Last updated: {generated_at}\n\n"
        "The latest **Architecture Understanding Document** is attached below.\n\n"
        f"[[_attachments/{docx_name}]]\n\n"
        "---\n"
        "_This page is automatically updated by the [intelligent-doc-engine](https://github.com) "
        "pipeline whenever source files change._\n"
    )


def get_page_etag(session: requests.Session, base_url: str, wiki_id: str, page_path: str) -> str | None:
    """Return the current ETag for a wiki page, or None if it doesn't exist yet."""
    url = f"{base_url}/_apis/wiki/wikis/{wiki_id}/pages"
    params = {"path": page_path, "api-version": API_VERSION}
    resp = session.get(url, params=params)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.headers.get("ETag")


def upsert_wiki_page(
    session: requests.Session,
    base_url: str,
    wiki_id: str,
    page_path: str,
    docx_name: str,
    generated_at: str,
) -> None:
    """Create or update a wiki page with a link to the .docx attachment."""
    url = f"{base_url}/_apis/wiki/wikis/{wiki_id}/pages"
    params = {"path": page_path, "api-version": API_VERSION}
    content = _build_wiki_page_content(docx_name, generated_at)

    headers: dict[str, str] = {"Content-Type": "application/json"}

    etag = get_page_etag(session, base_url, wiki_id, page_path)
    if etag:
        headers["If-Match"] = etag
        logger.info("Updating existing wiki page '%s' (ETag: %s)", page_path, etag)
    else:
        headers["If-Match"] = "*"
        logger.info("Creating new wiki page '%s'", page_path)

    resp = session.put(url, params=params, json={"content": content}, headers=headers)
    resp.raise_for_status()
    logger.info("Wiki page '%s' upserted successfully.", page_path)


def main() -> None:
    token = _require_env("SYSTEM_ACCESSTOKEN")
    org_url = _require_env("ADO_ORG_URL").rstrip("/")
    project = _require_env("ADO_PROJECT")
    wiki_id = _require_env("ADO_WIKI_ID")
    page_path = _require_env("ADO_WIKI_PAGE_PATH")
    output_dir = Path(os.environ.get("OUTPUT_DIR", "output"))

    base_url = f"{org_url}/{project}"

    docx_path = find_latest_docx(output_dir)
    if docx_path is None:
        logger.info(
            "No .docx found in '%s' — pipeline likely skipped (no file changes). Nothing to publish.",
            output_dir,
        )
        sys.exit(0)

    logger.info("Found document to publish: %s", docx_path)

    session = requests.Session()
    session.auth = ("", token)  # PAT / system token auth

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    upload_attachment(session, base_url, wiki_id, docx_path)
    upsert_wiki_page(session, base_url, wiki_id, page_path, docx_path.name, generated_at)

    logger.info("Done — '%s' published to wiki page '%s'.", docx_path.name, page_path)


if __name__ == "__main__":
    main()
