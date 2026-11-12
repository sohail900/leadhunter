import re
from html import unescape
from html.parser import HTMLParser

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")

_BLOCK_TAGS = {"p", "div", "br", "ul", "ol", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre"}
_SKIP_TAGS = {"script", "style"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return clean_whitespace(unescape("".join(parser.parts)))


def clean_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def find_email(text: str) -> str | None:
    match = EMAIL_RE.search(text)
    if match:
        return match.group(0).rstrip(".")
    spelled = re.search(
        r"\b([A-Za-z0-9._%+-]+)\s*[\[(]?\s*at\s*[\])]?\s*([A-Za-z0-9-]+)"
        r"\s*[\[(]?\s*dot\s*[\])]?\s*([A-Za-z]{2,})\b",
        text,
    )
    if spelled and re.search(r"[\[(]\s*(at|dot)\s*[\])]", text, re.IGNORECASE):
        return f"{spelled.group(1)}@{spelled.group(2)}.{spelled.group(3)}"
    return None


def first_line(text: str, limit: int = 150) -> str:
    for line in text.split("\n"):
        line = line.strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"
    return ""
