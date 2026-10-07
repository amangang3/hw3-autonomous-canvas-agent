from __future__ import annotations

import html
import re
import unicodedata
from html.parser import HTMLParser

TOKEN_PATTERN = re.compile(r"\b\d+~[A-Za-z0-9]{20,}\b")
URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.|//[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?1[ .-]?)?(?:\(?\d{3}\)?[ .-]?)\d{3}[ .-]?\d{4}(?!\d)")
DISCLOSURE_PATTERN = re.compile(r"(?i)\b(?:password|api\s*key|token|grade)\b")


class _TextExtractor(HTMLParser):
    block_tags = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.block_tags:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def strip_html(value: str) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    parser.close()
    return normalize_text("".join(parser.parts))


def first_nonempty_line(html_message: str) -> str:
    text = strip_html(html_message)
    return text.split("\n", 1)[0] if text else ""


def filter_message(message: str) -> tuple[bool, str | None]:
    normalized = normalize_text(message)
    if len(normalized) < 40:
        return False, "length"
    if len(normalized) > 2000:
        return False, "length"
    checks = (
        (URL_PATTERN, "url"),
        (EMAIL_PATTERN, "email"),
        (PHONE_PATTERN, "phone"),
        (TOKEN_PATTERN, "token_like"),
        (DISCLOSURE_PATTERN, "disclosure"),
    )
    for pattern, reason in checks:
        if pattern.search(normalized):
            return False, reason
    return True, None


def html_message(message: str) -> str:
    normalized = normalize_text(message)
    return "".join(f"<p>{html.escape(line, quote=True)}</p>" for line in normalized.split("\n"))


def redact(value: str, token: str | None = None) -> str:
    result = str(value)
    if token:
        result = result.replace(token, "[REDACTED]")
    result = TOKEN_PATTERN.sub("[REDACTED]", result)
    result = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", result)
    return result
