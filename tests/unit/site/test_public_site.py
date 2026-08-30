from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

SITE_ROOT = Path(__file__).parents[3] / "site"
PRODUCTION_ORIGIN = "https://evaatyourservice.com"


@dataclass
class ParsedPage:
    title: str = ""
    descriptions: list[str] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)
    headings: list[tuple[int, str]] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.page = ParsedPage()
        self._title_parts: list[str] | None = None
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        if tag == "title":
            self._title_parts = []
        elif tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
            self._heading_level = int(tag[1])
            self._heading_parts = []
        elif tag == "meta" and attributes.get("name") == "description":
            self.page.descriptions.append(attributes.get("content", ""))
        elif tag == "link" and attributes.get("rel") == "canonical":
            self.page.canonicals.append(attributes.get("href", ""))
        elif tag == "a":
            self.page.links.append(attributes.get("href", ""))
        elif tag in {"img", "script"} and attributes.get("src"):
            self.page.sources.append(attributes["src"] or "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._title_parts is not None:
            self.page.title = " ".join(self._title_parts)
            self._title_parts = None
        elif self._heading_level is not None and tag == f"h{self._heading_level}":
            self.page.headings.append((self._heading_level, " ".join(self._heading_parts)))
            self._heading_level = None
            self._heading_parts = []
        if tag in {"script", "style"}:
            self._ignored_depth -= 1

    def handle_data(self, data: str) -> None:
        normalized = " ".join(data.split())
        if not normalized or self._ignored_depth:
            return
        self.page.text.append(normalized)
        if self._title_parts is not None:
            self._title_parts.append(normalized)
        if self._heading_level is not None:
            self._heading_parts.append(normalized)


def parse_page(relative_path: str) -> ParsedPage:
    parser = PageParser()
    parser.feed((SITE_ROOT / relative_path).read_text(encoding="utf-8"))
    return parser.page


def resolve_internal_path(path: str) -> Path:
    relative = path.removeprefix("/")
    if not relative:
        return SITE_ROOT / "index.html"
    if path.endswith("/"):
        return SITE_ROOT / relative / "index.html"
    return SITE_ROOT / relative


def test_homepage_has_release_metadata_and_one_primary_heading() -> None:
    page = parse_page("index.html")

    assert page.title == "Eva — Proactive personal AI"
    assert page.descriptions == [
        "Eva is a proactive personal AI that notices what matters, connects it to your "
        "goals, and brings you a clear next step."
    ]
    assert page.canonicals == [f"{PRODUCTION_ORIGIN}/"]
    assert [text for level, text in page.headings if level == 1] == [
        "The personal AI that notices what matters."
    ]


def test_homepage_is_truthful_about_current_and_planned_capabilities() -> None:
    text = " ".join(parse_page("index.html").text).lower()

    assert "private beta" in text
    assert "read-only gmail" in text
    assert "available today" in text
    assert "being built" in text
    assert "telegram" in text
    assert "asks before consequential actions" in text


def test_homepage_uses_only_local_assets_and_no_tracking() -> None:
    page = parse_page("index.html")
    source = (SITE_ROOT / "index.html").read_text(encoding="utf-8").lower()

    assert page.sources == ["/assets/site.js", "/assets/mark.svg"]
    for forbidden in (
        "google-analytics",
        "gtag(",
        "segment.com",
        "facebook.net",
        "http://",
    ):
        assert forbidden not in source


def test_homepage_references_committed_shared_assets() -> None:
    for path in (
        "assets/styles.css",
        "assets/site.js",
        "assets/mark.svg",
        "assets/favicon.svg",
    ):
        assert (SITE_ROOT / path).is_file(), path
