# Eva Public Site Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and deploy a polished, truthful, tracker-free public website for Eva at `evaatyourservice.com`, including stable privacy-policy and terms-of-service pages for Google OAuth production publishing.

**Architecture:** A dependency-free static site lives under `site/` and is deployed by a least-privilege GitHub Pages workflow. Standard-library Python tests treat the site as a release artifact and verify page structure, disclosures, internal links, metadata, and deployment configuration without adding a web framework.

**Tech Stack:** HTML5, CSS, local SVG, minimal progressive-enhancement JavaScript, Python 3.14 standard library, pytest, GitHub Pages, GitHub Actions

**Spec:** `docs/superpowers/specs/2026-08-30-eva-public-site-design.md`

## Global Constraints

- Keep all public-site files under `site/`; do not couple them to FastAPI or the Python package.
- Do not load analytics, cookies, trackers, external fonts, third-party scripts, or insecure HTTP assets.
- Essential content and navigation must work without JavaScript.
- Describe Eva as an in-development private beta and distinguish implemented capabilities from the roadmap.
- Describe Gmail access as read-only and do not claim that Eva can send, delete, or modify email.
- Do not expose the owner's personal email address; use the public GitHub repository as the contact route.
- Avoid unsupported security, retention, availability, or professional-advice guarantees.
- Preserve the repository's pinned-action convention and grant the Pages workflow only the permissions it needs.
- Add concise comments only where an accessibility, security, or deployment choice is not self-evident.
- Use TDD for every task and commit each independently testable deliverable.

## File Map

### New files

- `site/index.html` — public product landing page and canonical navigation.
- `site/privacy/index.html` — current private-beta privacy policy and Google Limited Use disclosure.
- `site/terms/index.html` — current private-beta terms of service.
- `site/404.html` — custom not-found recovery page.
- `site/assets/styles.css` — shared responsive, accessible visual system.
- `site/assets/site.js` — optional progressive reveal enhancement with reduced-motion protection.
- `site/assets/mark.svg` — decorative Eva identity mark used in page chrome.
- `site/assets/favicon.svg` — compact Eva browser icon.
- `site/CNAME` — GitHub Pages custom apex domain.
- `site/robots.txt` — crawler policy and sitemap pointer.
- `site/sitemap.xml` — canonical production page inventory.
- `.github/workflows/pages.yml` — static Pages deployment.
- `tests/unit/site/__init__.py` — site test package marker.
- `tests/unit/site/test_public_site.py` — release-contract tests for all site files.

### Modified files

- `README.md` — local preview, verification, deployment, and post-merge domain instructions.

---

### Task 1: Site Contract, Shared Visual System, and Homepage

**Files:**
- Create: `tests/unit/site/__init__.py`
- Create: `tests/unit/site/test_public_site.py`
- Create: `site/index.html`
- Create: `site/assets/styles.css`
- Create: `site/assets/site.js`
- Create: `site/assets/mark.svg`
- Create: `site/assets/favicon.svg`

**Interfaces:**
- Consumes: the copy and visual requirements in the approved spec.
- Produces: `SITE_ROOT`, `parse_page(relative_path: str) -> ParsedPage`, `resolve_internal_path(href: str) -> Path`, shared navigation/footer markup, and the CSS classes reused by policy and error pages.

- [ ] **Step 1: Write failing homepage and asset contract tests**

Create a standard-library parser that records titles, descriptions, canonical URLs, headings, links, script sources, image sources, and inline text. Add tests equivalent to:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

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
    # Track the active text-bearing element so assertions inspect semantic content,
    # rather than relying on brittle regular expressions over formatted HTML.
    def __init__(self) -> None:
        super().__init__()
        self.page = ParsedPage()
        self._title_parts: list[str] | None = None
        self._heading_level: int | None = None
        self._heading_parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
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
            self.page.headings.append(
                (self._heading_level, " ".join(self._heading_parts))
            )
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
        "Eva is a proactive personal AI that notices what matters, connects it to your goals, and brings you a clear next step."
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
    assert page.sources == ["/assets/site.js", "/assets/mark.svg"]
    source = (SITE_ROOT / "index.html").read_text(encoding="utf-8").lower()
    for forbidden in ("google-analytics", "gtag(", "segment.com", "facebook.net", "http://"):
        assert forbidden not in source
