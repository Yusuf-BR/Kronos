import fitz  # pymupdf
import pdfplumber
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Poppler path for Windows
POPPLER_PATH = r"C:\poppler\Library\bin"

# Thresholds
SMALL_PDF = 10      # pages
MEDIUM_PDF = 50     # pages
MIN_TEXT_PER_PAGE = 50  # characters — below this triggers OCR


def extract_pdf(filepath: str) -> dict:
    """
    Tiered extraction based on document size and quality.
    
    Tier 1: < 10 pages  → full extraction, all chunks
    Tier 2: 10-50 pages → section-aware extraction, weighted chunks  
    Tier 3: 50+ pages   → hierarchical extraction, summary-first
    
    OCR fallback: triggered per-page when text extraction fails
    """
    path = Path(filepath)
    result = {
        "filename": path.name,
        "filepath": str(path),
        "pages": [],
        "tables": [],
        "metadata": {},
        "tier": 1,
        "ocr_pages": [],
        "quality_score": 1.0
    }

    # Open with pymupdf for metadata
    doc = fitz.open(filepath)
    page_count = len(doc)

    result["metadata"] = {
        "title": doc.metadata.get("title", path.stem) or path.stem,
        "author": doc.metadata.get("author", "unknown"),
        "page_count": page_count,
        "subject": doc.metadata.get("subject", "")
    }

    # Determine tier
    if page_count < SMALL_PDF:
        result["tier"] = 1
        logger.info(f"  Tier 1 (small): {page_count} pages")
    elif page_count < MEDIUM_PDF:
        result["tier"] = 2
        logger.info(f"  Tier 2 (medium): {page_count} pages")
    else:
        result["tier"] = 3
        logger.info(f"  Tier 3 (large): {page_count} pages")

    # Extract text per page with OCR fallback
    ocr_count = 0
    total_text = 0

    for page_num, page in enumerate(doc):
        text = page.get_text("text").strip()

        # OCR fallback if page has too little text
        if len(text) < MIN_TEXT_PER_PAGE:
            ocr_text = _ocr_page(filepath, page_num)
            if ocr_text and len(ocr_text) > len(text):
                text = ocr_text
                ocr_count += 1
                result["ocr_pages"].append(page_num + 1)
                logger.info(f"  OCR used on page {page_num + 1}")

        if text:
            result["pages"].append({
                "page": page_num + 1,
                "text": text,
                "char_count": len(text),
                "section": _detect_section(text, page_num, page_count)
            })
            total_text += len(text)

    doc.close()

    # Extract tables via pdfplumber
    try:
        with pdfplumber.open(filepath) as pdf:
            for page_num, page in enumerate(pdf.pages):
                tables = page.extract_tables()
                for table in tables:
                    if table and len(table) > 1:
                        result["tables"].append({
                            "page": page_num + 1,
                            "data": table,
                            "text": _table_to_text(table)
                        })
    except Exception as e:
        logger.warning(f"  Table extraction failed: {e}")

    # Quality score
    if page_count > 0:
        text_pages = len(result["pages"])
        result["quality_score"] = round(text_pages / page_count, 2)

    if ocr_count > 0:
        logger.info(f"  OCR used on {ocr_count}/{page_count} pages")

    logger.info(f"  Quality score: {result['quality_score']} | {total_text} chars extracted")
    return result


def _ocr_page(filepath: str, page_num: int) -> str:
    """Run OCR on a single page using pytesseract."""
    try:
        import pytesseract
        from pdf2image import convert_from_path

        images = convert_from_path(
            filepath,
            first_page=page_num + 1,
            last_page=page_num + 1,
            poppler_path=POPPLER_PATH if os.path.exists(POPPLER_PATH) else None
        )
        if images:
            text = pytesseract.image_to_string(images[0], lang="eng+fra")
            return text.strip()
    except Exception as e:
        logger.warning(f"  OCR failed on page {page_num + 1}: {e}")
    return ""


