"""
publish_to_wiki.py
==================
Publishes generated .docx files to Azure DevOps Wiki — one page per project.

Reads project definitions from config.json. Each project must have a
``wiki_page_path`` field specifying the target wiki page.

Usage (in Azure DevOps Pipeline):
    python scripts/publish_to_wiki.py

Required environment variables:
    SYSTEM_ACCESSTOKEN   — built-in pipeline token (set via env: SYSTEM_ACCESSTOKEN: $(System.AccessToken))
    ADO_ORG_URL          — e.g. https://dev.azure.com/myorg
    ADO_PROJECT          — e.g. MyProject
    ADO_WIKI_ID          — wiki identifier (from the wiki URL in Azure DevOps)

Optional:
    CONFIG_PATH          — path to config.json (default: config.json)
"""

from __future__ import annotations

import json
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


def load_config(config_path: str = "config.json") -> dict:
    """Load and return config.json."""
    path = Path(config_path)
    if not path.exists():
        logger.error("Config file not found: %s", path.resolve())
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def find_latest_docx(output_dir: Path) -> Path | None:
    """Return the most recently modified .docx under output_dir, or None."""
    candidates = sorted(output_dir.rglob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def find_latest_additional_points(output_dir: Path) -> Path | None:
    """Return the most recently modified *_additional_points.md, or None."""
    candidates = sorted(
        output_dir.rglob("*_additional_points.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
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


def _build_wiki_page_content(project_name: str, docx_name: str, generated_at: str, additional_points: str) -> str:
    """Return the markdown content for the wiki page."""
    additional_points_block = additional_points.strip() or "No additional documentation points for this run."
    return (
        f"# Automated Documentation (AUD) — {project_name}\n\n"
        f"> Last updated: {generated_at}\n\n"
        "The latest **Architecture Understanding Document** is attached below.\n\n"
        f"[[_attachments/{docx_name}]]\n\n"
        "## Additional Points To Add\n\n"
        f"{additional_points_block}\n\n"
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
    project_name: str,
    docx_name: str,
    generated_at: str,
    additional_points: str,
) -> None:
    """Create or update a wiki page with a link to the .docx attachment."""
    url = f"{base_url}/_apis/wiki/wikis/{wiki_id}/pages"
    params = {"path": page_path, "api-version": API_VERSION}
    content = _build_wiki_page_content(project_name, docx_name, generated_at, additional_points)

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


def publish_project(
    session: requests.Session,
    base_url: str,
    wiki_id: str,
    project: dict,
    generated_at: str,
) -> bool:
    """Publish a single project's .docx to its wiki page.

    Returns True if published, False if skipped (no .docx found).
    """
    name: str = project["name"]
    output_dir = Path(project.get("output_dir", f"output/{name}"))
    page_path: str | None = project.get("wiki_page_path")

    if not page_path:
        logger.warning("[%s] No wiki_page_path configured — skipping wiki publish.", name)
        return False

    docx_path = find_latest_docx(output_dir)
    if docx_path is None:
        logger.info(
            "[%s] No .docx found in '%s' — pipeline likely skipped (no file changes). Nothing to publish.",
            name, output_dir,
        )
        return False

    logger.info("[%s] Publishing: %s → %s", name, docx_path, page_path)

    additional_points_path = find_latest_additional_points(output_dir)
    additional_points_text = ""
    if additional_points_path and additional_points_path.exists():
        additional_points_text = additional_points_path.read_text(encoding="utf-8", errors="replace")

    upload_attachment(session, base_url, wiki_id, docx_path)
    upsert_wiki_page(
        session,
        base_url,
        wiki_id,
        page_path,
        name,
        docx_path.name,
        generated_at,
        additional_points_text,
    )

    logger.info("[%s] Done — '%s' published to '%s'.", name, docx_path.name, page_path)
    return True


def main() -> None:
    token = _require_env("SYSTEM_ACCESSTOKEN")
    org_url = _require_env("ADO_ORG_URL").rstrip("/")
    project_name = _require_env("ADO_PROJECT")
    wiki_id = _require_env("ADO_WIKI_ID")
    config_path = os.environ.get("CONFIG_PATH", "config.json")

    base_url = f"{org_url}/{project_name}"

    config = load_config(config_path)
    projects: list[dict] = config.get("projects", [])

    if not projects:
        logger.warning("No projects defined in %s — nothing to publish.", config_path)
        sys.exit(0)

    session = requests.Session()
    session.auth = ("", token)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    published = 0
    for proj in projects:
        if publish_project(session, base_url, wiki_id, proj, generated_at):
            published += 1

    if published == 0:
        logger.info("No documents published — all projects were skipped (no changes detected).")
    else:
        logger.info("Published %d project(s) to the wiki.", published)


if __name__ == "__main__":
    main()