```

Implement `PageParser` fully: capture one `<title>`, `meta[name=description]`, `link[rel=canonical]`, `h1` through `h6`, anchor `href`, script/image `src`, and normalized visible text while ignoring style/script contents.

- [ ] **Step 2: Run the new tests and confirm the site contract fails**

Run: `uv run pytest tests/unit/site/test_public_site.py -v`

Expected: FAIL because `site/index.html` and the shared assets do not exist.

- [ ] **Step 3: Implement the homepage and shared assets**

Create semantic HTML with:

```html
<a class="skip-link" href="#main-content">Skip to content</a>
<header class="site-header">
  <a class="brand" href="/" aria-label="Eva home">
    <img src="/assets/mark.svg" alt="" width="32" height="32">
    <span>Eva</span>
  </a>
  <nav aria-label="Primary navigation">
    <a aria-current="page" href="/">Home</a>
    <a href="/privacy/">Privacy</a>
    <a href="/terms/">Terms</a>
    <a href="https://github.com/BugsBunnyWanders/EvaAI">Source</a>
  </nav>
</header>
```

Use the six approved homepage sections and exact truth boundaries: “Available today” contains the durable event backbone and read-only Gmail ingestion; “Being built in phases” contains Telegram conversations, contextual reasoning, and approved actions. Include `meta` description, canonical URL, Open Graph fields, theme color, local favicon, stylesheet, and deferred local script.

Create CSS custom properties for the dark ink background, warm white text, violet/coral accents, focus rings, type scale, spacing, cards, status chips, responsive grids, and policy typography. Add `@media (prefers-reduced-motion: reduce)` to disable smooth scrolling, transitions, and reveal animations.

Create JavaScript that adds an enhancement class and uses `IntersectionObserver` only when reduced motion is not requested. Content begins visible; the script may enhance entrance presentation but never hide content permanently.

Create local SVG mark and favicon files with `role="img"`/accessible title only where standalone, no embedded script, and no remote references.

- [ ] **Step 4: Run the homepage contract and full formatting checks**

Run: `uv run pytest tests/unit/site/test_public_site.py -v && uv run ruff format --check tests/unit/site && uv run ruff check tests/unit/site`

Expected: all site tests pass and Ruff reports no issues.

- [ ] **Step 5: Commit the homepage foundation**

```bash
git add site/index.html site/assets tests/unit/site
git commit -m "feat: add Eva public homepage"
```

---

### Task 2: Privacy, Terms, Metadata, and Error Recovery

**Files:**
- Modify: `tests/unit/site/test_public_site.py`
- Create: `site/privacy/index.html`
- Create: `site/terms/index.html`
- Create: `site/404.html`
- Create: `site/CNAME`
- Create: `site/robots.txt`
- Create: `site/sitemap.xml`

**Interfaces:**
- Consumes: `parse_page`, `resolve_internal_path`, navigation/footer structure, and shared CSS from Task 1.
- Produces: final OAuth policy URLs, complete internal route graph, production-domain metadata, and custom-domain artifacts consumed by the Pages deployment.

- [ ] **Step 1: Extend the contract with failing policy and route tests**

Add parameterized tests with exact release expectations:

```python
import pytest


@pytest.mark.parametrize(
    ("path", "title", "canonical", "heading"),
    [
        ("privacy/index.html", "Privacy — Eva", f"{PRODUCTION_ORIGIN}/privacy/", "Privacy policy"),
        ("terms/index.html", "Terms — Eva", f"{PRODUCTION_ORIGIN}/terms/", "Terms of service"),
        ("404.html", "Page not found — Eva", f"{PRODUCTION_ORIGIN}/404.html", "This page drifted out of context."),
    ],
)
def test_secondary_pages_have_release_metadata(
    path: str, title: str, canonical: str, heading: str
) -> None:
    page = parse_page(path)
    assert page.title == title
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


def test_domain_metadata_uses_only_production_origin() -> None:
    assert (SITE_ROOT / "CNAME").read_text(encoding="utf-8") == "evaatyourservice.com\n"
    assert f"Sitemap: {PRODUCTION_ORIGIN}/sitemap.xml" in (
        SITE_ROOT / "robots.txt"
    ).read_text(encoding="utf-8")
    sitemap = (SITE_ROOT / "sitemap.xml").read_text(encoding="utf-8")
    assert sitemap.count("<url>") == 3
    for route in ("/", "/privacy/", "/terms/"):
        assert f"<loc>{PRODUCTION_ORIGIN}{route}</loc>" in sitemap
```

- [ ] **Step 2: Run the extended tests and confirm missing policy artifacts fail**

Run: `uv run pytest tests/unit/site/test_public_site.py -v`

Expected: FAIL for the missing policy pages, 404 page, `CNAME`, `robots.txt`, and `sitemap.xml`.

- [ ] **Step 3: Implement complete policy pages and metadata artifacts**

Create privacy sections for scope, collected data, use, Gmail permissions, credentials/security, sharing, retention/deletion, Google Limited Use, international processing, children, changes, and contact. Link “Google API Services User Data Policy” to `https://developers.google.com/terms/api-services-user-data-policy` and the contact route to the repository.

