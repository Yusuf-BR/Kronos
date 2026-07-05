import fitz  # pymupdf
import pdfplumber
import os
from pathlib import Path

def extract_pdf(filepath: str) -> dict:
    """
    Extract structured content from a PDF.
    Returns text by page, tables, and metadata.
    """
    path = Path(filepath)
    result = {
        "filename": path.name,
        "filepath": str(path),
        "pages": [],
        "tables": [],
        "metadata": {}
    }

    # Metadata via pymupdf
    doc = fitz.open(filepath)
    result["metadata"] = {
        "title": doc.metadata.get("title", path.stem),
        "author": doc.metadata.get("author", "unknown"),
        "page_count": len(doc),
        "subject": doc.metadata.get("subject", "")
    }

    # Text per page via pymupdf
    for page_num, page in enumerate(doc):
        text = page.get_text("text").strip()
        if text:
            result["pages"].append({
                "page": page_num + 1,
                "text": text
            })
    doc.close()

    # Tables via pdfplumber
    with pdfplumber.open(filepath) as pdf:
        for page_num, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            for table in tables:
                if table:
                    result["tables"].append({
                        "page": page_num + 1,
                        "data": table
                    })

    return result


def chunk_text(pages: list[dict], chunk_size: int = 500,
               overlap: int = 50) -> list[dict]:
    """
    Split page text into overlapping chunks for Qdrant.
    """
    chunks = []
    for page in pages:
        text = page["text"]
        words = text.split()
        for i in range(0, len(words), chunk_size - overlap):
            chunk_words = words[i:i + chunk_size]
            if len(chunk_words) < 20:  # skip tiny chunks
                continue
            chunks.append({
                "text": " ".join(chunk_words),
                "page": page["page"]
            })
    return chunks