"""General helper utilities."""

import re
import urllib.parse
from typing import Optional


def validate_url(url: str) -> Optional[str]:
    """Validate and normalize a URL. Returns None if invalid."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc and "." in parsed.netloc:
        return url
    return None


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP/HTTPS URLs from text."""
    pattern = r"https?://[^\s<>\"']+|www\.[^\s<>\"']+"
    found = re.findall(pattern, text)
    return [validate_url(u) for u in found if validate_url(u)]


def normalize_text(text: str, max_len: int = 10_000) -> str:
    """Clean whitespace and truncate text for LLM consumption."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "\n\n[...truncated]"
    return text


def chunk_text(text: str, chunk_size: int = 2000, overlap: int = 200) -> list[str]:
    """Split long text into overlapping chunks for embedding."""
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = start + chunk_size
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        start += chunk_size - overlap
        if start >= len(words):
            break
    return chunks