def _detect_section(text: str, page_num: int, total_pages: int) -> str:
    """
    Detect document section based on content and position.
    Returns section type for weighted chunking.
    """
    text_lower = text.lower()[:500]  # check first 500 chars

    # Position-based heuristics
    position = page_num / max(total_pages, 1)

    if position < 0.1:
        if any(w in text_lower for w in ["abstract", "summary", "overview", "introduction"]):
            return "abstract"
        return "front_matter"

    if position > 0.85:
        if any(w in text_lower for w in ["reference", "bibliography", "appendix", "annex"]):
            return "back_matter"
        return "conclusion"

    # Content-based detection
    if any(w in text_lower for w in ["abstract", "résumé", "summary"]):
        return "abstract"
    if any(w in text_lower for w in ["introduction", "background", "context"]):
        return "introduction"
    if any(w in text_lower for w in ["method", "methodology", "approach", "procedure"]):
        return "methodology"
    if any(w in text_lower for w in ["result", "finding", "experiment", "evaluation"]):
        return "results"
    if any(w in text_lower for w in ["conclusion", "discussion", "future work"]):
        return "conclusion"
    if any(w in text_lower for w in ["reference", "bibliography", "works cited"]):
        return "references"

    return "body"


def _table_to_text(table: list) -> str:
    """Convert table to readable text for embedding."""
    rows = []
    for row in table:
        if row:
            clean = [str(cell).strip() if cell else "" for cell in row]
            rows.append(" | ".join(clean))
    return "\n".join(rows)


def chunk_text(pages: list[dict], tier: int = 1) -> list[dict]:
    """
    Tier-aware chunking strategy.
    
    Tier 1: 500 words, 50 overlap — full detail
    Tier 2: 800 words, 100 overlap — section-aware, skip back_matter
    Tier 3: 1200 words, 150 overlap — large chunks, skip references/back_matter
    """
    # Section weights — higher = more important
    SECTION_WEIGHTS = {
        "abstract": 1.0,
        "introduction": 0.9,
        "methodology": 0.95,
        "results": 1.0,
        "conclusion": 0.95,
        "body": 0.8,
        "front_matter": 0.6,
        "back_matter": 0.2,
        "references": 0.1
    }

    # Skip low-value sections for large docs
    SKIP_SECTIONS_TIER3 = {"references", "back_matter"}
    SKIP_SECTIONS_TIER2 = {"references"}

    if tier == 1:
        chunk_size = 500
        overlap = 50
        skip_sections = set()
    elif tier == 2:
        chunk_size = 800
        overlap = 100
        skip_sections = SKIP_SECTIONS_TIER2
    else:
        chunk_size = 1200
        overlap = 150
        skip_sections = SKIP_SECTIONS_TIER3

    chunks = []

    for page in pages:
        section = page.get("section", "body")
        weight = SECTION_WEIGHTS.get(section, 0.8)

        # Skip low-value sections for large docs
        if section in skip_sections:
            continue

        text = page["text"]
        words = text.split()

        for i in range(0, len(words), chunk_size - overlap):
            chunk_words = words[i:i + chunk_size]
            if len(chunk_words) < 20:
                continue

            chunks.append({
                "text": " ".join(chunk_words),
                "page": page["page"],
                "section": section,
                "weight": weight
            })

    logger.info(f"  Tier {tier} chunking: {len(chunks)} chunks from {len(pages)} pages")
    return chunks


def get_priority_chunks(chunks: list[dict], tier: int, max_chunks: int = None) -> list[dict]:
    """
    For large documents, prioritize high-value chunks.
    Sorts by section weight, limits total if needed.
    """
    if tier < 3:
        return chunks

    # Sort by weight descending
    sorted_chunks = sorted(chunks, key=lambda x: x.get("weight", 0.8), reverse=True)

    if max_chunks:
        return sorted_chunks[:max_chunks]

    return sorted_chunks