"""
template_writer.py
==================
Word document (.docx) placeholder injection for the Intelligent Documentation Engine.

Responsibilities:
  - Load the AUD_template.docx master template.
  - Replace every {{PLACEHOLDER}} token in paragraphs, table cells, and headers/
    footers with the corresponding generated text.
  - Save the filled document to the appropriate versioned output path.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn

logger = logging.getLogger(__name__)

# Pattern that matches {{SOME_KEY}} tokens (uppercase letters and underscores only)
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z_]+)\}\}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _replace_in_paragraph(paragraph: object, replacements: dict[str, str]) -> None:
    """Replace all placeholder tokens in *paragraph* in-place.

    Operates on the paragraph's XML runs to preserve formatting while still
    substituting tokens that may have been split across runs by Word's XML
    serialiser.

    Args:
        paragraph:    A ``docx.text.paragraph.Paragraph`` object.
        replacements: Mapping of placeholder key (without braces) → replacement text.
    """
    # Merge all run text so we can detect multi-run tokens
    full_text = "".join(run.text for run in paragraph.runs)

    if not _PLACEHOLDER_RE.search(full_text):
        return  # Nothing to do

    def substitute(match: re.Match) -> str:  # type: ignore[type-arg]
        key = match.group(1)
        value = replacements.get(key, match.group(0))  # leave token if key missing
        if not replacements.get(key):
            logger.debug("No replacement value for placeholder {{%s}} — leaving as-is", key)
        return value

    new_text = _PLACEHOLDER_RE.sub(substitute, full_text)

    # Write the merged text into the first run, clear the rest
    if paragraph.runs:
        paragraph.runs[0].text = new_text
        for run in paragraph.runs[1:]:
            run.text = ""


def _replace_in_document(doc: object, replacements: dict[str, str]) -> None:
    """Walk every text-bearing element in *doc* and apply *replacements*.

    Covers body paragraphs, table cells, and header/footer paragraphs.

    Args:
        doc:          A ``docx.Document`` instance.
        replacements: Mapping of placeholder key → replacement text.
    """
    # Body paragraphs
    for para in doc.paragraphs:  # type: ignore[union-attr]
        _replace_in_paragraph(para, replacements)

    # Table cells
    for table in doc.tables:  # type: ignore[union-attr]
        for row in table.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    _replace_in_paragraph(para, replacements)

    # Headers and footers for each section
    for section in doc.sections:  # type: ignore[union-attr]
        for hf in (section.header, section.footer):
            if hf is not None:
                for para in hf.paragraphs:
                    _replace_in_paragraph(para, replacements)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_document(
    template_path: str,
    output_path: str,
    replacements: dict[str, str],
) -> Path:
    """Fill the Word template and write the result to *output_path*.

    Copies the template to *output_path* (creating parent directories as
    needed) then performs in-place placeholder substitution before saving.

    Args:
        template_path: Path to the ``AUD_template.docx`` master file.
        output_path:   Destination path for the filled document.
        replacements:  Mapping of placeholder key (without ``{{`` / ``}}``)
                       to the text that should replace it.

    Returns:
        The resolved :class:`pathlib.Path` of the written document.

    Raises:
        FileNotFoundError: If *template_path* does not exist.
        OSError:            If the output file cannot be written.
    """
    tmpl = Path(template_path)
    if not tmpl.exists():
        raise FileNotFoundError(f"Template not found: {tmpl}")

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    # Work on a copy so the master template is never modified
    shutil.copy2(tmpl, out)
    logger.debug("Copied template %s → %s", tmpl, out)

    doc: Document = Document(str(out))
    _replace_in_document(doc, replacements)
    doc.save(str(out))

    logger.info("Document written to %s", out)
    return out.resolve()
