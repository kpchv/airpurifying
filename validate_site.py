#!/usr/bin/env python3
"""Check generated search discovery, canonical routes, and affiliate markup."""

from collections import deque
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
import xml.etree.ElementTree as ET

DOMAIN = "https://airpurify.ing"
NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.canonicals = []
        self.description = ""
        self.robots = ""
        self.h1s = 0
        self.links = []
        self.schema = []
        self.schema_text = None
        self.title = ""
        self.in_title = False
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonicals.append(attrs.get("href", ""))
        if tag == "meta":
            if attrs.get("name") == "description":
                self.description = attrs.get("content", "")
            if attrs.get("name") == "robots":
                self.robots = attrs.get("content", "")
        if tag == "h1":
            self.h1s += 1
        if tag == "a" and "href" in attrs:
            self.links.append(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.schema_text = ""

    def handle_data(self, data):
        if self.schema_text is not None:
            self.schema_text += data
        if self.in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.schema_text is not None:
            self.schema.append(json.loads(self.schema_text))
            self.schema_text = None


def validate(root):
    errors = []
    redirects = {}
    for line in (root / "_redirects").read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        source, destination, status = line.split()
        if "*" not in source:
            redirects[source] = destination
        if source.startswith("/products/"):
            errors.append(f"Canonical product URL redirects: {source}")
        if destination.startswith("http"):
            errors.append(f"Unexpected external redirect: {source}")

    sitemap_urls = [e.text for e in ET.parse(root / "sitemap.xml").iter(NS + "loc")]
    sitemap = set(sitemap_urls)
    if len(sitemap) != len(sitemap_urls):
        errors.append("Duplicate sitemap URLs")
    image_pages = {e.text for e in ET.parse(root / "sitemap-images.xml").iter(NS + "loc")}
    if not image_pages <= sitemap:
        errors.append("Image sitemap includes noncanonical or nonindexable pages")

    def file_for(path):
        relative = unquote(path).lstrip("/")
        candidate = root / relative
        return candidate / "index.html" if path.endswith("/") or not candidate.suffix else candidate

    def resolve(path):
        visited = set()
        while path in redirects:
            if path in visited:
                raise ValueError(f"Redirect loop: {path}")
            visited.add(path)
            path = redirects[path]
        return path

    for source in redirects:
        target = resolve(source)
        if not file_for(target).is_file():
            errors.append(f"Redirect destination missing: {source} -> {target}")

    # Include browsable noindex catalog entries in link and markup checks.
    catalog_urls = {DOMAIN + "/products/" + p.parent.name + "/" for p in (root / "products").glob("*/index.html")}
    graph = {}
    product_count = affiliate_count = noindex_count = 0
    for url in sorted(sitemap | catalog_urls):
        path = urlsplit(url).path
        if path in redirects:
            errors.append(f"Sitemap URL redirects: {path}")
        target = file_for(path)
        if not target.is_file():
            errors.append(f"Missing sitemap page: {path}")
            continue
        page = Page(target.read_text())
        if page.canonicals != [url]:
            errors.append(f"Canonical mismatch: {path}")
        if "noindex" in page.robots and url in sitemap:
            errors.append(f"Noindex page in sitemap: {path}")
        if "noindex" in page.robots:
            noindex_count += 1
        elif url not in sitemap:
            errors.append(f"Indexable product missing from sitemap: {path}")
        if not page.title.strip() or not page.description.strip() or page.h1s != 1:
            errors.append(f"Missing title/description or incorrect H1 count ({page.h1s}): {path}")
        if not page.schema:
            errors.append(f"Missing structured data: {path}")
        graph[path] = set()
        for link in page.links:
            href = urlsplit(urljoin(url, link["href"]))
            if href.scheme not in {"http", "https"}:
                continue
            host = href.hostname or ""
            if host in {"amazon.com", "www.amazon.com", "amzn.to"}:
                affiliate_count += 1
                if "sponsored" not in link.get("rel", "").split():
                    errors.append(f"Unqualified affiliate link: {path}")
            if host == "airpurify.ing":
                destination = resolve(href.path or "/")
                if not file_for(destination).is_file():
                    errors.append(f"Broken internal link: {path} -> {destination}")
                graph[path].add(destination)
        if path.startswith("/products/") and path != "/products/":
            product_count += 1
            for item in page.schema:
                if item.get("@type") == "Product":
                    if "offers" in item:
                        errors.append(f"Unverified offer data: {path}")
                    if item.get("brand", {}).get("name") == "Air Purifying Systems":
                        errors.append(f"Publisher incorrectly used as product brand: {path}")

    reachable = set()
    pending = deque(["/"])
    while pending:
        path = pending.popleft()
        if path in reachable:
            continue
        reachable.add(path)
        pending.extend(graph.get(path, set()) - reachable)
    orphan_products = {p for p in graph if p.startswith("/products/")} - reachable
    if orphan_products:
        errors.append(f"Product pages unreachable through HTML links: {len(orphan_products)}")
    for entry in json.loads((root / "search-index.json").read_text()):
        if urljoin(DOMAIN, entry["url"]) not in sitemap | catalog_urls:
            errors.append(f"Search index includes a noncanonical URL: {entry['url']}")

    robots_text = (root / "robots.txt").read_text()
    robots = RobotFileParser()
    robots.parse(robots_text.splitlines())
    for bot in ["Googlebot", "Bingbot", "OAI-SearchBot", "PerplexityBot", "Claude-SearchBot", "Claude-User"]:
        for path in ["/", "/articles/", "/products/levoit-core-300-air-purifier/"]:
            if not robots.can_fetch(bot, DOMAIN + path):
                errors.append(f"Search crawler blocked: {bot} {path}")
    if set(robots.site_maps() or []) != {DOMAIN + "/sitemap.xml", DOMAIN + "/sitemap-images.xml"}:
        errors.append("Robots sitemap declarations are missing or incorrect")

    if errors:
        print("FAILED:\n" + "\n".join(sorted(set(errors))))
        return 1
    print(f"PASS: {len(sitemap)} indexable canonical pages, {product_count} reachable product pages "
          f"({noindex_count} noindex catalog entries), "
          f"{affiliate_count} qualified affiliate links; sitemaps, redirects, JSON-LD, "
          "internal links, search index, and search crawler access checked.")
    return 0


if __name__ == "__main__":
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    sys.exit(validate(root))