Create terms sections for eligibility/authorization, beta status, user responsibility, professional-advice exclusion, acceptable use, integrations, intellectual property, third-party services, availability, warranty, liability, changes, and contact. Use “to the extent permitted by applicable law” rather than selecting an unconfirmed jurisdiction.

Create a focused 404 page with links to `/`, `/privacy/`, and `/terms/`. Reuse the header/footer and shared assets on every page, use `aria-current="page"` appropriately, and show “Effective 30 August 2026” on both policy pages.

Create exact domain artifacts:

```text
# site/CNAME
evaatyourservice.com

# site/robots.txt
User-agent: *
Allow: /

Sitemap: https://evaatyourservice.com/sitemap.xml
```

Create an XML sitemap with exactly the canonical homepage, privacy, and terms URLs.

- [ ] **Step 4: Run site tests and inspect the complete route contract**

Run: `uv run pytest tests/unit/site/test_public_site.py -v && git diff --check`

Expected: all site tests pass and the diff has no whitespace errors.

- [ ] **Step 5: Commit the policy and metadata release**

```bash
git add site tests/unit/site/test_public_site.py
git commit -m "feat: add Eva privacy and terms pages"
```

---

### Task 3: GitHub Pages Deployment, Documentation, and Release Verification

**Files:**
- Create: `.github/workflows/pages.yml`
- Modify: `README.md`

**Interfaces:**
- Consumes: the complete `site/` artifact from Tasks 1 and 2.
- Produces: repeatable deployment from `main`, local preview instructions, and the operator handoff for Hostinger DNS and Google OAuth production publishing.

- [ ] **Step 1: Implement the pinned least-privilege Pages workflow**

This task contains GitHub Actions configuration and human operator documentation rather than application behavior. Do not add source-grep tests for either artifact; GitHub validates workflow semantics on the pushed branch, while local review checks the permission and artifact boundaries.

Create:

```yaml
name: Deploy public site

on:
  push:
    branches: [main]
    paths:
      - "site/**"
      - ".github/workflows/pages.yml"
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: pages
  cancel-in-progress: false

jobs:
  deploy:
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    permissions:
      pages: write
      id-token: write
    runs-on: ubuntu-latest
    steps:
      - name: Check out repository
        uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - name: Configure Pages
        uses: actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b # v5
      - name: Upload static site
        uses: actions/upload-pages-artifact@7b1f4a764d45c48632c6b24a0339c27f5614fb0b # v4
        with:
          path: site
      - name: Deploy to GitHub Pages
        id: deployment
        uses: actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e # v4
```

- [ ] **Step 2: Document preview and exact operator handoff**

Add a “Public website” README section with:

```bash
python -m http.server 8080 --directory site
```

Document preview at `http://127.0.0.1:8080`, deployment through GitHub Actions, Pages source selection, custom-domain setup, the four official GitHub Pages apex `A` records, the `www` CNAME to `bugsbunnywanders.github.io`, preserving Hostinger MX/TXT records, HTTPS enablement, Search Console verification, Google Auth Platform URLs, production publishing, and Gmail connector reauthorization.

- [ ] **Step 3: Run focused and full automated verification**

Run: `uv run pytest tests/unit/site/test_public_site.py -v && make verify && git diff --check`

Expected: all site tests and all existing repository checks pass with no diff whitespace errors.

- [ ] **Step 4: Perform local visual and behavior verification**

Serve `site/` on a local ephemeral port. Inspect `/`, `/privacy/`, `/terms/`, and `/404.html` at 390×844 and 1440×1000 viewports. Verify no horizontal overflow, readable policy measure, working navigation, visible focus states, truthful status labels, graceful no-JavaScript content, and reduced-motion behavior. Fix any defects and rerun focused tests.

- [ ] **Step 5: Commit deployment and documentation**

```bash
git add .github/workflows/pages.yml README.md
git commit -m "ci: deploy Eva public site"
```

- [ ] **Step 6: Final branch review and PR handoff**

Review `git diff main...HEAD`, run `make verify` once more on the exact branch tip, push `codex/eva-public-site`, and open a PR against `main` titled `Add Eva public website`. Include the three production URLs, verification evidence, Pages/DNS operator steps, and an explicit note that merging does not itself change Hostinger DNS or publish the Google OAuth app.
