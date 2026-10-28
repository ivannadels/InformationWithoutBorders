"""
Structure-aware HTML -> text extraction for saved web pages.

Saved web pages contain much more than the document: cookie scripts, menus,
breadcrumbs, language pickers, "related articles", share buttons, footers...
Indexing all that would make every page match words like "home", "donate"
or "English". This module:

  1. builds a small tree of the page (standard library only),
  2. reads metadata from <head> (title, URL, language, description, dates),
  3. keeps only the main content area (<main>, else <body>),
  4. removes boilerplate blocks by tag, ARIA role and class/id names,
  5. writes the rest as light Markdown, keeping what helps search:
       # Headings (with their level)      -> section names for each passage
       - list items
       table rows as "cell | cell | cell"  (header row first)

The output is plain text, so the rest of the engine does not care that it
came from HTML.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Union
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 1. A tolerant mini DOM
# ---------------------------------------------------------------------------

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
             "meta", "param", "source", "track", "wbr"}


@dataclass
class Node:
    tag: str
    attrs: Dict[str, str] = field(default_factory=dict)
    children: List[Union["Node", str]] = field(default_factory=list)

    def find_all(self, tag: str) -> List["Node"]:
        """All descendants with this tag, in document order (matters for table rows)."""
        found: List[Node] = []
        stack: List[Node] = [self]
        while stack:
            node = stack.pop()
            if node is not self and node.tag == tag:
                found.append(node)
            stack.extend(reversed([c for c in node.children if isinstance(c, Node)]))
        return found

    def find(self, tag: str) -> Optional["Node"]:
        # breadth-first so the outermost match wins
        queue = [self]
        while queue:
            node = queue.pop(0)
            for child in node.children:
                if isinstance(child, Node):
                    if child.tag == tag:
                        return child
                    queue.append(child)
        return None

    def text(self) -> str:
        parts: List[str] = []
        stack: List[Union[Node, str]] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in ("script", "style", "noscript", "svg", "template"):
                stack.extend(reversed(item.children))
        return " ".join(" ".join(parts).split())


class _TreeBuilder(HTMLParser):
    # Tags whose open element is implicitly closed by a new one of these.
    _IMPLICIT_CLOSE = {
        "p": ({"p"}, {"div", "section", "article", "main", "body", "td", "li", "blockquote"}),
        "li": ({"li"}, {"ul", "ol"}),
        "dt": ({"dt", "dd"}, {"dl"}),
        "dd": ({"dt", "dd"}, {"dl"}),
        "tr": ({"tr", "td", "th"}, {"table", "tbody", "thead", "tfoot"}),
        "td": ({"td", "th"}, {"tr", "table"}),
        "th": ({"td", "th"}, {"tr", "table"}),
        "option": ({"option"}, {"select", "datalist"}),
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.stack: List[Node] = [self.root]

    def handle_starttag(self, tag, attrs):
        attrs_dict = {k: (v or "") for k, v in attrs}
        rule = self._IMPLICIT_CLOSE.get(tag)
        if rule:
            closes, stops = rule
            for i in range(len(self.stack) - 1, 0, -1):
                open_tag = self.stack[i].tag
                if open_tag in stops:
                    break
                if open_tag in closes:
                    del self.stack[i:]
                    break
        node = Node(tag, attrs_dict)
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # stray end tag: ignore

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def parse_html(raw: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(raw)
    builder.close()
    return builder.root


# ---------------------------------------------------------------------------
# 2. Boilerplate detection
# ---------------------------------------------------------------------------

DROP_TAGS = {"script", "style", "noscript", "svg", "iframe", "nav", "aside", "footer", "form",
             "button", "select", "textarea", "template", "canvas", "video", "audio", "object",
             "embed", "input", "label", "img", "picture", "head", "dialog", "menu"}

DROP_ROLES = {"navigation", "search", "banner", "contentinfo", "complementary", "dialog",
              "menu", "menubar", "toolbar", "alert", "tablist"}

# Matched against each class / id token, on word boundaries made of - or _.
# e.g. "block-system-breadcrumb-block" matches "breadcrumb".
_BOILERPLATE_WORDS = (
    "breadcrumbs?", "share", "sharing", "social", "related", "latest-content", "newsletter",
    "subscribe", "subscription", "cookies?", "consent", "visually-hidden", "sr-only",
    "element-invisible", "screen-reader", "skip-link", "flag", "pager", "book-nav", "book-navigation",
    "switcher", "languages?", "langague", "language-popup", "popup", "modal", "donate", "donation",
    "table-of-contents", "toc", "nav-section", "nav-in-page", "menu", "toolbar", "print",
    "read-more", "contact", "sidebar", "advert", "promo", "feedback", "hotjar", "antibot",
)
_BOILERPLATE_RE = re.compile(r"(?:^|[-_])(?:" + "|".join(_BOILERPLATE_WORDS) + r")(?:$|[-_])", re.I)
_BREADCRUMB_RE = re.compile(r"breadcrumb", re.I)
_REACT_STREAM_ID = re.compile(r"^[SB]:\d+$")


def _class_tokens(node: Node) -> List[str]:
    return (node.attrs.get("class", "") + " " + node.attrs.get("id", "")).split()


def is_boilerplate(node: Node) -> bool:
    if node.tag in DROP_TAGS:
        return True
    if "hidden" in node.attrs and not _REACT_STREAM_ID.match(node.attrs.get("id", "")):
        # React/Next.js sites send the article inside <div hidden id="S:0">
        # and reveal it with JavaScript, so those blocks are real content.
        return True
    if node.attrs.get("aria-hidden") == "true":
        return True
    if node.attrs.get("role", "").lower() in DROP_ROLES:
        return True
    return any(_BOILERPLATE_RE.search(tok) for tok in _class_tokens(node))


# ---------------------------------------------------------------------------
# 3. Rendering to light Markdown
# ---------------------------------------------------------------------------

BLOCK_TAGS = {"p", "div", "section", "article", "main", "header", "blockquote", "figure",
              "figcaption", "dl", "dt", "dd", "ul", "ol", "pre", "address", "details",
              "summary", "body", "center", "hgroup"}
HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


class _Renderer:
    def __init__(self) -> None:
        self.out: List[str] = []

    def block_break(self) -> None:
        self.out.append("\n\n")

    def render(self, node: Node) -> None:
        for child in node.children:
            if isinstance(child, str):
                self.out.append(child)
                continue
            if is_boilerplate(child):
                continue
            tag = child.tag
            if tag in HEADING_TAGS:
                title = _clean_inline(_visible_text(child))
                if title:
                    self.out.append(f"\n\n{'#' * HEADING_TAGS[tag]} {title}\n\n")
            elif tag == "table":
                self.render_table(child)
            elif tag == "li":
                self.out.append("\n- ")
                self.render(child)
                self.out.append("\n")
            elif tag == "br":
                self.out.append("\n")
            elif tag == "hr":
                self.block_break()
            elif tag in BLOCK_TAGS:
                self.block_break()
                self.render(child)
                self.block_break()
            elif tag in ("a", "span", "td", "th", "time", "small", "label", "cite"):
                # Often styled as separate tags/badges: keep words apart
                # ("vaccination" + "outbreaks", not "vaccinationoutbreaks").
                self.out.append(" ")
                self.render(child)
                self.out.append(" ")
            else:  # other inline tags: strong, em, b, i, sup, sub, ...
                self.render(child)

    def render_table(self, table: Node) -> None:
        rows = []
        for tr in table.find_all("tr"):
            cells = [c for c in tr.children if isinstance(c, Node) and c.tag in ("td", "th")]
            texts = [_clean_inline(_visible_text(c)) for c in cells]
            if any(texts):
                rows.append(texts)
        if not rows:
            return
        if max(len(r) for r in rows) <= 1:
            # A one-column "layout" table: render its contents normally.
            self.block_break()
            self.render(table)
            self.block_break()
            return
        self.block_break()
        self.out.append("\n".join(" | ".join(t for t in r) for r in rows))
        self.block_break()


def _visible_text(node: Node) -> str:
    parts: List[str] = []

    def walk(n: Node) -> None:
        for c in n.children:
            if isinstance(c, str):
                parts.append(c)
            elif not is_boilerplate(c):
                if c.tag == "br":
                    parts.append(" ")
                walk(c)
                if c.tag in BLOCK_TAGS or c.tag in ("td", "th", "li", "a", "span"):
                    parts.append(" ")
    walk(node)
    return "".join(parts)


def _clean_inline(text: str) -> str:
    return " ".join(text.split())


# Interface text that is never part of a document, in the languages seen so far.
_JUNK_LINE_RE = re.compile(
    r"^(permalink|on this page|sommaire|en esta página|sur cette page|read more|lire la suite|"
    r"download( pdf)?|télécharger|share|partager|print|imprimer|back to top|show more|see more|"
    r"\|+|\d+\s*min|min|save|cite|export all citations|open publication|"
    r"(photo )?credit:.*|©.*|copyright ©.*|v\d+(\.\w+)+)$", re.I)


def _tidy(raw_text: str) -> str:
    """Collapse whitespace, drop empty lines, drop repeated paragraphs."""
    blocks: List[str] = []
    seen = set()
    for block in re.split(r"\n\s*\n", raw_text):
        lines = [_clean_inline(line) for line in block.split("\n")]
        lines = [line for line in lines if line and line != "-" and not _JUNK_LINE_RE.match(line.lstrip("#- ").strip())]
        if not lines:
            continue
        text = "\n".join(lines)
        is_table = all(" | " in line for line in lines)
        key = text.lower()
        # Identical long paragraphs repeated on a page (desktop + mobile copies) are kept once.
        if not is_table and len(text.split()) >= 8:
            if key in seen:
                continue
            seen.add(key)
        blocks.append(text)
    return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# 4. Public function
# ---------------------------------------------------------------------------

_TITLE_SUFFIX_RE = re.compile(r"\s*[|\-–—]\s*(MSF|Médecins Sans Frontières|MSF Access|MSF Science Portal|"
                              r"Epicentre|MSF Medical Guidelines)[^|]*$", re.I)
_LAST_UPDATED_RE = re.compile(r"(?:last update[d]?|dernière mise à jour|última actualización)[ \t]*[:\-–]?[ \t]*"
                              r"([^\n*]{0,30}?\d{4})", re.I)


@dataclass
class ExtractedPage:
    title: str
    text: str
    metadata: Dict[str, str]
    breadcrumb: List[str]


def _json_ld_dates(root: Node) -> Dict[str, str]:
    """Read datePublished / dateModified from <script type="application/ld+json"> blocks."""
    found: Dict[str, str] = {}

    def visit(obj) -> None:
        if isinstance(obj, dict):
            for key in ("datePublished", "dateModified"):
                if isinstance(obj.get(key), str) and key not in found:
                    found[key] = obj[key]
            for value in obj.values():
                visit(value)
        elif isinstance(obj, list):
            for value in obj:
                visit(value)

    for script in root.find_all("script"):
        if "ld+json" in script.attrs.get("type", ""):
            raw = "".join(c for c in script.children if isinstance(c, str))
            try:
                visit(json.loads(raw))
            except ValueError:
                continue
    return found


def _head_metadata(root: Node) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    head = root.find("head") or root
    # <head> first; then the whole page, because some React sites put their
    # <title> and <meta> tags inside the body.
    for scope in (head, root):
        for m in scope.find_all("meta"):
            key = (m.attrs.get("property") or m.attrs.get("name") or m.attrs.get("itemprop") or "").lower()
            value = " ".join((m.attrs.get("content") or "").split())
            if key and value and key not in meta:
                meta[key] = value
        for link in scope.find_all("link"):
            if link.attrs.get("rel", "").lower() == "canonical" and link.attrs.get("href") and "canonical" not in meta:
                meta["canonical"] = link.attrs["href"]
        title_node = scope.find("title")
        if title_node and "html_title" not in meta and title_node.text():
            meta["html_title"] = title_node.text()
    for key, value in _json_ld_dates(root).items():
        meta.setdefault(key.lower(), value)
    html = root.find("html")
    if html and html.attrs.get("lang"):
        meta["lang"] = html.attrs["lang"]
    return meta


def _guess_language(text: str) -> str:
    """Very small fallback used only when the page does not declare a language."""
    sample = text[:5000]
    letters = [c for c in sample if c.isalpha()]
    if letters and sum(1 for c in letters if "؀" <= c <= "ۿ") / len(letters) > 0.3:
        return "ar"
    words = re.findall(r"[a-zà-ÿ]+", sample.lower())
    scores = {
        "en": sum(w in {"the", "and", "of", "to", "in", "is", "with", "for"} for w in words),
        "fr": sum(w in {"le", "la", "les", "et", "des", "du", "est", "dans", "pour"} for w in words),
        "es": sum(w in {"el", "los", "las", "y", "del", "es", "en", "para", "con"} for w in words),
    }
    best = max(scores, key=scores.get)
    return best if scores[best] >= 5 else ""


def extract_html_page(raw: str) -> ExtractedPage:
    root = parse_html(raw)
    head = _head_metadata(root)

    body = root.find("body") or root
    # Use <main> when it holds the document. Some sites render the article
    # outside <main> (which then only contains a loading spinner): fall back
    # to the whole <body>, still with boilerplate removed.
    main = body.find("main")
    content = main if main is not None and len(_visible_text(main).split()) >= 30 else body

    # Breadcrumbs are removed from the text but kept: they tell us which
    # guideline/book a page belongs to (e.g. "Clinical guidelines > Diarrhoea").
    breadcrumb: List[str] = []
    for node in [content] + content.find_all("div") + content.find_all("nav") + content.find_all("ol"):
        if any(_BREADCRUMB_RE.search(t) for t in _class_tokens(node)):
            items = [_clean_inline(li.text()) for li in node.find_all("li")]
            breadcrumb = [i for i in items if i and i.lower() not in ("home", "accueil", "inicio")]
            if breadcrumb:
                break

    renderer = _Renderer()
    renderer.render(content)
    text = _tidy("".join(renderer.out))

    first_h1 = next((line[2:].strip() for line in text.splitlines() if line.startswith("# ")), "")
    og_title = _TITLE_SUFFIX_RE.sub("", head.get("og:title", "")).strip()
    html_title = _TITLE_SUFFIX_RE.sub("", head.get("html_title", "")).strip()
    title = first_h1 or og_title.split(" | ")[0] or html_title.split(" | ")[0]

    metadata: Dict[str, str] = {}
    url = head.get("canonical") or head.get("og:url", "")
    if url:
        metadata["url"] = url
        metadata["site"] = urlparse(url).netloc
    else:
        # No URL saved: use the site name so per-site cleanup still works.
        site = head.get("og:site_name") or (head.get("html_title", "").rsplit(" | ", 1)[-1]
                                             if " | " in head.get("html_title", "") else "")
        if site:
            metadata["site"] = site.strip()
    language = (head.get("lang") or "").split("-")[0].lower() or _guess_language(text)
    if language:
        metadata["language"] = language
    description = head.get("og:description") or head.get("description")
    if description:
        metadata["description"] = description
    for key, name in (("article:published_time", "published"), ("article:modified_time", "modified"),
                      ("datepublished", "published"), ("datemodified", "modified"),
                      ("dcterms.date", "published"), ("citation_publication_date", "published"),
                      ("citation_date", "published")):
        if head.get(key) and name not in metadata:
            metadata[name] = head[key]
    updated = _LAST_UPDATED_RE.search(text[:2000])
    if updated:
        metadata["last_updated"] = updated.group(1).strip(" -–:")
    for key in ("citation_journal_title", "citation_doi", "citation_author"):
        if head.get(key):
            metadata[key.replace("citation_", "")] = head[key]
    if breadcrumb:
        metadata["breadcrumb"] = " > ".join(breadcrumb)
    if og_title and og_title != title:
        metadata["page_title"] = og_title

    return ExtractedPage(title=title, text=text, metadata=metadata, breadcrumb=breadcrumb)
