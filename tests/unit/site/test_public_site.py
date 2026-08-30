from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import pytest

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
            self.page.descriptions.append(attributes.get("content") or "")
        elif tag == "link" and attributes.get("rel") == "canonical":
            self.page.canonicals.append(attributes.get("href") or "")
        elif tag == "a":
            self.page.links.append(attributes.get("href") or "")
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


@pytest.mark.parametrize(
    ("path", "title", "canonical", "heading"),
    [
        (
            "privacy/index.html",
            "Privacy — Eva",
            f"{PRODUCTION_ORIGIN}/privacy/",
            "Privacy policy",
        ),
        (
            "terms/index.html",
            "Terms — Eva",
            f"{PRODUCTION_ORIGIN}/terms/",
            "Terms of service",
        ),
        (
            "404.html",
            "Page not found — Eva",
            f"{PRODUCTION_ORIGIN}/404.html",
            "This page drifted out of context.",
        ),
    ],
)
def test_secondary_pages_have_release_metadata(
    path: str,
    title: str,
    canonical: str,
    heading: str,
) -> None:
    page = parse_page(path)

    assert page.title == title
    assert page.descriptions
    assert page.canonicals == [canonical]
    assert [text for level, text in page.headings if level == 1] == [heading]


def test_privacy_policy_discloses_actual_google_data_handling() -> None:
    text = " ".join(parse_page("privacy/index.html").text).lower()

    for phrase in (
        "effective 30 august 2026",
        "read-only gmail",
        "message metadata",
        "message content",
        "attachment metadata",
        "oauth",
        "managed secret infrastructure",
        "retention",
        "deletion",
        "google api services user data policy",
        "limited use",
        "do not sell",
    ):
        assert phrase in text


def test_terms_set_private_beta_and_safety_boundaries() -> None:
    text = " ".join(parse_page("terms/index.html").text).lower()

    for phrase in (
        "effective 30 august 2026",
        "experimental private beta",
        "review eva's suggestions",
        "not legal, medical, financial, or emergency advice",
        "prohibited",
        "third-party services",
        "no guarantee",
        "applicable law",
    ):
        assert phrase in text


def test_every_internal_link_resolves_to_a_committed_page_or_asset() -> None:
    for html_path in SITE_ROOT.rglob("*.html"):
        page = parse_page(str(html_path.relative_to(SITE_ROOT)))
        for href in page.links:
            parsed = urlparse(href)
            if parsed.scheme or parsed.netloc or href.startswith(("#", "mailto:")):
                continue
            assert resolve_internal_path(parsed.path).exists(), (html_path, href)


@pytest.mark.parametrize(
    "path",
    ["index.html", "privacy/index.html", "terms/index.html", "404.html"],
)
def test_every_page_keeps_navigation_and_assets_local(path: str) -> None:
    page = parse_page(path)
    source = (SITE_ROOT / path).read_text(encoding="utf-8").lower()

    assert "/" in page.links
    assert "/privacy/" in page.links
    assert "/terms/" in page.links
    assert "https://github.com/BugsBunnyWanders/EvaAI" in page.links
    for forbidden in (
        "google-analytics",
        "gtag(",
        "segment.com",
        "facebook.net",
        "http://",
    ):
        assert forbidden not in source


def test_domain_metadata_uses_only_production_origin() -> None:
    assert (SITE_ROOT / "CNAME").read_text(encoding="utf-8") == ("evaatyourservice.com\n")
    assert f"Sitemap: {PRODUCTION_ORIGIN}/sitemap.xml" in (SITE_ROOT / "robots.txt").read_text(
        encoding="utf-8"
    )
    sitemap = (SITE_ROOT / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.count("<url>") == 3
    for route in ("/", "/privacy/", "/terms/"):
        assert f"<loc>{PRODUCTION_ORIGIN}{route}</loc>" in sitemap
