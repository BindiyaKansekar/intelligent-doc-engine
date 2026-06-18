"""
mermaid_renderer.py
===================
Render Mermaid diagrams to PNG images for embedding in Word documents.

Renderer chain (auto mode):
  1. mmdc (mermaid-cli)       — local, fast, needs Node + puppeteer
  2. kroki.io POST API        — hosted, handles large diagrams, no URL limit
  3. mermaid.ink GET API      — hosted, simple, fails on very large diagrams

Returns None if no renderer succeeds — caller should embed a placeholder.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

_MERMAID_FENCE_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.DOTALL)

# kroki.io endpoint — supports POST with plain-text body, no URL length limit
_KROKI_URL = "https://kroki.io/mermaid/png"
# mermaid.ink — GET only, works for small/medium diagrams
_MERMAID_INK_URL = "https://mermaid.ink/img/{encoded}"
# Diagrams larger than this byte threshold skip mermaid.ink GET to avoid 414
_MERMAID_INK_MAX_BYTES = 3000


def extract_mermaid_code(text: str) -> str | None:
    """Extract Mermaid diagram code from a fenced code block."""
    match = _MERMAID_FENCE_RE.search(text)
    return match.group(1).strip() if match else None


def render_mermaid_to_png(
    mermaid_text: str,
    output_path: str,
    renderer: str = "auto",
    timeout: int = 60,
) -> Path | None:
    """Render Mermaid text to a PNG image.

    Args:
        mermaid_text: Raw Mermaid diagram code (without fences).
        output_path:  Destination PNG path.
        renderer:     "mmdc", "kroki", "web", or "auto" (try all in order).
        timeout:      Seconds before timing out per renderer.

    Returns:
        Path to PNG on success, None on failure.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    if renderer in ("mmdc", "auto"):
        result = _render_via_mmdc(mermaid_text, str(out), timeout)
        if result:
            return result
        if renderer == "mmdc":
            return None

    if renderer in ("kroki", "auto"):
        result = _render_via_kroki(mermaid_text, str(out), timeout)
        if result:
            return result
        if renderer == "kroki":
            return None

    if renderer in ("web", "auto"):
        result = _render_via_mermaid_ink(mermaid_text, str(out), timeout)
        if result:
            return result

    logger.warning("All Mermaid renderers failed for diagram (%d chars)", len(mermaid_text))
    return None


def render_mermaid_to_png_with_fallback(
    mermaid_text: str,
    output_path: str,
    simple_mermaid_text: str | None = None,
    timeout: int = 60,
) -> Path | None:
    """Try rendering *mermaid_text*; if that fails and *simple_mermaid_text* is
    provided, retry with the simplified version.  Useful for very large graphs."""
    result = render_mermaid_to_png(mermaid_text, output_path, timeout=timeout)
    if result:
        return result
    if simple_mermaid_text:
        logger.info("Retrying with simplified diagram")
        result = render_mermaid_to_png(simple_mermaid_text, output_path, timeout=timeout)
    return result


# ── Renderer 1: mmdc (mermaid-cli) ───────────────────────────────────────────

def _render_via_mmdc(mermaid_text: str, output_path: str, timeout: int) -> Path | None:
    """Render via mermaid-cli (mmdc). Injects --no-sandbox puppeteer config."""
    mmdc = shutil.which("mmdc")
    if not mmdc:
        logger.debug("mmdc not found on PATH")
        return None

    input_path: str | None = None
    puppeteer_cfg_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mmd", delete=False, encoding="utf-8"
        ) as f:
            f.write(mermaid_text)
            input_path = f.name

        # Create a puppeteer config that disables the Chrome sandbox — required on
        # many Windows/Linux CI environments where sandboxing is not available.
        puppeteer_cfg = {"args": ["--no-sandbox", "--disable-setuid-sandbox"]}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as pf:
            json.dump(puppeteer_cfg, pf)
            puppeteer_cfg_path = pf.name

        cmd = [
            mmdc,
            "-i", input_path,
            "-o", output_path,
            "-b", "white",
            "--width", "1200",
            "--height", "700",
            "-p", puppeteer_cfg_path,
        ]
        proc = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            timeout=timeout,
        )
        if Path(output_path).exists() and Path(output_path).stat().st_size > 0:
            logger.info("Rendered via mmdc → %s", output_path)
            return Path(output_path)
        logger.warning("mmdc succeeded but output file is empty/missing")
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        logger.warning("mmdc non-zero exit (%d): %s", exc.returncode, stderr or "(no stderr)")
    except subprocess.TimeoutExpired:
        logger.warning("mmdc timed out after %ds", timeout)
    except OSError as exc:
        logger.warning("mmdc OS error: %s", exc)
    finally:
        if input_path:
            Path(input_path).unlink(missing_ok=True)
        if puppeteer_cfg_path:
            Path(puppeteer_cfg_path).unlink(missing_ok=True)
    return None


# ── Renderer 2: kroki.io POST ─────────────────────────────────────────────────

def _render_via_kroki(mermaid_text: str, output_path: str, timeout: int) -> Path | None:
    """Render via kroki.io POST API. Handles arbitrarily large diagrams."""
    try:
        resp = requests.post(
            _KROKI_URL,
            data=mermaid_text.encode("utf-8"),
            headers={"Content-Type": "text/plain", "Accept": "image/png"},
            timeout=timeout,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        if "image" in content_type or len(resp.content) > 1000:
            Path(output_path).write_bytes(resp.content)
            logger.info("Rendered via kroki.io → %s", output_path)
            return Path(output_path)
        logger.warning("kroki.io returned unexpected content-type: %s", content_type)
    except requests.exceptions.HTTPError as exc:
        logger.warning("kroki.io HTTP error: %s", exc)
    except requests.exceptions.ConnectionError:
        logger.warning("kroki.io unreachable (no internet?)")
    except requests.exceptions.Timeout:
        logger.warning("kroki.io timed out after %ds", timeout)
    except Exception as exc:
        logger.warning("kroki.io unexpected error: %s", exc)
    return None


# ── Renderer 3: mermaid.ink GET ───────────────────────────────────────────────

def _render_via_mermaid_ink(mermaid_text: str, output_path: str, timeout: int) -> Path | None:
    """Render via mermaid.ink GET API. Skipped for large diagrams (414 risk)."""
    encoded = base64.urlsafe_b64encode(mermaid_text.encode()).decode()
    if len(encoded) > _MERMAID_INK_MAX_BYTES:
        logger.debug(
            "Skipping mermaid.ink GET — encoded size %d > %d byte limit",
            len(encoded), _MERMAID_INK_MAX_BYTES,
        )
        return None
    try:
        url = _MERMAID_INK_URL.format(encoded=encoded)
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        if resp.headers.get("content-type", "").startswith("image/"):
            Path(output_path).write_bytes(resp.content)
            logger.info("Rendered via mermaid.ink → %s", output_path)
            return Path(output_path)
        logger.warning("mermaid.ink non-image response: %s", resp.headers.get("content-type"))
    except requests.exceptions.HTTPError as exc:
        logger.warning("mermaid.ink HTTP error: %s", exc)
    except Exception as exc:
        logger.warning("mermaid.ink error: %s", exc)
    return None
