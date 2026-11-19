#!/usr/bin/env python3
"""Build a static site from the Air Purifying Systems WordPress backup."""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


DOMAIN = "https://airpurify.ing"
SITE_NAME = "Air Purifying Systems"
SITE_TAGLINE = "Sharing and reviewing the best rated air purifiers"
CONTACT_EMAIL = "mail@airpurify.ing"
AMAZON_ASSOCIATE_TAG = "homemag-20"
SOURCE_ROOT = Path(
    "/Volumes/EXT SSD/websites backups/full backups updraftplus/airpurifying-full-backup/folders"
)
SQL_PATH = SOURCE_ROOT / "backup_2026-03-10-0812_Air_Purifying_Systems_b8bf6bdc6026-db"
UPLOADS_PATH = SOURCE_ROOT / "uploads"
OUT_ROOT = Path(__file__).resolve().parent
ASSETS_DIR = OUT_ROOT / "assets"
UPLOADS_OUT = ASSETS_DIR / "uploads"
PRODUCT_DETAIL_PATHS: dict[str, str] = {}
HOME_DESCRIPTION = (
    "Compare air purifiers by room size, filtration, noise, and features. "
    "Explore product details, buying guides, and recommendations for your home."
)

PRODUCT_USE_CASE_ORDER = [
    "Allergies",
    "Pets",
    "Smoke & Odor",
    "Dust",
    "Mold",
    "Asthma",
    "Baby & Nursery",
    "Filter Replacement",
    "Commercial / Restoration",
    "General Health",
]

PRODUCT_ROOM_ORDER = [
    "Desktop / Car",
    "Bedroom",
    "Nursery",
    "Office",
    "Small Room",
    "Medium Room",
    "Large Room",
    "Whole Home",
    "Commercial Space",
]

PRODUCT_FEATURE_ORDER = [
    "True HEPA",
    "Activated Carbon",
    "Smart / App",
    "Wi-Fi",
    "Low Noise / Sleep",
    "Air Quality Sensor",
    "Timer",
    "Energy Efficient",
    "UV / Ionizer",
    "Washable Filter",
]


@dataclass
class WPPost:
    post_id: int
    post_author: int
    post_date: str
    post_content: str
    post_title: str
    post_excerpt: str
    post_status: str
    post_name: str
    post_parent: int
    guid: str
    post_type: str
    post_mime_type: str
    post_modified: str


class PhpUnserializeError(Exception):
    pass


class PhpUnserializer:
    """Minimal PHP serialize parser for arrays/objects/scalars."""

    def __init__(self, data: str | bytes):
        if isinstance(data, str):
            self.buf = data.encode("utf-8", errors="surrogatepass")
        else:
            self.buf = data
        self.idx = 0

    def parse(self) -> Any:
        value = self._parse_value()
        return value

    def _read(self, n: int) -> bytes:
        if self.idx + n > len(self.buf):
            raise PhpUnserializeError("Unexpected EOF")
        out = self.buf[self.idx : self.idx + n]
        self.idx += n
        return out

    def _expect(self, token: bytes) -> None:
        got = self._read(len(token))
        if got != token:
            raise PhpUnserializeError(f"Expected {token!r}, got {got!r}")

    def _read_until(self, delim: bytes) -> bytes:
        pos = self.buf.find(delim, self.idx)
        if pos < 0:
            raise PhpUnserializeError("Delimiter not found")
        out = self.buf[self.idx : pos]
        self.idx = pos + len(delim)
        return out

    def _parse_value(self) -> Any:
        t = self._read(1)

        if t == b"N":
            self._expect(b";")
            return None

        self._expect(b":")

        if t in (b"i", b"d", b"b"):
            raw = self._read_until(b";").decode("ascii", errors="ignore")
            if t == b"i":
                return int(raw or "0")
            if t == b"d":
                return float(raw or "0")
            return raw == "1"

        if t == b"s":
            length = int(self._read_until(b":") or b"0")
            self._expect(b'"')
            raw = self._read(length)
            if self.buf[self.idx : self.idx + 2] == b'";':
                self.idx += 2
            else:
                # fallback for malformed lengths
                end = self.buf.find(b'";', self.idx)
                if end < 0:
                    raise PhpUnserializeError("Unterminated string")
                raw = self.buf[self.idx - len(raw) : end]
                self.idx = end + 2
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin1", errors="replace")

        if t == b"a":
            count = int(self._read_until(b":") or b"0")
            self._expect(b"{")
            out: dict[Any, Any] = {}
            list_like = True
            for i in range(count):
                key = self._parse_value()
                val = self._parse_value()
                out[key] = val
                if key != i:
                    list_like = False
            self._expect(b"}")
            if list_like:
                return [out[i] for i in range(count)]
            return out

        if t == b"O":
            class_len = int(self._read_until(b":") or b"0")
            self._expect(b'"')
            cls = self._read(class_len)
            self._expect(b'":')
            prop_count = int(self._read_until(b":") or b"0")
            self._expect(b"{")
            out: dict[Any, Any] = {"__class__": cls.decode("utf-8", errors="replace")}
            for _ in range(prop_count):
                key = self._parse_value()
                val = self._parse_value()
                out[key] = val
            self._expect(b"}")
            return out

        raise PhpUnserializeError(f"Unsupported serialized type {t!r}")


class SQLDump:
    def __init__(self, sql_text: str):
        self.sql_text = sql_text

    def iter_insert_statements(self, table: str):
        needle = f"INSERT INTO `{table}` VALUES "
        pos = 0
        while True:
            start = self.sql_text.find(needle, pos)
            if start < 0:
                return

            i = start
            in_str = False
            esc = False
            while i < len(self.sql_text):
                c = self.sql_text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == "'":
                        in_str = False
                else:
                    if c == "'":
                        in_str = True
                    elif c == ";":
                        i += 1
                        break
                i += 1

            yield self.sql_text[start:i]
            pos = i

    @staticmethod
    def split_rows(values_clause: str) -> list[list[str]]:
        rows: list[list[str]] = []
        i = 0
        n = len(values_clause)
        while i < n:
            while i < n and values_clause[i] != "(":
                i += 1
            if i >= n:
                break

            i += 1
            row: list[str] = []
            buf: list[str] = []
            in_str = False
            esc = False

            while i < n:
                c = values_clause[i]
                if in_str:
                    if esc:
                        buf.append("\\" + c)
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == "'":
                        in_str = False
                        buf.append(c)
                    else:
                        buf.append(c)
                    i += 1
                    continue

                if c == "'":
                    in_str = True
                    buf.append(c)
                    i += 1
                    continue
                if c == ",":
                    row.append("".join(buf).strip())
                    buf = []
                    i += 1
                    continue
                if c == ")":
                    row.append("".join(buf).strip())
                    i += 1
                    rows.append(row)
                    break

                buf.append(c)
                i += 1

        return rows

    @staticmethod
    def decode_mysql_token(token: str) -> str | None:
        if token == "NULL":
            return None
        if len(token) >= 2 and token[0] == "'" and token[-1] == "'":
            inner = token[1:-1]
            out: list[str] = []
            i = 0
            escape_map = {
                "0": "\x00",
                "b": "\b",
                "n": "\n",
                "r": "\r",
                "t": "\t",
                "Z": "\x1a",
                "\\": "\\",
                "'": "'",
                '"': '"',
            }
            while i < len(inner):
                c = inner[i]
                if c == "\\" and i + 1 < len(inner):
                    n = inner[i + 1]
                    out.append(escape_map.get(n, n))
                    i += 2
                else:
                    out.append(c)
                    i += 1
            return "".join(out)
        return token


def load_wp_data(sql_path: Path) -> dict[str, Any]:
    sql_text = sql_path.read_text(encoding="utf-8", errors="replace")
    dump = SQLDump(sql_text)

    options: dict[str, str] = {}
    posts: dict[int, WPPost] = {}
    postmeta: dict[int, dict[str, list[str]]] = {}
    terms: dict[int, dict[str, str]] = {}
    term_taxonomy: dict[int, dict[str, Any]] = {}
    term_relationships: dict[int, list[int]] = {}
    cegg_product_index: dict[str, dict[str, str]] = {}

    for stmt in dump.iter_insert_statements("wp_options"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 4:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            if d[1] is not None:
                options[d[1]] = d[2] or ""

    for stmt in dump.iter_insert_statements("wp_posts"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 23:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            pid = int(d[0] or 0)
            posts[pid] = WPPost(
                post_id=pid,
                post_author=int(d[1] or 0),
                post_date=d[2] or "",
                post_content=d[4] or "",
                post_title=d[5] or "",
                post_excerpt=d[6] or "",
                post_status=d[7] or "",
                post_name=d[11] or "",
                post_parent=int(d[17] or 0),
                guid=d[18] or "",
                post_type=d[20] or "",
                post_mime_type=d[21] or "",
                post_modified=d[14] or "",
            )

    for stmt in dump.iter_insert_statements("wp_postmeta"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 4:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            pid = int(d[1] or 0)
            key = d[2] or ""
            val = d[3] or ""
            postmeta.setdefault(pid, {}).setdefault(key, []).append(val)

    for stmt in dump.iter_insert_statements("wp_terms"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 4:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            term_id = int(d[0] or 0)
            terms[term_id] = {"name": d[1] or "", "slug": d[2] or ""}

    for stmt in dump.iter_insert_statements("wp_term_taxonomy"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 6:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            tt_id = int(d[0] or 0)
            term_taxonomy[tt_id] = {
                "term_id": int(d[1] or 0),
                "taxonomy": d[2] or "",
                "description": d[3] or "",
                "count": int(d[4] or 0),
            }

    for stmt in dump.iter_insert_statements("wp_term_relationships"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 3:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            obj_id = int(d[0] or 0)
            tt_id = int(d[1] or 0)
            term_relationships.setdefault(obj_id, []).append(tt_id)

    for stmt in dump.iter_insert_statements("wp_cegg_product"):
        values = stmt.split("VALUES ", 1)[1].rstrip(" ;\n")
        for row in dump.split_rows(values):
            if len(row) < 15:
                continue
            d = [dump.decode_mysql_token(x) for x in row]
            unique_id = (d[1] or "").strip()
            if not unique_id:
                continue
            entry = {
                "title": (d[12] or "").strip(),
                "image": (d[13] or "").strip(),
                "url": (d[14] or "").strip(),
                "price": (d[9] or "").strip(),
                "old_price": (d[10] or "").strip(),
                "currency": (d[11] or "").strip(),
            }
            prev = cegg_product_index.get(unique_id)
            if not prev:
                cegg_product_index[unique_id] = entry
            else:
                # Prefer entries with image and non-empty URL/price.
                score_prev = int(bool(prev.get("image"))) + int(bool(prev.get("url"))) + int(
                    bool(prev.get("price"))
                )
                score_new = int(bool(entry.get("image"))) + int(bool(entry.get("url"))) + int(
                    bool(entry.get("price"))
                )
                if score_new >= score_prev:
                    cegg_product_index[unique_id] = entry

    return {
        "options": options,
        "posts": posts,
        "postmeta": postmeta,
        "terms": terms,
        "term_taxonomy": term_taxonomy,
        "term_relationships": term_relationships,
        "cegg_product_index": cegg_product_index,
    }


def slugify(text: str, fallback: str = "item") -> str:
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower()
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return ascii_text or fallback


def html_to_text(value: str) -> str:
    cleaned = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    cleaned = re.sub(r"<style[\s\S]*?</style>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def excerpt_from_content(content: str, max_len: int = 165) -> str:
    txt = html_to_text(sanitize_html_content(content))
    if len(txt) <= max_len:
        return txt
    return txt[: max_len - 1].rstrip() + "…"


def reading_time_minutes(content: str, words_per_minute: int = 220) -> int:
    text = html_to_text(sanitize_html_content(content))
    if not text:
        return 1
    words = len(re.findall(r"\b\w+\b", text))
    return max(1, (words + words_per_minute - 1) // words_per_minute)


def display_date(date_value: str) -> str:
    token = (date_value or "").strip()[:10]
    if not token:
        return ""
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", token)
    if not match:
        return token
    year, month, day = match.groups()
    month_index = int(month)
    month_names = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    if month_index < 1 or month_index > 12:
        return token
    return f"{month_names[month_index - 1]} {int(day)}, {year}"


def clip_text(value: str, max_len: int = 170) -> str:
    text = re.sub(r"\s+", " ", html.unescape(value or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def normalize_meta_description(value: str, fallback: str = "", max_len: int = 165) -> str:
    candidate = html_to_text(value or "")
    if not candidate.strip():
        candidate = html_to_text(fallback or "")
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if not candidate:
        return clip_text(SITE_TAGLINE, max_len)
    return clip_text(candidate, max_len)


def iso_date_token(date_value: str) -> str:
    token = (date_value or "").strip()[:10]
    if re.match(r"^\d{4}-\d{2}-\d{2}$", token or ""):
        return token
    return ""


def sanitize_html_content(content: str) -> str:
    # Preserve source HTML, remove WP block comments wrappers only.
    cleaned = re.sub(r"<!--\s*/?wp:[\s\S]*?-->", "", content)
    # Drop source theme scripts/styles that can break static layouts.
    cleaned = re.sub(r"<script\b[^>]*>[\s\S]*?</script>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<style\b[^>]*>[\s\S]*?</style>", "", cleaned, flags=re.I)
    cleaned = re.sub(r"<noscript\b[^>]*>[\s\S]*?</noscript>", "", cleaned, flags=re.I)
    # Remove leftover slider navigation glyphs from old Elementor markup.
    cleaned = re.sub(
        r"<svg\b[^>]*viewBox=[\"']0 0 1000 1000[\"'][^>]*>[\s\S]*?</svg>",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"<svg\b[\s\S]*?</svg>\s*(?:Previous|Next)\s+slide",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"\b(?:Previous|Next)\s+slide\b", "", cleaned, flags=re.I)
    cleaned = re.sub(
        r"<(h[1-6]|p)>\s*(?:&nbsp;|\u00a0|\s)*</\1>",
        "",
        cleaned,
        flags=re.I,
    )
    # Page templates already render a single main H1, so demote in-body H1 tags.
    cleaned = re.sub(r"<\s*h1(\b[^>]*)>", r"<h2\1>", cleaned, flags=re.I)
    cleaned = re.sub(r"</\s*h1\s*>", "</h2>", cleaned, flags=re.I)
    cleaned = re.sub(r"\s{3,}", " ", cleaned)
    return cleaned.strip()


def fallback_image_alt(source_url: str, fallback: str = "Air purifier image") -> str:
    src = html.unescape((source_url or "").strip())
    if src:
        path = urlsplit(src).path or src
        token = Path(path).stem
        token = re.sub(r"-\d+x\d+$", "", token, flags=re.I)
        token = re.sub(r"-scaled$", "", token, flags=re.I)
        token = re.sub(r"[_\-\s]+", " ", token).strip()
        if token:
            cleaned = clip_text(token, 90)
            return cleaned
    fallback_clean = html_to_text(fallback or "").strip()
    if fallback_clean:
        return clip_text(fallback_clean, 90)
    return "Air purifier image"


def ensure_image_alt_attributes(content: str, fallback_alt: str = "") -> str:
    if "<img" not in (content or "").lower():
        return content

    img_pattern = re.compile(r"<img\b[^>]*>", flags=re.I)
    src_pattern = re.compile(r'\bsrc\s*=\s*([\"\'])([^\"\']+)\1', flags=re.I)
    alt_pattern = re.compile(r'\balt\s*=\s*([\"\'])(.*?)\1', flags=re.I | re.S)

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        src_match = src_pattern.search(tag)
        src = src_match.group(2).strip() if src_match else ""
        alt_match = alt_pattern.search(tag)
        alt_value = alt_match.group(2) if alt_match else ""
        if alt_value and html_to_text(alt_value).strip():
            return tag

        alt_text = html.escape(fallback_image_alt(src, fallback=fallback_alt), quote=True)
        if alt_match:
            return tag[: alt_match.start()] + f'alt={alt_match.group(1)}{alt_text}{alt_match.group(1)}' + tag[alt_match.end() :]
        if tag.endswith("/>"):
            return tag[:-2] + f' alt="{alt_text}"/>'
        if tag.endswith(">"):
            return tag[:-1] + f' alt="{alt_text}">'
        return tag

    return img_pattern.sub(repl, content)


def is_amazon_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return False
    if host == "amzn.to" or host.endswith(".amzn.to"):
        return True
    return bool(re.search(r"(^|\.)amazon\.[a-z.]+$", host))


def affiliateize_url(url: str) -> str:
    value = normalize_external_url(url)
    if not re.match(r"^https?://", value, flags=re.I):
        return value

    parsed = urlsplit(html.unescape(value))
    if not is_amazon_host(parsed.netloc):
        return value

    query_pairs = [(k, v) for (k, v) in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "tag"]
    query_pairs.append(("tag", AMAZON_ASSOCIATE_TAG))
    tagged = parsed._replace(query=urlencode(query_pairs, doseq=True))
    return urlunsplit(tagged)


def affiliateize_html_links(content: str) -> str:
    attr_pattern = re.compile(r"((?:href|src)\s*=\s*[\"'])(https?://[^\"']+)([\"'])", flags=re.I)

    def repl(match: re.Match[str]) -> str:
        prefix = match.group(1)
        url = match.group(2)
        suffix = match.group(3)
        tagged = affiliateize_url(url)
        return f"{prefix}{html.escape(tagged, quote=True)}{suffix}"

    return attr_pattern.sub(repl, content)


def enforce_amazon_anchor_attrs(content: str) -> str:
    anchor_pattern = re.compile(r"<a\b[^>]*>", flags=re.I)
    href_pattern = re.compile(r'href\s*=\s*([\"\'])([^\"\']+)\1', flags=re.I)
    rel_pattern = re.compile(r'rel\s*=\s*([\"\'])([^\"\']*)\1', flags=re.I)

    def repl(match: re.Match[str]) -> str:
        tag = match.group(0)
        href_match = href_pattern.search(tag)
        if not href_match:
            return tag
        raw_href = href_match.group(2)
        parsed = urlsplit(html.unescape(raw_href))
        if not is_amazon_host(parsed.netloc):
            return tag

        tagged_href = affiliateize_url(raw_href)
        tag = href_pattern.sub(
            lambda m: f'href={m.group(1)}{html.escape(tagged_href, quote=True)}{m.group(1)}',
            tag,
            count=1,
        )

        rel_match = rel_pattern.search(tag)
        required_tokens = ["sponsored", "nofollow", "noopener"]
        if rel_match:
            existing = rel_match.group(2).strip()
            tokens = [token for token in re.split(r"\s+", existing) if token]
            for token in required_tokens:
                if token not in tokens:
                    tokens.append(token)
            merged = " ".join(tokens)
            tag = rel_pattern.sub(
                lambda m: f'rel={m.group(1)}{html.escape(merged, quote=True)}{m.group(1)}',
                tag,
                count=1,
            )
        else:
            tag = tag[:-1] + ' rel="sponsored nofollow noopener">'

        if not re.search(r"\btarget\s*=", tag, flags=re.I):
            tag = tag[:-1] + ' target="_blank">'

        return tag

    return anchor_pattern.sub(repl, content)


def linkify_amazon_urls(content: str) -> str:
    parts = re.split(r"(<[^>]+>)", content)
    url_pattern = re.compile(
        r"https?://(?:www\.)?(?:amazon\.[^\s<>'\"]+|amzn\.to/[^\s<>'\"]+)",
        flags=re.I,
    )

    def text_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,);:!?":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        tagged = affiliateize_url(raw)
        link = (
            f'<a href="{html.escape(tagged, quote=True)}" rel="sponsored nofollow noopener" target="_blank">'
            f"{html.escape(raw)}</a>"
        )
        return link + trailing

    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            continue
        parts[idx] = url_pattern.sub(text_repl, part)
    return "".join(parts)


def linkify_plain_urls(content: str) -> str:
    parts = re.split(r"(<[^>]+>)", content)
    url_pattern = re.compile(
        r"https?://[^\s<>'\"]+",
        flags=re.I,
    )

    def text_repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,);:!?":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        if is_amazon_host(urlsplit(raw).netloc):
            rel_attr = "sponsored nofollow noopener"
            linked = affiliateize_url(raw)
        else:
            rel_attr = "nofollow noopener"
            linked = raw
        link = (
            f'<a href="{html.escape(linked, quote=True)}" rel="{rel_attr}" target="_blank">'
            f"{html.escape(raw)}</a>"
        )
        return link + trailing

    in_anchor = False
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            if re.match(r"<a\b", part, flags=re.I):
                in_anchor = True
            elif re.match(r"</a\b", part, flags=re.I):
                in_anchor = False
            continue
        if in_anchor:
            continue
        parts[idx] = url_pattern.sub(text_repl, part)
    return "".join(parts)


def normalize_internal_attr_paths(content: str) -> str:
    attr_pattern = re.compile(r"((?:href|src)\s*=\s*[\"'])(/[^\"']*)([\"'])", flags=re.I)

    def repl(match: re.Match[str]) -> str:
        prefix, path, suffix = match.groups()
        normalized = re.sub(r"/{2,}", "/", path)
        return f"{prefix}{normalized}{suffix}"

    return attr_pattern.sub(repl, content)


def rewrite_urls(content: str) -> str:
    replaced = content
    replaced = replaced.replace("https://airpurify.ing/wp-content/uploads/", "/assets/uploads/")
    replaced = replaced.replace("http://airpurify.ing/wp-content/uploads/", "/assets/uploads/")
    replaced = replaced.replace("/wp-content/uploads/", "/assets/uploads/")
    replaced = replaced.replace("https://www.airpurify.ing/wp-content/uploads/", "/assets/uploads/")
    replaced = replaced.replace(
        "https://i0.wp.com/airpurify.ing/wp-content/uploads/", "/assets/uploads/"
    )
    replaced = replaced.replace(
        "https://i0.wp.com/www.airpurify.ing/wp-content/uploads/", "/assets/uploads/"
    )
    replaced = replaced.replace("https://i0.wp.com/airpurify.ing/assets/uploads/", "/assets/uploads/")
    replaced = replaced.replace(
        "https://i0.wp.com/www.airpurify.ing/assets/uploads/", "/assets/uploads/"
    )
    replaced = re.sub(
        r"https?://(?:www\.)?airpurify\.ing/product/([^\s\"'#?]+)/?",
        r"/products/\1/",
        replaced,
        flags=re.I,
    )
    replaced = re.sub(r"(['\"])/product/([^\s\"'#?]+)/", r"\1/products/\2/", replaced)
    replaced = re.sub(
        r"https?://(?:www\.)?airpurify\.ing/reviews/([^\s\"'#?]+)/?",
        r"/products/\1/",
        replaced,
        flags=re.I,
    )
    replaced = re.sub(r"(['\"])/reviews/([^\s\"'#?]+)/", r"\1/products/\2/", replaced)
    replaced = re.sub(
        r"https?://(?:www\.)?airpurify\.ing/wp-admin/admin-ajax\.php\?paged=\d+",
        "/articles/",
        replaced,
        flags=re.I,
    )
    replaced = re.sub(
        r"https?://(?:www\.)?airpurify\.ing/([^\s\"'#?]+)",
        r"/\1",
        replaced,
        flags=re.I,
    )
    replaced = re.sub(r"https?://(?:www\.)?airpurify\.ing/?", "/", replaced, flags=re.I)
    replaced = affiliateize_html_links(replaced)
    replaced = enforce_amazon_anchor_attrs(replaced)
    replaced = linkify_plain_urls(replaced)
    replaced = normalize_internal_attr_paths(replaced)
    return replaced


def link_unlinked_images(content: str, destination_url: str) -> str:
    target = external_http_url(destination_url)
    if not target or "<img" not in content.lower():
        return content

    img_pattern = re.compile(r"<img\b[^>]*>", flags=re.I)
    out: list[str] = []
    cursor = 0
    for match in img_pattern.finditer(content):
        start, end = match.span()
        image_html = match.group(0)
        open_anchor = content.rfind("<a", 0, start)
        close_anchor = content.rfind("</a>", 0, start)
        out.append(content[cursor:start])
        if open_anchor != -1 and open_anchor > close_anchor:
            out.append(image_html)
        else:
            out.append(
                f'<a class="image-link" href="{html.escape(target)}" rel="sponsored nofollow noopener" target="_blank">{image_html}</a>'
            )
        cursor = end
    out.append(content[cursor:])
    return "".join(out)


def normalize_image_source_key(url: str) -> str:
    raw = html.unescape((url or "").strip())
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"

    parsed = urlsplit(raw)
    if parsed.scheme:
        path = parsed.path
    else:
        path = raw.split("?", 1)[0].split("#", 1)[0]
    path = html.unescape(path).strip()
    if not path:
        return ""

    if re.match(r"^https?://", path, flags=re.I):
        nested = urlsplit(path)
        path = nested.path

    path = re.sub(r"^/(?:www\.)?airpurify\.ing/wp-content/uploads/", "/assets/uploads/", path, flags=re.I)
    path = re.sub(r"^/(?:www\.)?airpurify\.ing/assets/uploads/", "/assets/uploads/", path, flags=re.I)
    path = re.sub(r"^/wp-content/uploads/", "/assets/uploads/", path, flags=re.I)
    if not path.startswith("/"):
        path = "/" + path.lstrip("/")
    path = re.sub(r"/{2,}", "/", path)
    path = unquote(path)
    path = re.sub(r"-\d+x\d+(?=\.[a-z0-9]{2,5}$)", "", path, flags=re.I)
    path = re.sub(r"-scaled(?=\.[a-z0-9]{2,5}$)", "", path, flags=re.I)
    return path.lower()


def remove_duplicate_featured_image(content: str, featured_img_url: str) -> tuple[str, str]:
    if not content.strip() or not featured_img_url.strip():
        return content, ""

    featured_key = normalize_image_source_key(featured_img_url)
    if not featured_key:
        return content, ""

    img_pattern = re.compile(r"<img\b[^>]*\bsrc\s*=\s*([\"'])([^\"']+)\1[^>]*>", flags=re.I)
    href_pattern = re.compile(r'href\s*=\s*([\"\'])([^\"\']+)\1', flags=re.I)

    for match in img_pattern.finditer(content):
        src = (match.group(2) or "").strip()
        if normalize_image_source_key(src) != featured_key:
            continue

        start, end = match.span()
        remove_start, remove_end = start, end
        inherited_link = ""

        open_anchor = content.rfind("<a", 0, start)
        close_anchor_before = content.rfind("</a>", 0, start)
        if open_anchor != -1 and open_anchor > close_anchor_before:
            anchor_tag_end = content.find(">", open_anchor)
            close_anchor_after = content.find("</a>", end)
            if anchor_tag_end != -1 and close_anchor_after != -1 and anchor_tag_end < start:
                anchor_tag = content[open_anchor : anchor_tag_end + 1]
                href_match = href_pattern.search(anchor_tag)
                if href_match:
                    candidate = external_http_url(href_match.group(2))
                    if candidate and is_amazon_host(urlsplit(candidate).netloc):
                        inherited_link = candidate

                anchor_inner = content[anchor_tag_end + 1 : close_anchor_after]
                anchor_inner_without_images = re.sub(r"<img\b[^>]*>", "", anchor_inner, flags=re.I)
                if not html_to_text(anchor_inner_without_images).strip():
                    remove_start = open_anchor
                    remove_end = close_anchor_after + len("</a>")

        open_figure = content.rfind("<figure", 0, remove_start)
        close_figure_before = content.rfind("</figure>", 0, remove_start)
        if open_figure != -1 and open_figure > close_figure_before:
            figure_tag_end = content.find(">", open_figure)
            close_figure_after = content.find("</figure>", remove_end)
            if figure_tag_end != -1 and close_figure_after != -1 and figure_tag_end < remove_start:
                remove_start = open_figure
                remove_end = close_figure_after + len("</figure>")

        cleaned = content[:remove_start] + content[remove_end:]
        cleaned = re.sub(r"<(p|figure|div)\b[^>]*>\s*(?:&nbsp;|\u00a0|\s)*</\1>", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s{3,}", " ", cleaned)
        return cleaned.strip(), inherited_link

    return content, ""


def post_summary_text(post: WPPost, featured_img_url: str, max_len: int) -> str:
    manual_excerpt = (post.post_excerpt or "").strip()
    if manual_excerpt:
        return excerpt_from_content(manual_excerpt, max_len)

    cleaned = rewrite_urls(sanitize_html_content(post.post_content or ""))
    if featured_img_url:
        cleaned, _ = remove_duplicate_featured_image(cleaned, featured_img_url)

    # Ignore media wrappers/captions when generating intro snippets.
    cleaned = re.sub(r"<figure\b[\s\S]*?</figure>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"<figcaption\b[\s\S]*?</figcaption>", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"<img\b[^>]*>", " ", cleaned, flags=re.I)

    paragraph_pattern = re.compile(r"<p\b[^>]*>([\s\S]*?)</p>", flags=re.I)
    for match in paragraph_pattern.finditer(cleaned):
        paragraph_text = re.sub(r"\s+", " ", html_to_text(match.group(1))).strip()
        if len(paragraph_text) >= 40:
            return clip_text(paragraph_text, max_len)

    without_headings = re.sub(r"<h[1-6]\b[\s\S]*?</h[1-6]>", " ", cleaned, flags=re.I)
    headingless_excerpt = excerpt_from_content(without_headings, max_len)
    if headingless_excerpt:
        return headingless_excerpt

    return excerpt_from_content(cleaned, max_len)


def get_first(meta: dict[str, list[str]], key: str, default: str = "") -> str:
    vals = meta.get(key)
    if not vals:
        return default
    return vals[0] or default


def parse_php_serialized(value: str) -> Any:
    parser = PhpUnserializer(value)
    return parser.parse()


def extract_attachment_path(post: WPPost, meta: dict[str, list[str]]) -> str:
    attached = get_first(meta, "_wp_attached_file", "")
    if attached:
        return attached.lstrip("/")

    if post.guid and "/wp-content/uploads/" in post.guid:
        return post.guid.split("/wp-content/uploads/", 1)[1].lstrip("/")

    return ""


def build_taxonomy_lookup(data: dict[str, Any]) -> dict[int, dict[str, list[str]]]:
    terms = data["terms"]
    term_taxonomy = data["term_taxonomy"]
    relationships = data["term_relationships"]

    out: dict[int, dict[str, list[str]]] = {}
    for post_id, tt_ids in relationships.items():
        for tt_id in tt_ids:
            tt = term_taxonomy.get(tt_id)
            if not tt:
                continue
            term = terms.get(tt["term_id"], {"name": "", "slug": ""})
            tax_name = tt["taxonomy"]
            out.setdefault(post_id, {}).setdefault(tax_name, []).append(term["name"])
    return out


def extract_cegg_products(
    meta: dict[str, list[str]], cegg_product_index: dict[str, dict[str, str]] | None = None
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cegg_product_index = cegg_product_index or {}
    for raw in meta.get("_cegg_data_Amazon", []):
        try:
            parsed = parse_php_serialized(raw)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue

        for unique_id, payload in parsed.items():
            if not isinstance(payload, dict):
                continue
            unique_id_str = str(unique_id)
            fallback = cegg_product_index.get(unique_id_str, {})

            title = str(payload.get("title") or fallback.get("title") or "").strip()
            if not title:
                continue
            entries.append(
                {
                    "unique_id": unique_id_str,
                    "title": title,
                    "url": str(payload.get("url") or fallback.get("url") or "").strip(),
                    "image": str(payload.get("image") or fallback.get("image") or "").strip(),
                    "price": str(payload.get("price") or fallback.get("price") or "").strip(),
                    "old_price": str(payload.get("old_price") or fallback.get("old_price") or "").strip(),
                    "currency": str(payload.get("currency") or fallback.get("currency") or "").strip(),
                    "description": str(payload.get("description") or "").strip(),
                    "rating": str(payload.get("rating") or "").strip(),
                }
            )
    return entries


def build_canonical(path: str) -> str:
    clean = path.strip("/")
    if not clean:
        return f"{DOMAIN}/"
    return f"{DOMAIN}/{clean}/"


def normalize_external_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    return value


def external_http_url(url: str) -> str:
    value = affiliateize_url(normalize_external_url(url))
    if re.match(r"^https?://", value, flags=re.I):
        return value
    return ""


def absolute_url(url: str) -> str:
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    if re.match(r"^https?://", value, flags=re.I):
        return value
    if value.startswith("/"):
        return f"{DOMAIN}{value}"
    return f"{DOMAIN}/{value.lstrip('/')}"


def layout_html(
    *,
    page_title: str,
    meta_description: str,
    canonical: str,
    body_class: str,
    main_html: str,
    og_image: str,
    json_ld: list[dict[str, Any]],
    logo_url: str,
    site_icon_url: str,
    recommendations_nav_html: str,
    og_type: str = "website",
    extra_head_tags: str = "",
    robots_directive: str = "index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1",
) -> str:
    affiliate_notice = ""
    if 'rel="sponsored' in main_html:
        affiliate_notice = (
            '<p class="affiliate-notice">As an Amazon Associate, we earn from qualifying purchases. '
            '<a href="/disclaimer/">Affiliate disclosure</a>.</p>'
        )
    ld = "\n".join(
        f'<script type="application/ld+json">{json.dumps(obj, ensure_ascii=False)}</script>'
        for obj in json_ld
    )
    interactive_script = """
  <script>
  (() => {
    const header = document.querySelector('.site-header');
    const dropdown = document.querySelector('.nav-dropdown[data-behavior="recommendations"]');
    if (header && dropdown) {
      const trigger = dropdown.querySelector(':scope > a');
      const openMenu = () => {
        dropdown.classList.add('is-open');
        if (trigger) {
          trigger.setAttribute('aria-expanded', 'true');
        }
      };
      const closeMenu = () => {
        dropdown.classList.remove('is-open');
        if (trigger) {
          trigger.setAttribute('aria-expanded', 'false');
        }
      };

      let armed = false;
      const armAndOpen = () => {
        armed = true;
        openMenu();
      };

      if (trigger) {
        trigger.addEventListener('mouseenter', armAndOpen);
        trigger.addEventListener('focus', armAndOpen);
      }
      dropdown.addEventListener('mouseenter', armAndOpen);
      header.addEventListener('mouseenter', () => {
        if (armed) {
          openMenu();
        }
      });

      const maybeClose = (event) => {
        const next = event.relatedTarget;
        if (next && (header.contains(next) || dropdown.contains(next))) {
          return;
        }
        armed = false;
        closeMenu();
      };
      header.addEventListener('mouseleave', maybeClose);
      dropdown.addEventListener('mouseleave', maybeClose);

      document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          armed = false;
          closeMenu();
        }
      });
      document.addEventListener('click', (event) => {
        if (!dropdown.contains(event.target)) {
          armed = false;
          closeMenu();
        }
      });
    }

    const filterRoots = document.querySelectorAll('[data-rec-filter-root]');
    for (const root of filterRoots) {
      const input = root.querySelector('[data-rec-search]');
      if (!input) {
        continue;
      }
      const items = Array.from(root.querySelectorAll('[data-rec-item]'));
      const groups = Array.from(root.querySelectorAll('[data-rec-group]'));
      const empty = root.querySelector('[data-rec-empty]');
      const applyFilter = () => {
        const query = (input.value || '').trim().toLowerCase();
        let visibleCount = 0;
        for (const item of items) {
          const haystack = (item.getAttribute('data-rec-text') || item.textContent || '').toLowerCase();
          const matches = !query || haystack.includes(query);
          item.hidden = !matches;
          if (matches) {
            visibleCount += 1;
          }
        }
        for (const group of groups) {
          const visibleInGroup = group.querySelectorAll('[data-rec-item]:not([hidden])').length;
          group.hidden = visibleInGroup === 0;
        }
        if (empty) {
          empty.hidden = visibleCount > 0;
        }
      };
      input.addEventListener('input', applyFilter);
      applyFilter();
    }

    const parseFilterValues = (raw) =>
      String(raw || '')
        .split('|')
        .map((value) => value.trim().toLowerCase())
        .filter(Boolean);

    const featuredProductsGrid = document.querySelector('[data-featured-products]');
    if (featuredProductsGrid) {
      const featuredProductCount = 12;
      fetch('/products/', { cache: 'force-cache' })
        .then((response) => {
          if (!response.ok) {
            throw new Error('Unable to load product catalog');
          }
          return response.text();
        })
        .then((catalogHtml) => {
          const catalog = new DOMParser().parseFromString(catalogHtml, 'text/html');
          const products = Array.from(catalog.querySelectorAll('.product-grid [data-product-item]'));
          for (let index = products.length - 1; index > 0; index -= 1) {
            const randomIndex = Math.floor(Math.random() * (index + 1));
            [products[index], products[randomIndex]] = [products[randomIndex], products[index]];
          }
          const featuredProducts = products
            .slice(0, featuredProductCount)
            .map((product) => document.importNode(product, true));
          if (featuredProducts.length) {
            featuredProductsGrid.replaceChildren(...featuredProducts);
          }
        })
        .catch(() => {});
    }

    const productFilterRoots = document.querySelectorAll('[data-product-filter-root]');
    for (const root of productFilterRoots) {
      const searchInput = root.querySelector('[data-product-search]');
      const useCaseSelect = root.querySelector('[data-product-use-case]');
      const roomSelect = root.querySelector('[data-product-room]');
      const featureSelect = root.querySelector('[data-product-feature]');
      const resetButton = root.querySelector('[data-product-reset]');
      const items = Array.from(root.querySelectorAll('[data-product-item]'));
      const empty = root.querySelector('[data-product-empty]');
      const summary = root.querySelector('[data-product-summary]');

      if (!items.length) {
        continue;
      }

      const totalCount = items.length;
      const applyProductFilter = () => {
        const query = (searchInput?.value || '').trim().toLowerCase();
        const useCase = (useCaseSelect?.value || '').trim().toLowerCase();
        const room = (roomSelect?.value || '').trim().toLowerCase();
        const feature = (featureSelect?.value || '').trim().toLowerCase();
        let visibleCount = 0;

        for (const item of items) {
          const haystack = (item.getAttribute('data-product-search') || '').toLowerCase();
          const useCases = parseFilterValues(item.getAttribute('data-product-use-cases'));
          const rooms = parseFilterValues(item.getAttribute('data-product-rooms'));
          const features = parseFilterValues(item.getAttribute('data-product-features'));

          const matchesQuery = !query || haystack.includes(query);
          const matchesUseCase = !useCase || useCases.includes(useCase);
          const matchesRoom = !room || rooms.includes(room);
          const matchesFeature = !feature || features.includes(feature);
          const matches = matchesQuery && matchesUseCase && matchesRoom && matchesFeature;
          item.hidden = !matches;
          if (matches) {
            visibleCount += 1;
          }
        }

        if (empty) {
          empty.hidden = visibleCount > 0;
        }
        if (summary) {
          summary.textContent =
            visibleCount === totalCount
              ? `Showing all ${totalCount} products.`
              : `Showing ${visibleCount} of ${totalCount} products.`;
        }
      };

      for (const control of [searchInput, useCaseSelect, roomSelect, featureSelect]) {
        if (!control) {
          continue;
        }
        const eventName = control.tagName === 'SELECT' ? 'change' : 'input';
        control.addEventListener(eventName, applyProductFilter);
      }

      if (resetButton) {
        resetButton.addEventListener('click', () => {
          if (searchInput) {
            searchInput.value = '';
          }
          if (useCaseSelect) {
            useCaseSelect.value = '';
          }
          if (roomSelect) {
            roomSelect.value = '';
          }
          if (featureSelect) {
            featureSelect.value = '';
          }
          applyProductFilter();
        });
      }

      applyProductFilter();
    }

    const searchBoxes = document.querySelectorAll('[data-site-search]');
    let searchDataPromise = null;
    const loadSearchData = async () => {
      if (!searchDataPromise) {
        searchDataPromise = fetch('/search-index.json', { cache: 'force-cache' })
          .then((response) => (response.ok ? response.json() : []))
          .catch(() => []);
      }
      const data = await searchDataPromise;
      return Array.isArray(data) ? data : [];
    };

    const escapeHtml = (value) =>
      String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');

    for (const box of searchBoxes) {
      const input = box.querySelector('[data-site-search-input]');
      const results = box.querySelector('[data-site-search-results]');
      if (!input || !results) {
        continue;
      }

      const hideResults = () => {
        results.hidden = true;
        results.innerHTML = '';
        box.classList.remove('has-results');
      };

      const renderNoMatches = () => {
        results.innerHTML = '<p class="search-empty">No matching pages found.</p>';
        results.hidden = false;
        box.classList.add('has-results');
      };

      const renderMatches = (matches) => {
        const html = matches
          .map((match) => {
            const title = escapeHtml(match.title || 'Untitled');
            const type = escapeHtml(match.type || 'Page');
            const url = escapeHtml(match.url || '/');
            return `<a class="search-hit" href="${url}"><span class="search-hit-title">${title}</span><span class="search-hit-meta">${type}</span></a>`;
          })
          .join('');
        results.innerHTML = html;
        results.hidden = false;
        box.classList.add('has-results');
      };

      const runSearch = async () => {
        const rawQuery = (input.value || '').trim().toLowerCase();
        if (rawQuery.length < 2) {
          hideResults();
          return;
        }
        const query = rawQuery
          .replace(/\\bheath\\b/g, 'health')
          .replace(/\\bwi[\\s-]?fi\\b/g, 'wifi');
        const queryTokens = query.split(/\\s+/).filter((token) => token.length > 1);

        const data = await loadSearchData();
        const scored = [];
        for (const item of data) {
          const haystack = String(item.search || '').toLowerCase();
          const phraseMatch = haystack.includes(query);
          const tokenMatch = queryTokens.length > 0 && queryTokens.every((token) => haystack.includes(token));
          if (!phraseMatch && !tokenMatch) {
            continue;
          }
          const title = String(item.title || '').toLowerCase();
          let score = 0;
          if (title.startsWith(query)) {
            score += 10;
          } else if (title.includes(query)) {
            score += 6;
          } else {
            score += 2;
          }
          if (phraseMatch) {
            score += 3;
          }
          if (tokenMatch) {
            score += Math.min(4, queryTokens.length);
          }
          scored.push({ score, item });
        }

        scored.sort((a, b) => b.score - a.score);
        const topMatches = scored.slice(0, 8).map((entry) => entry.item);

        if (topMatches.length === 0) {
          renderNoMatches();
          return;
        }
        renderMatches(topMatches);
      };

      input.addEventListener('input', runSearch);
      input.addEventListener('focus', runSearch);
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Escape') {
          hideResults();
          return;
        }
        if (event.key !== 'Enter') {
          return;
        }
        const firstMatch = results.querySelector('.search-hit');
        if (firstMatch) {
          event.preventDefault();
          window.location.assign(firstMatch.getAttribute('href') || '/');
        }
      });

      results.addEventListener('click', () => {
        hideResults();
      });
      document.addEventListener('click', (event) => {
        if (!box.contains(event.target)) {
          hideResults();
        }
      });
    }

    const cardMediaImages = document.querySelectorAll('.article-card img, .product-card img, .rec-product-card img, .related-article-card img');
    for (const img of cardMediaImages) {
      const collapseCardMedia = () => {
        const figure = img.closest('figure');
        if (figure) {
          figure.classList.add('is-empty');
        }
        const card = img.closest('.article-card, .product-card, .rec-product-card, .related-article-card');
        if (card) {
          card.classList.add('no-media');
        }
      };
      img.addEventListener('error', collapseCardMedia);
      if (img.complete && img.naturalWidth === 0) {
        collapseCardMedia();
      }
    }
  })();
  </script>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(page_title)}</title>
  <meta name="description" content="{html.escape(meta_description)}">
  <meta name="robots" content="{html.escape(robots_directive)}">
  <link rel="canonical" href="{html.escape(canonical)}">
  <meta property="og:locale" content="en_US">
  <meta property="og:type" content="{html.escape(og_type)}">
  <meta property="og:site_name" content="{html.escape(SITE_NAME)}">
  <meta property="og:title" content="{html.escape(page_title)}">
  <meta property="og:description" content="{html.escape(meta_description)}">
  <meta property="og:url" content="{html.escape(canonical)}">
  <meta property="og:image" content="{html.escape(og_image)}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{html.escape(page_title)}">
  <meta name="twitter:description" content="{html.escape(meta_description)}">
  <meta name="twitter:image" content="{html.escape(og_image)}">
  {extra_head_tags}
  <link rel="icon" href="{html.escape(site_icon_url)}" type="image/svg+xml">
  <link rel="stylesheet" href="/assets/styles.css">
  {ld}
</head>
<body class="{html.escape(body_class)}">
  <header class="site-header">
    <div class="shell topbar">
      <a class="brand" href="/" aria-label="Air Purifying Systems home">
        <img src="{html.escape(logo_url)}" alt="Air Purifying Systems logo" loading="eager" decoding="async">
      </a>
      <div class="header-tools">
        <nav class="main-nav" aria-label="Primary">
          <a href="/">Home</a>
          {recommendations_nav_html}
          <a href="/products/">Products</a>
          <a href="/articles/">Articles</a>
        </nav>
        <form class="site-search" role="search" data-site-search onsubmit="return false;">
          <label class="sr-only" for="site-search-input">Search Air Purifying Systems</label>
          <input id="site-search-input" class="rec-search-input" data-site-search-input type="search" placeholder="Search articles, products, pages..." autocomplete="off">
          <div class="search-results" data-site-search-results hidden></div>
        </form>
      </div>
    </div>
  </header>

  <main class="shell">
    {affiliate_notice}
    {main_html}
  </main>

  <footer class="site-footer">
    <div class="shell footer-grid">
      <p>{html.escape(SITE_NAME)}. {html.escape(SITE_TAGLINE)}.</p>
      <p>Affiliate disclosure: As an Amazon Associate, this website may earn from qualifying purchases.</p>
      <nav aria-label="Site information"><a href="/air-purifier-buying-guide/">Buying guide</a> · <a href="/contact/">Contact</a> · <a href="/disclaimer/">Disclosure</a> · <a href="/privacy-policy/">Privacy</a></nav>
      <p><a href="https://hepacertified.com/" rel="noopener">hepacertified.com</a></p>
    </div>
  </footer>
{interactive_script}
</body>
</html>
"""


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def product_detail_path(product: dict[str, Any]) -> str:
    """Only link to actual generated paths, including collision-resolved slugs."""
    url = external_http_url(str(product.get("url", "")))
    title = html_to_text(str(product.get("title", ""))).strip().casefold()
    return PRODUCT_DETAIL_PATHS.get(url, "") or PRODUCT_DETAIL_PATHS.get(title, "")


def product_card_html(product: dict[str, Any]) -> str:
    img = product.get("image", "")
    title = product.get("title", "")
    url = external_http_url(product.get("url", ""))
    if not url:
        return ""
    cta = (
        f'<a class="btn" href="{html.escape(url)}" rel="sponsored nofollow noopener" target="_blank">View on Amazon</a>'
    )
    title_html = f'<a href="{html.escape(url)}" rel="sponsored nofollow noopener" target="_blank">{html.escape(title)}</a>'
    detail_path = product_detail_path(product)
    if detail_path:
        title_html = f'<a href="{html.escape(detail_path)}">{html.escape(title)}</a>'

    image_html = ""
    if img:
        img_tag = f'<img src="{html.escape(img)}" alt="{html.escape(title)}" loading="lazy" decoding="async">'
        image_html = (
            f'<a class="image-link" href="{html.escape(url)}" rel="sponsored nofollow noopener" target="_blank">{img_tag}</a>'
        )
        if detail_path:
            image_html = f'<a class="image-link" href="{html.escape(detail_path)}">{img_tag}</a>'

    return f"""<article class="product-card">
  <figure>{image_html}</figure>
  <h3>{title_html}</h3>
  {cta}
</article>"""


def recommendation_topic_from_slug(slug: str, fallback_title: str) -> str:
    clean = (slug or "").strip("/")
    if clean.startswith("hepa-air-purifiers-for-"):
        return clean.removeprefix("hepa-air-purifiers-for-").replace("-", " ")
    if clean.startswith("hepa-air-purifiers-with-"):
        return clean.removeprefix("hepa-air-purifiers-with-").replace("-", " ")
    if clean.endswith("-hepa-air-purifiers"):
        return clean.removesuffix("-hepa-air-purifiers").replace("-", " ")
    return fallback_title.lower()


def recommendation_terms_from_slug(slug: str) -> set[str]:
    clean = recommendation_topic_from_slug(slug, "").strip().lower()
    if not clean:
        return set()
    stopwords = {
        "for",
        "with",
        "and",
        "the",
        "air",
        "purifier",
        "purifiers",
        "hepa",
    }
    terms = {
        token
        for token in re.split(r"[^a-z0-9]+", clean)
        if token and token not in stopwords and len(token) > 2
    }
    if "wifi" in clean or "wi fi" in clean:
        terms.update({"wifi", "smart"})
    return terms


def recommendation_category_keywords(category: str) -> str:
    lookup = {
        "By Health Need": "health need wellness allergies asthma smoke mold pollen pet dander",
        "By Room And Space": "room space bedroom nursery office kitchen living room basement large room small room",
        "By Feature": "feature true hepa carbon filter wifi smart app quiet low noise sensor energy star uv ionizer",
        "By Type": "type portable quiet whole house",
        "By Use Case": "use case best for specific needs",
    }
    return lookup.get(category, "air purifier recommendations guides")


def recommendation_search_text(title: str, slug: str, category: str = "") -> str:
    clean_slug = (slug or "").strip("/")
    category_label = category or recommendation_category_for_slug(clean_slug)
    topic_text = recommendation_topic_from_slug(clean_slug, title).strip().lower()
    topic_terms = " ".join(sorted(recommendation_terms_from_slug(clean_slug)))
    blob = " ".join(
        part
        for part in [
            title,
            clean_slug.replace("-", " "),
            topic_text,
            category_label.lower(),
            recommendation_category_keywords(category_label),
            topic_terms,
            "air purifier use case health need room and space feature",
        ]
        if part
    )
    return re.sub(r"\s+", " ", blob).strip().lower()


def recommendation_product_blurb(product: dict[str, Any]) -> str:
    description = clip_text(html_to_text(str(product.get("description", "")).strip()), 160)
    if description:
        return description
    title = html_to_text(str(product.get("title", "")))
    parts = [part.strip() for part in title.split(",") if part.strip()]
    if len(parts) > 1:
        return clip_text(". ".join(parts[1:3]), 160)
    return clip_text(title, 160)


def recommendation_product_chips(product: dict[str, Any]) -> list[str]:
    haystack = f"{product.get('title', '')} {product.get('description', '')}".lower()
    chips: list[str] = []
    keywords = [
        ("Wi-Fi", ["wifi", "wi-fi", "wi fi"]),
        ("Smart", ["smart", "app", "alexa"]),
        ("True HEPA", ["true hepa", "h13", "hepa"]),
        ("Large Room", ["large room", "1900", "2000", "3000", "1000", "sq ft", "ft²"]),
        ("Low Noise", ["sleep mode", "quiet", "24db", "22db", "low noise"]),
        ("Pets", ["pet", "dander"]),
        ("Smoke", ["smoke", "wildfire"]),
    ]
    for label, triggers in keywords:
        if any(trigger in haystack for trigger in triggers):
            chips.append(label)
        if len(chips) >= 3:
            break
    return chips


def unique_labels(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def extract_sqft_values(text: str) -> list[int]:
    values: list[int] = []
    pattern = re.compile(
        r"(\d[\d,]{1,5})\s*(?:sq\.?\s*(?:ft|feet)|sqft|ft²|ft2|square\s*feet)",
        flags=re.I,
    )
    for match in pattern.finditer(text or ""):
        token = (match.group(1) or "").replace(",", "")
        if not token.isdigit():
            continue
        number = int(token)
        if 50 <= number <= 10000:
            values.append(number)
    return values


def product_filter_facets(product: dict[str, Any]) -> dict[str, list[str]]:
    haystack = f"{product.get('title', '')} {product.get('description', '')}".lower()

    use_case_rules = [
        ("Allergies", ["allergy", "allergies", "allergen", "pollen", "hay fever"]),
        ("Pets", ["pet", "dander", "pet hair"]),
        ("Smoke & Odor", ["smoke", "wildfire", "odor", "odour", "voc", "cooking"]),
        ("Dust", ["dust"]),
        ("Mold", ["mold", "mould"]),
        ("Asthma", ["asthma"]),
        ("Baby & Nursery", ["baby", "nursery", "infant", "child"]),
        (
            "Filter Replacement",
            [
                "replacement filter",
                "filter replacement",
                "compatible with",
                "rf",
                "flt",
            ],
        ),
        (
            "Commercial / Restoration",
            ["commercial", "industrial", "restoration", "air scrubber", "scrubber", "hospital"],
        ),
    ]
    room_rules = [
        ("Desktop / Car", ["desktop", "car", "portable", "dorm"]),
        ("Bedroom", ["bedroom"]),
        ("Nursery", ["nursery", "baby room"]),
        ("Office", ["office"]),
        ("Whole Home", ["whole house", "whole-home", "entire home"]),
        ("Commercial Space", ["commercial", "industrial", "restoration", "hospital"]),
    ]
    feature_rules = [
        ("True HEPA", ["true hepa", "h13", "hepa"]),
        ("Activated Carbon", ["activated carbon", "carbon filter", "deodorization filter"]),
        ("Smart / App", ["smart", "app control", "app-enabled", "alexa", "google assistant"]),
        ("Wi-Fi", ["wi-fi", "wifi", "wi fi"]),
        ("Low Noise / Sleep", ["sleep mode", "quiet", "low noise", "22db", "24db", "whisper"]),
        ("Air Quality Sensor", ["air quality sensor", "air quality monitoring", "pm2.5", "auto mode"]),
        ("Timer", ["timer", "timing function", "auto-off"]),
        ("Energy Efficient", ["energy star", "low energy", "energy saving", "energy efficient"]),
        ("UV / Ionizer", ["uv", "uv-c", "ionizer", "ionic"]),
        ("Washable Filter", ["washable filter", "reusable filter", "washable pre-filter"]),
    ]

    use_cases = [label for label, triggers in use_case_rules if any(trigger in haystack for trigger in triggers)]
    rooms = [label for label, triggers in room_rules if any(trigger in haystack for trigger in triggers)]
    features = [label for label, triggers in feature_rules if any(trigger in haystack for trigger in triggers)]

    sqft_values = extract_sqft_values(haystack)
    if sqft_values:
        max_sqft = max(sqft_values)
        if max_sqft >= 1200:
            rooms.append("Large Room")
        elif max_sqft >= 500:
            rooms.append("Medium Room")
        else:
            rooms.append("Small Room")
    else:
        if "large room" in haystack:
            rooms.append("Large Room")
        elif "small room" in haystack:
            rooms.append("Small Room")
        elif "medium room" in haystack:
            rooms.append("Medium Room")

    if not use_cases:
        use_cases.append("General Health")

    return {
        "use_cases": unique_labels(use_cases),
        "rooms": unique_labels(rooms),
        "features": unique_labels(features),
    }


def ordered_labels(values: set[str], preferred_order: list[str]) -> list[str]:
    out: list[str] = [label for label in preferred_order if label in values]
    remaining = sorted(label for label in values if label not in out)
    out.extend(remaining)
    return out


def options_html_from_labels(labels: list[str]) -> str:
    return "".join(
        f'<option value="{html.escape(slugify(label, "filter"))}">{html.escape(label)}</option>'
        for label in labels
    )


def recommendation_product_card_html(product: dict[str, Any]) -> str:
    title = str(product.get("title", "")).strip()
    if not title:
        return ""
    url = external_http_url(str(product.get("url", "")).strip())
    if not url:
        return ""
    detail_path = product_detail_path(product)
    title_link = (
        f'<a href="{html.escape(detail_path)}">{html.escape(title)}</a>'
        if detail_path else
        f'<a href="{html.escape(url)}" rel="sponsored nofollow noopener" target="_blank">{html.escape(title)}</a>'
    )
    image_url = str(product.get("image", "")).strip()
    blurb = recommendation_product_blurb(product)
    chips = recommendation_product_chips(product)
    chips_html = ""
    if chips:
        chips_html = '<p class="rec-product-chips">' + "".join(
            f"<span>{html.escape(chip)}</span>" for chip in chips
        ) + "</p>"

    image_html = ""
    if image_url:
        image_html = (
            f'<a class="image-link" href="{html.escape(url)}" rel="sponsored nofollow noopener" target="_blank">'
            f'<img src="{html.escape(image_url)}" alt="{html.escape(title)}" loading="lazy" decoding="async">'
            "</a>"
        )
        if detail_path:
            image_html = (
                f'<a class="image-link" href="{html.escape(detail_path)}">'
                f'<img src="{html.escape(image_url)}" alt="{html.escape(title)}" loading="lazy" decoding="async"></a>'
            )

    return f"""<article class="rec-product-card">
  <figure>{image_html}</figure>
  <div class="rec-product-copy">
    <h3>{title_link}</h3>
    <p class="rec-product-info">{html.escape(blurb)}</p>
    {chips_html}
    <p><a class="btn" href="{html.escape(url)}" rel="sponsored nofollow noopener" target="_blank">View on Amazon</a></p>
  </div>
</article>"""


def render_recommendation_products(
    products: list[dict[str, Any]],
    *,
    heading: str = "Recommended Products",
    intro: str = "Quick details for each pick with direct product links.",
    section_class: str = "recommendation-products",
    index_label: str = "Browse full product index",
    index_href: str = "/products/",
) -> str:
    cards = "\n".join(filter(None, (recommendation_product_card_html(product) for product in products)))
    if not cards.strip():
        return ""
    return f"""
<section class="content-section {html.escape(section_class)}">
  <div class="section-head">
    <h2>{html.escape(heading)}</h2>
    <a class="section-link" href="{html.escape(index_href)}">{html.escape(index_label)}</a>
  </div>
  <p class="section-intro">{html.escape(intro)}</p>
  <div class="rec-product-grid">{cards}</div>
</section>
"""


def select_recommendation_products(
    slug: str,
    cegg_products: list[dict[str, Any]],
    review_entries: dict[str, dict[str, Any]],
    limit: int = 8,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for item in cegg_products:
        url = external_http_url(str(item.get("url", "")).strip())
        title = str(item.get("title", "")).strip()
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append(
            {
                "title": title,
                "url": url,
                "image": str(item.get("image", "")).strip(),
                "description": str(item.get("description", "")).strip(),
            }
        )
        if len(candidates) >= limit:
            return candidates

    terms = recommendation_terms_from_slug(slug)
    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in review_entries.values():
        title = str(entry.get("title", "")).strip()
        url = external_http_url(str(entry.get("url", "")).strip())
        if not title or not url or url in seen_urls:
            continue
        haystack = f"{title} {entry.get('description', '')}".lower()
        score = 0
        for term in terms:
            if term in haystack:
                score += 3
        if score == 0 and terms:
            continue
        scored.append(
            (
                score,
                {
                    "title": title,
                    "url": url,
                    "image": str(entry.get("image", "")).strip(),
                    "description": str(entry.get("description", "")).strip(),
                },
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1]["title"].lower()))
    for _, item in scored:
        seen_urls.add(item["url"])
        candidates.append(item)
        if len(candidates) >= limit:
            break

    if len(candidates) < limit:
        fallback = sorted(
            (
                {
                    "title": str(entry.get("title", "")).strip(),
                    "url": external_http_url(str(entry.get("url", "")).strip()),
                    "image": str(entry.get("image", "")).strip(),
                    "description": str(entry.get("description", "")).strip(),
                }
                for entry in review_entries.values()
            ),
            key=lambda item: item["title"].lower(),
        )
        for item in fallback:
            if not item["title"] or not item["url"] or item["url"] in seen_urls:
                continue
            seen_urls.add(item["url"])
            candidates.append(item)
            if len(candidates) >= limit:
                break

    return candidates[:limit]


def article_card_html(
    *,
    slug: str,
    title: str,
    excerpt: str,
    image_url: str,
    published_date: str,
    minutes_to_read: int,
    featured: bool = False,
) -> str:
    href = f"/{slug.strip('/')}/" if slug else "/articles/"
    classes = ["article-card"]
    if image_url:
        classes.append("has-media")
    if featured:
        classes.append("is-featured")

    media_html = ""
    if image_url:
        media_html = (
            "<figure>"
            f'<a class="image-link" href="{html.escape(href)}">'
            f'<img src="{html.escape(image_url)}" alt="{html.escape(title)}" loading="lazy" decoding="async">'
            "</a>"
            "</figure>"
        )

    meta_bits: list[str] = []
    date_label = display_date(published_date)
    if date_label:
        meta_bits.append(f"<span>{html.escape(date_label)}</span>")
    if minutes_to_read > 0:
        meta_bits.append(f"<span>{minutes_to_read} min read</span>")
    meta_html = f'<p class="article-card-meta">{"".join(meta_bits)}</p>' if meta_bits else ""

    excerpt_html = f'<p class="article-excerpt">{html.escape(excerpt)}</p>' if excerpt else ""

    return f"""<article class="{' '.join(classes)}">
  {media_html}
  <div class="article-copy">
    {meta_html}
    <h3 class="article-title"><a href="{html.escape(href)}">{html.escape(title)}</a></h3>
    {excerpt_html}
    <p class="article-link-row"><a class="article-link" href="{html.escape(href)}">Read article</a></p>
  </div>
</article>"""


def render_cegg_block(products: list[dict[str, Any]]) -> str:
    linked_products = [p for p in products if p.get("url", "").strip()]
    if not linked_products:
        return ""
    cards = "\n".join(filter(None, (product_card_html(p) for p in linked_products)))
    if not cards.strip():
        return ""
    return f"""
<section class="content-section">
  <h2>Related Products</h2>
  <div class="product-grid">{cards}</div>
</section>
"""


def derive_page_content(
    post: WPPost,
    meta: dict[str, list[str]],
    featured_img_url: str,
    cegg_product_index: dict[str, dict[str, str]],
    featured_link: str = "",
) -> tuple[str, list[dict[str, Any]]]:
    cegg_products = extract_cegg_products(meta, cegg_product_index)

    if post.post_content.strip():
        content = rewrite_urls(sanitize_html_content(post.post_content))
        content = ensure_image_alt_attributes(content, fallback_alt=post.post_title)
    else:
        content = ""

    if post.post_type == "post" and featured_img_url and content:
        content, inherited_link = remove_duplicate_featured_image(content, featured_img_url)
        if not featured_link and inherited_link:
            featured_link = inherited_link

    if not featured_link and cegg_products:
        featured_link = str(cegg_products[0].get("url", "")).strip()

    blocks = []
    if featured_img_url:
        image_tag = (
            f'<img src="{html.escape(featured_img_url)}" alt="{html.escape(post.post_title)}" loading="lazy" decoding="async">'
        )
        linked_feature = external_http_url(featured_link)
        if linked_feature:
            image_tag = (
                f'<a class="image-link" href="{html.escape(linked_feature)}" rel="sponsored nofollow noopener" target="_blank">'
                f"{image_tag}</a>"
            )
        blocks.append(
            f'<figure class="featured">{image_tag}</figure>'
        )

    if content:
        blocks.append(f'<section class="content-section prose">{content}</section>')

    if cegg_products:
        blocks.append(render_cegg_block(cegg_products))

    if not blocks:
        blocks.append('<section class="content-section prose"><p>No additional body content was provided for this page.</p></section>')

    return "\n".join(blocks), cegg_products


def first_image_from_content(content: str) -> str:
    if not content.strip():
        return ""
    cleaned = rewrite_urls(sanitize_html_content(content))
    match = re.search(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", cleaned, flags=re.I)
    if not match:
        return ""
    src = match.group(1).strip()
    if src.startswith("//"):
        return f"https:{src}"
    return src


def is_recommendation_page(post: WPPost, route: str) -> bool:
    if post.post_type != "page":
        return False
    slug = (post.post_name or route or "").strip()
    if not slug:
        return False
    if slug.startswith("hepa-air-purifiers-"):
        return True
    return slug in {
        "portable-hepa-air-purifiers",
        "quiet-hepa-air-purifiers",
        "whole-house-hepa-air-purifiers",
    }


def recommendation_category_for_slug(slug: str) -> str:
    clean = (slug or "").strip("/")
    room_space_topics = {
        "baby-rooms",
        "basements",
        "bedrooms",
        "commercial-use",
        "hospitals",
        "kitchens",
        "large-rooms",
        "living-rooms",
        "offices",
        "schools",
        "small-rooms",
    }
    health_topics = {
        "allergies",
        "asthma",
        "dust",
        "mold",
        "pet-dander",
        "pets",
        "pollen",
        "smoke",
    }
    if clean.startswith("hepa-air-purifiers-with-"):
        return "By Feature"
    if clean in {"portable-hepa-air-purifiers", "quiet-hepa-air-purifiers", "whole-house-hepa-air-purifiers"}:
        return "By Type"
    if clean.startswith("hepa-air-purifiers-for-"):
        topic = clean.removeprefix("hepa-air-purifiers-for-")
        if topic in room_space_topics:
            return "By Room And Space"
        if topic in health_topics:
            return "By Health Need"
        return "By Use Case"
    return "More Guides"


def tokenize_terms(text: str) -> set[str]:
    stopwords = {
        "air",
        "purifier",
        "purifiers",
        "hepa",
        "with",
        "for",
        "from",
        "this",
        "that",
        "your",
        "into",
        "about",
        "guide",
        "review",
        "best",
        "home",
        "room",
        "rooms",
        "the",
        "and",
        "are",
        "what",
        "when",
        "how",
        "why",
        "can",
        "you",
        "our",
        "all",
    }
    return {
        token
        for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) >= 3 and token not in stopwords
    }


def match_score(terms: set[str], haystack: str) -> int:
    if not terms:
        return 0
    score = 0
    lower_haystack = (haystack or "").lower()
    for token in terms:
        if token in lower_haystack:
            score += 1
    return score


def select_related_products_for_article(
    article_title: str,
    article_excerpt: str,
    review_entries: dict[str, dict[str, Any]],
    cegg_products: list[dict[str, Any]],
    limit: int = 6,
) -> list[dict[str, Any]]:
    related: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    terms = tokenize_terms(f"{article_title} {article_excerpt}")

    for item in cegg_products:
        url = external_http_url(str(item.get("url", "")))
        title = str(item.get("title", "")).strip()
        if not url or not title or url in seen_urls:
            continue
        seen_urls.add(url)
        related.append(
            {
                "title": title,
                "url": url,
                "image": str(item.get("image", "")).strip(),
                "description": str(item.get("description", "")).strip(),
            }
        )
        if len(related) >= limit:
            return related

    scored: list[tuple[int, dict[str, Any]]] = []
    for entry in review_entries.values():
        title = str(entry.get("title", "")).strip()
        url = external_http_url(str(entry.get("url", "")))
        if not title or not url or url in seen_urls:
            continue
        description = str(entry.get("description", "")).strip()
        score = match_score(terms, f"{title} {description}")
        if score <= 0 and terms:
            continue
        scored.append(
            (
                score,
                {
                    "title": title,
                    "url": url,
                    "image": str(entry.get("image", "")).strip(),
                    "description": description,
                },
            )
        )

    scored.sort(key=lambda item: (-item[0], item[1]["title"].lower()))
    for _, item in scored:
        if item["url"] in seen_urls:
            continue
        seen_urls.add(item["url"])
        related.append(item)
        if len(related) >= limit:
            break

    return related[:limit]


def select_related_articles(
    focus_text: str,
    article_entries: list[dict[str, Any]],
    *,
    current_slug: str = "",
    limit: int = 4,
) -> list[dict[str, Any]]:
    terms = tokenize_terms(focus_text)
    scored: list[tuple[int, dict[str, Any]]] = []
    current = current_slug.strip("/")
    for entry in article_entries:
        slug = str(entry.get("slug", "")).strip("/")
        title = str(entry.get("title", "")).strip()
        if not slug or not title or slug == current:
            continue
        excerpt = str(entry.get("excerpt", "")).strip()
        score = match_score(terms, f"{title} {excerpt}")
        if score <= 0 and terms:
            continue
        scored.append((score, entry))

    scored.sort(key=lambda item: (-item[0], str(item[1].get("title", "")).lower()))
    if not scored:
        fallback = [entry for entry in reversed(article_entries) if str(entry.get("slug", "")).strip("/") != current]
        return fallback[:limit]
    return [entry for _, entry in scored[:limit]]


def select_related_recommendations(
    focus_text: str,
    recommendation_entries: list[dict[str, str]],
    *,
    current_href: str = "",
    limit: int = 6,
) -> list[dict[str, str]]:
    terms = tokenize_terms(focus_text)
    scored: list[tuple[int, dict[str, str]]] = []
    current = current_href.rstrip("/")
    for entry in recommendation_entries:
        href = str(entry.get("href", "")).rstrip("/")
        title = str(entry.get("title", "")).strip()
        if not href or not title or href == current:
            continue
        haystack = f"{title} {entry.get('search_text', '')}"
        score = match_score(terms, haystack)
        if score <= 0 and terms:
            continue
        scored.append((score, entry))

    scored.sort(key=lambda item: (-item[0], item[1].get("title", "").lower()))
    if not scored:
        return recommendation_entries[:limit]
    return [entry for _, entry in scored[:limit]]


def related_article_card_html(entry: dict[str, Any]) -> str:
    slug = str(entry.get("slug", "")).strip("/")
    title = str(entry.get("title", "")).strip()
    if not slug or not title:
        return ""
    href = f"/{slug}/"
    image = str(entry.get("image", "")).strip()
    excerpt = clip_text(str(entry.get("excerpt", "")).strip(), 150)
    date_label = display_date(str(entry.get("date", "")).strip())
    meta = date_label or "Article"
    image_html = ""
    if image:
        image_html = (
            f'<a class="image-link" href="{html.escape(href)}">'
            f'<img src="{html.escape(image)}" alt="{html.escape(title)}" loading="lazy" decoding="async">'
            "</a>"
        )
    return f"""<article class="related-article-card">
  <figure>{image_html}</figure>
  <div class="related-article-copy">
    <p class="related-article-meta">{html.escape(meta)}</p>
    <h3><a href="{html.escape(href)}">{html.escape(title)}</a></h3>
    <p>{html.escape(excerpt)}</p>
  </div>
</article>"""


def render_related_articles_block(
    entries: list[dict[str, Any]],
    *,
    heading: str = "Related Articles",
    intro: str = "Continue with more practical air purifier guides.",
) -> str:
    cards = "\n".join(filter(None, (related_article_card_html(entry) for entry in entries)))
    if not cards.strip():
        return ""
    return f"""
<section class="content-section related-articles">
  <div class="section-head">
    <h2>{html.escape(heading)}</h2>
    <a class="section-link" href="/articles/">Browse all articles</a>
  </div>
  <p class="section-intro">{html.escape(intro)}</p>
  <div class="related-article-grid">{cards}</div>
</section>
"""


def render_related_recommendations_block(
    entries: list[dict[str, str]],
    *,
    heading: str = "Related Use Cases",
) -> str:
    links = "".join(
        f'<a href="{html.escape(item["href"])}">{html.escape(item["title"])}</a>'
        for item in entries
        if item.get("href") and item.get("title")
    )
    if not links:
        return ""
    return f"""
<section class="content-section related-use-cases">
  <div class="section-head">
    <h2>{html.escape(heading)}</h2>
    <a class="section-link" href="/recommendations/">All use cases</a>
  </div>
  <div class="use-case-chips">{links}</div>
</section>
"""


def build_breadcrumb_items(items: list[tuple[str, str]]) -> list[dict[str, str]]:
    clean: list[dict[str, str]] = []
    for title, href in items:
        label = html_to_text(title or "").strip()
        url = absolute_url(href or "/")
        if not label or not url:
            continue
        clean.append({"name": label, "url": url})
    return clean


def render_breadcrumbs(items: list[dict[str, str]]) -> str:
    if len(items) < 2:
        return ""
    links: list[str] = []
    for idx, item in enumerate(items):
        if idx == len(items) - 1:
            links.append(f'<span aria-current="page">{html.escape(item["name"])}</span>')
        else:
            links.append(f'<a href="{html.escape(item["url"])}">{html.escape(item["name"])}</a>')
    return '<nav class="breadcrumbs" aria-label="Breadcrumb">' + "<span class=\"crumb-sep\">/</span>".join(links) + "</nav>"


def breadcrumb_schema(items: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {
                "@type": "ListItem",
                "position": idx + 1,
                "name": item["name"],
                "item": item["url"],
            }
            for idx, item in enumerate(items)
        ],
    }


def main() -> None:
    data = load_wp_data(SQL_PATH)
    options = data["options"]
    posts: dict[int, WPPost] = data["posts"]
    postmeta: dict[int, dict[str, list[str]]] = data["postmeta"]
    cegg_product_index: dict[str, dict[str, str]] = data["cegg_product_index"]

    taxonomy_lookup = build_taxonomy_lookup(data)

    page_on_front = int(options.get("page_on_front") or 0)
    blogname = options.get("blogname", SITE_NAME).strip() or SITE_NAME
    blogdesc = options.get("blogdescription", SITE_TAGLINE).strip() or SITE_TAGLINE

    # Build attachment path map
    attachment_rel_paths: dict[int, str] = {}
    for pid, post in posts.items():
        if post.post_type != "attachment":
            continue
        attachment_rel_paths[pid] = extract_attachment_path(post, postmeta.get(pid, {}))

    def attachment_url(attachment_id: int) -> str:
        rel = attachment_rel_paths.get(attachment_id, "")
        if not rel:
            return ""
        return f"/assets/uploads/{rel}"

    site_logo_id = int(options.get("site_logo") or 0)
    site_icon_id = int(options.get("site_icon") or 0)

    # Fallback from theme mods if needed.
    if not site_logo_id:
        theme_mods_raw = options.get("theme_mods_hello-elementor", "")
        if theme_mods_raw:
            try:
                mods = parse_php_serialized(theme_mods_raw)
                if isinstance(mods, dict) and isinstance(mods.get("custom_logo"), int):
                    site_logo_id = int(mods["custom_logo"])
            except Exception:
                pass

    logo_url = attachment_url(site_logo_id) or "/assets/uploads/2024/01/airpurifying-systems-logo.svg"
    site_icon_url = attachment_url(site_icon_id) or "/assets/uploads/2024/01/airpurifying-fav-icon.svg"

    # Overwrite generated files without deleting source, tests, or unrelated work.
    ensure_dir(ASSETS_DIR)
    if UPLOADS_PATH.resolve() != UPLOADS_OUT.resolve():
        shutil.copytree(UPLOADS_PATH, UPLOADS_OUT, dirs_exist_ok=True)

    # Collect published items
    published = [
        p
        for p in posts.values()
        if p.post_status == "publish" and p.post_type in {"page", "post", "product"}
    ]

    # Stable sort by date then id
    published.sort(key=lambda p: (p.post_date, p.post_id))
    site_lastmod = ""
    for post in published:
        token = iso_date_token(post.post_modified or post.post_date)
        if token and token > site_lastmod:
            site_lastmod = token

    paths_by_id: dict[int, str] = {}
    used_paths: set[str] = set()

    for post in published:
        base_slug = post.post_name.strip() or f"{slugify(post.post_title, 'page')}-{post.post_id}"
        route = "" if post.post_id == page_on_front else base_slug

        if route in used_paths:
            route = f"{base_slug}-{post.post_id}"
        used_paths.add(route)
        paths_by_id[post.post_id] = route

    recommendation_entries: list[dict[str, str]] = []
    for post in published:
        route = paths_by_id.get(post.post_id, "")
        if not is_recommendation_page(post, route):
            continue
        href = "/" if not route else f"/{route.strip('/')}/"
        title = (post.post_title or route.replace("-", " ").title()).strip()
        slug = route.strip("/")
        category = recommendation_category_for_slug(slug)
        search_text = recommendation_search_text(title, slug, category)
        recommendation_entries.append(
            {
                "title": title,
                "href": href,
                "slug": slug,
                "category": category,
                "search_text": search_text,
            }
        )

    recommendation_entries.sort(key=lambda x: x["title"].lower())
    recommendation_groups_map: dict[str, list[dict[str, str]]] = {}
    for entry in recommendation_entries:
        recommendation_groups_map.setdefault(entry["category"], []).append(entry)

    category_order = [
        "By Health Need",
        "By Room And Space",
        "By Feature",
        "By Type",
        "By Use Case",
        "More Guides",
    ]
    recommendation_grouped_entries: list[tuple[str, list[dict[str, str]]]] = []
    for category in category_order:
        items = recommendation_groups_map.pop(category, [])
        if items:
            recommendation_grouped_entries.append((category, items))
    for category in sorted(recommendation_groups_map):
        recommendation_grouped_entries.append((category, recommendation_groups_map[category]))

    recommendation_dropdown_html = ""
    if recommendation_grouped_entries:
        dropdown_groups_html = "".join(
            (
                f"""<section class="mega-group" data-rec-group>
  <h3>{html.escape(category)}</h3>
  <div class="mega-links">"""
                + "".join(
                    f'<a href="{html.escape(item["href"])}" data-rec-item data-rec-text="{html.escape(item["search_text"])}">{html.escape(item["title"])}</a>'
                    for item in items
                )
                + "</div></section>"
            )
            for category, items in recommendation_grouped_entries
        )
        recommendation_dropdown_html = f"""
<div class="nav-dropdown" data-behavior="recommendations">
  <a href="/recommendations/" aria-haspopup="true" aria-expanded="false">Recommendations</a>
  <div class="mega-menu" aria-label="Recommendations menu" data-rec-filter-root>
    <div class="mega-toolbar">
      <p class="mega-heading">Find By Use Case</p>
      <input type="search" class="rec-search-input" data-rec-search placeholder="Search by room, allergy, pet, Wi-Fi..." autocomplete="off">
    </div>
    <p class="mega-empty" data-rec-empty hidden>No matching recommendation found.</p>
    <div class="mega-groups">{dropdown_groups_html}</div>
  </div>
</div>
"""

    search_index_entries: list[dict[str, str]] = []
    search_index_seen: set[str] = set()

    def local_search_url(url: str) -> str:
        value = (url or "").strip()
        if not value:
            return ""
        if value.startswith(DOMAIN):
            value = value[len(DOMAIN) :]
        if re.match(r"^https?://", value, flags=re.I):
            return ""
        if not value:
            return "/"
        if not value.startswith("/"):
            value = "/" + value.lstrip("/")
        return value

    def add_search_entry(title: str, url: str, item_type: str, extra_text: str = "") -> None:
        clean_title = html_to_text(title or "").strip()
        clean_url = local_search_url(url)
        if not clean_title or not clean_url:
            return
        unique_key = clean_url.rstrip("/") or "/"
        if unique_key in search_index_seen:
            return
        search_index_seen.add(unique_key)
        search_blob = re.sub(
            r"\s+",
            " ",
            f"{clean_title} {item_type} {extra_text or ''}".strip().lower(),
        )
        search_index_entries.append(
            {
                "title": clean_title,
                "url": clean_url,
                "type": item_type,
                "search": search_blob,
            }
        )

    sitemap_entries: list[str] = []
    sitemap_lastmod: dict[str, str] = {}
    images_by_page: dict[str, set[str]] = {}
    articles_index_cards: list[str] = []
    article_index_entries: list[dict[str, Any]] = []
    article_card_images: list[str] = []
    product_card_images: list[str] = []
    product_redirects: dict[str, str] = {}

    def add_sitemap_url(url: str, modified: str = "") -> None:
        if not url:
            return
        sitemap_entries.append(url)
        token = iso_date_token(modified)
        if not token:
            return
        prev = sitemap_lastmod.get(url, "")
        if token > prev:
            sitemap_lastmod[url] = token

    def add_page_image(page_url: str, image_url: str) -> None:
        img = absolute_url(image_url)
        if not img:
            return
        images_by_page.setdefault(page_url, set()).add(img)

    def register_product_redirect(source_path: str, destination_url: str) -> None:
        # Legacy product aliases consolidate onto an on-site product page.
        target = destination_url
        if not target.startswith("/products/") or target.startswith("//"):
            raise ValueError("Product redirects must stay on the site")
        clean = source_path.strip("/")
        if not clean:
            return
        source_with_slash = f"/{clean}/"
        source_without_slash = f"/{clean}"
        product_redirects[source_with_slash] = target
        product_redirects[source_without_slash] = target

    review_entries: dict[str, dict[str, Any]] = {}

    # Pre-index Woo products for dedicated product pages.
    woo_products = [p for p in published if p.post_type == "product"]
    for post in woo_products:
        meta = postmeta.get(post.post_id, {})
        sku = get_first(meta, "_sku")
        purl = get_first(meta, "_product_url") or get_first(meta, "_ei_product_url")
        thumb_id = int(get_first(meta, "_thumbnail_id", "0") or 0)
        image = attachment_url(thumb_id)
        key = sku or post.post_name or str(post.post_id)

        review_entries[key] = {
            "title": post.post_title,
            "slug": post.post_name or slugify(post.post_title, f"product-{post.post_id}"),
            "source": "woo",
            "url": purl,
            "image": image,
            "description": post.post_content,
            "sku": sku,
            "post_id": post.post_id,
        }

    # Add CEGG products discovered in metadata.
    for post in published:
        meta = postmeta.get(post.post_id, {})
        for item in extract_cegg_products(meta, cegg_product_index):
            key = item.get("unique_id") or item.get("url") or item.get("title")
            if not key:
                continue
            if key in review_entries:
                continue

            title = item.get("title", "").strip()
            if not title:
                continue

            review_entries[key] = {
                "title": title,
                "slug": slugify(title, "product"),
                "source": "cegg",
                "url": item.get("url", ""),
                "image": item.get("image", ""),
                "description": item.get("description", ""),
                "sku": item.get("unique_id", ""),
                "post_id": post.post_id,
            }

    # Deduplicate review slugs.
    used_review_slugs: set[str] = set()
    for item in review_entries.values():
        slug = item["slug"]
        base = slug
        i = 2
        while slug in used_review_slugs:
            slug = f"{base}-{i}"
            i += 1
        item["slug"] = slug
        used_review_slugs.add(slug)

    review_slug_by_post_id: dict[int, str] = {}
    PRODUCT_DETAIL_PATHS.clear()
    for item in review_entries.values():
        detail_path = f"/products/{item['slug']}/"
        url = external_http_url(str(item.get("url", "")))
        if url:
            PRODUCT_DETAIL_PATHS.setdefault(url, detail_path)
        PRODUCT_DETAIL_PATHS.setdefault(html_to_text(item["title"]).strip().casefold(), detail_path)
        post_id = int(item.get("post_id") or 0)
        if item.get("source") == "woo" and post_id:
            review_slug_by_post_id[post_id] = item["slug"]

    article_entry_by_post_id: dict[int, dict[str, Any]] = {}
    all_article_entries: list[dict[str, Any]] = []
    for post in published:
        if post.post_type != "post":
            continue
        meta = postmeta.get(post.post_id, {})
        thumb_id = int(get_first(meta, "_thumbnail_id", "0") or 0)
        article_image = attachment_url(thumb_id) or first_image_from_content(post.post_content)
        article_excerpt = post_summary_text(post, article_image, 190)
        article_entry = {
            "slug": post.post_name,
            "title": post.post_title,
            "excerpt": article_excerpt,
            "image": article_image,
            "date": post.post_date[:10],
            "minutes": reading_time_minutes(post.post_content),
            "post_id": post.post_id,
        }
        article_entry_by_post_id[post.post_id] = article_entry
        all_article_entries.append(article_entry)

    # Generate all original permalink pages.
    for post in published:
        post_route = paths_by_id[post.post_id]
        route = post_route
        meta = postmeta.get(post.post_id, {})
        is_contact_page = post.post_type == "page" and (post_route == "contact" or post.post_name == "contact")
        is_recommendation = is_recommendation_page(post, post_route)

        rank_title = get_first(meta, "rank_math_title")
        rank_desc = get_first(meta, "rank_math_description")
        display_title = rank_title or post.post_title

        thumb_id = int(get_first(meta, "_thumbnail_id", "0") or 0)
        featured_img_url = attachment_url(thumb_id)
        og_image = featured_img_url or logo_url
        buy_url = ""
        if post.post_type == "product":
            buy_url = external_http_url(get_first(meta, "_product_url") or get_first(meta, "_ei_product_url"))

        content_html, cegg_products = derive_page_content(
            post,
            meta,
            featured_img_url,
            cegg_product_index,
            featured_link=buy_url,
        )
        recommendation_page_products: list[dict[str, Any]] = []

        if is_contact_page:
            og_image = logo_url
            content_html = f"""
<section class="content-section prose">
  <p><a href="mailto:{html.escape(CONTACT_EMAIL)}">{html.escape(CONTACT_EMAIL)}</a></p>
</section>
"""

        if post.post_type == "page" and route == "blog":
            content_html = """
<section class="content-section prose">
  <p>The article archive is now organized in one streamlined index.</p>
  <p><a class="btn" href="/articles/">Open Articles Archive</a></p>
</section>
"""

        if post.post_type == "product":
            cta_html = (
                f'<p><a class="btn btn-large" href="{html.escape(buy_url)}" rel="sponsored nofollow noopener" target="_blank">Shop on Amazon</a></p>'
                if buy_url
                else ""
            )

            product_slug = review_slug_by_post_id.get(post.post_id, post.post_name or slugify(post.post_title))
            product_url = f"/products/{product_slug}/"

            content_html = (
                f"<section class=\"content-section\">{cta_html}<p><a href=\"{product_url}\">Open product page</a></p></section>"
                + content_html
            )
            content_html = link_unlinked_images(content_html, buy_url)
            if post_route:
                register_product_redirect(post_route, product_url)
            if post.post_name:
                register_product_redirect(f"product/{post.post_name}", product_url)

        article_image = ""
        article_read_minutes = 0
        article_excerpt = ""
        if post.post_type == "post":
            article_entry = article_entry_by_post_id.get(post.post_id, {})
            article_image = str(article_entry.get("image", ""))
            article_excerpt = str(article_entry.get("excerpt", ""))
            article_read_minutes = int(article_entry.get("minutes", 0) or 0)
            if article_image:
                article_card_images.append(article_image)

        tax_bits: list[str] = []
        post_tax = taxonomy_lookup.get(post.post_id, {})
        if post_tax.get("category"):
            tax_bits.append("Categories: " + ", ".join(html.escape(x) for x in post_tax["category"]))
        if post_tax.get("post_tag"):
            tax_bits.append("Tags: " + ", ".join(html.escape(x) for x in post_tax["post_tag"]))
        if post_tax.get("product_cat"):
            tax_bits.append("Product categories: " + ", ".join(html.escape(x) for x in post_tax["product_cat"]))

        tax_html = ""
        published_meta = ""
        article_intro = ""
        article_meta_line = ""
        if post.post_type != "product" and not is_contact_page:
            if post.post_type == "post":
                intro_text = post_summary_text(post, featured_img_url or article_image, 260)
                if intro_text:
                    article_intro = f'<p class="article-intro">{html.escape(intro_text)}</p>'
                date_label = display_date(post.post_date)
                article_meta_parts = []
                if date_label:
                    article_meta_parts.append(f"<span>{html.escape(date_label)}</span>")
                if article_read_minutes <= 0:
                    article_read_minutes = reading_time_minutes(post.post_content)
                article_meta_parts.append(f"<span>{article_read_minutes} min read</span>")
                article_meta_line = f'<p class="article-meta-line">{"".join(article_meta_parts)}</p>'
                if tax_bits:
                    tax_html = "<p class=\"article-tax\">" + " | ".join(tax_bits) + "</p>"
            else:
                published_label = display_date(post.post_date) or post.post_date[:10]
                published_meta = f'<p class="meta">Published: {html.escape(published_label)}</p>'
                if tax_bits:
                    tax_html = "<p class=\"meta\">" + " | ".join(tax_bits) + "</p>"

        breadcrumbs_items = build_breadcrumb_items(
            [
                ("Home", "/"),
                (post.post_title, "/" if not route else f"/{route.strip('/')}/"),
            ]
        )

        if post.post_type == "post":
            breadcrumbs_items = build_breadcrumb_items(
                [
                    ("Home", "/"),
                    ("Articles", "/articles/"),
                    (post.post_title, f"/{route.strip('/')}/"),
                ]
            )
            article_related_products = select_related_products_for_article(
                post.post_title,
                article_excerpt or post.post_excerpt,
                review_entries,
                cegg_products,
                limit=6,
            )
            article_related_products_html = render_recommendation_products(
                article_related_products,
                heading="Products To Consider",
                intro="Shortlisted products related to this topic, each with direct Amazon links.",
                section_class="article-related-products",
                index_label="All product pages",
                index_href="/products/",
            )
            related_use_cases = select_related_recommendations(
                f"{post.post_title} {article_excerpt}",
                recommendation_entries,
                limit=6,
            )
            related_use_cases_html = render_related_recommendations_block(
                related_use_cases,
                heading="Relevant Use Cases",
            )

            article_actions_html = """
<section class="content-section article-actions">
  <p class="article-actions-label">Keep exploring</p>
  <p class="article-actions-links"><a class="btn" href="/articles/">Browse all articles</a> <a class="btn ghost" href="/products/">Explore products</a></p>
</section>
"""
            page_article_classes = ["page-article", "article-page", "has-featured" if featured_img_url else "no-featured"]
            main_html = f"""
<article class="{' '.join(page_article_classes)}">
  {render_breadcrumbs(breadcrumbs_items)}
  <header class="content-header article-header">
    <p class="eyebrow">Article</p>
    <h1>{html.escape(post.post_title)}</h1>
    {article_intro}
    {article_meta_line}
    {tax_html}
  </header>
  {content_html}
  {article_related_products_html}
  {related_use_cases_html}
  {article_actions_html}
</article>
"""
        elif is_recommendation and post.post_type == "page":
            breadcrumbs_items = build_breadcrumb_items(
                [
                    ("Home", "/"),
                    ("Recommendations", "/recommendations/"),
                    (post.post_title, f"/{route.strip('/')}/"),
                ]
            )
            rec_slug = post_route.strip("/") or post.post_name
            rec_topic = recommendation_topic_from_slug(rec_slug, post.post_title)
            rec_topic_label = rec_topic.replace("wi fi", "Wi-Fi").replace("wifi", "Wi-Fi").title()
            recommendation_page_products = select_recommendation_products(
                rec_slug,
                cegg_products,
                review_entries,
                limit=8,
            )
            recommendation_intro = (
                f"Use-case focused picks for {rec_topic_label}. Each product includes quick feature notes and a direct Amazon link."
            )
            content_without_related = re.sub(
                r"<section class=\"content-section\">\s*<h2>Related Products</h2>[\s\S]*?</section>",
                "",
                content_html,
                flags=re.I,
            ).strip()
            recommendation_products_html = render_recommendation_products(
                recommendation_page_products,
                heading="Recommended Products",
                intro="Product picks matched to this use case, with direct Amazon links.",
                section_class="recommendation-products",
            )
            recommendation_related_articles = select_related_articles(
                f"{post.post_title} {rec_topic_label}",
                all_article_entries,
                limit=4,
            )
            recommendation_related_articles_html = render_related_articles_block(
                recommendation_related_articles,
                heading="Related Articles",
                intro="Learn practical considerations before choosing your model.",
            )
            recommendation_related_use_cases = select_related_recommendations(
                post.post_title,
                recommendation_entries,
                current_href=f"/{route.strip('/')}/",
                limit=8,
            )
            recommendation_related_use_cases_html = render_related_recommendations_block(
                recommendation_related_use_cases,
                heading="More Recommendation Pages",
            )
            main_html = f"""
<article class="page-article recommendation-page">
  {render_breadcrumbs(breadcrumbs_items)}
  <header class="content-header recommendation-header">
    <p class="eyebrow">Recommendation</p>
    <h1>{html.escape(post.post_title)}</h1>
    {published_meta}
    <p class="recommendation-intro">{html.escape(clip_text(recommendation_intro, 220))}</p>
    {tax_html}
    <p class="recommendation-links"><a class="section-link" href="/recommendations/">All recommendation categories</a> <a class="section-link" href="/products/">All products</a></p>
  </header>
  {content_without_related}
  {recommendation_products_html}
  {recommendation_related_articles_html}
  {recommendation_related_use_cases_html}
</article>
"""
        else:
            if post.post_type == "product":
                breadcrumbs_items = build_breadcrumb_items(
                    [
                        ("Home", "/"),
                        ("Products", "/products/"),
                        (post.post_title, f"/{route.strip('/')}/"),
                    ]
                )
            elif route == "articles":
                breadcrumbs_items = build_breadcrumb_items(
                    [
                        ("Home", "/"),
                        ("Articles", "/articles/"),
                    ]
                )
            elif route == "products":
                breadcrumbs_items = build_breadcrumb_items(
                    [
                        ("Home", "/"),
                        ("Products", "/products/"),
                    ]
                )
            elif route == "recommendations":
                breadcrumbs_items = build_breadcrumb_items(
                    [
                        ("Home", "/"),
                        ("Recommendations", "/recommendations/"),
                    ]
                )
            main_html = f"""
<article class="page-article">
  {render_breadcrumbs(breadcrumbs_items)}
  <header class="content-header">
    <p class="eyebrow">{html.escape(post.post_type.title())}</p>
    <h1>{html.escape(post.post_title)}</h1>
    {published_meta}
    {tax_html}
  </header>
  {content_html}
</article>
"""

        if is_contact_page:
            meta_description = normalize_meta_description(
                f"Contact {SITE_NAME} via email.",
                fallback=f"Contact {SITE_NAME}.",
            )
        else:
            meta_description = rank_desc or post.post_excerpt or excerpt_from_content(post.post_content)
            if not meta_description and cegg_products:
                meta_description = excerpt_from_content(" ".join(p.get("title", "") for p in cegg_products))
            if not meta_description and recommendation_page_products:
                meta_description = excerpt_from_content(
                    " ".join(str(p.get("title", "")) for p in recommendation_page_products)
                )
            if not meta_description:
                meta_description = blogdesc
            meta_description = normalize_meta_description(
                meta_description,
                fallback=f"{post.post_title} | {blogdesc}",
            )

        canonical = build_canonical(route)
        if post.post_type == "product":
            canonical = build_canonical(f"products/{review_slug_by_post_id[post.post_id]}")
        add_page_image(canonical, featured_img_url)
        add_page_image(canonical, article_image)
        for cegg_item in cegg_products:
            add_page_image(canonical, str(cegg_item.get("image", "")))
        for recommendation_item in recommendation_page_products:
            add_page_image(canonical, str(recommendation_item.get("image", "")))

        json_ld: list[dict[str, Any]] = [
            {
                "@context": "https://schema.org",
                "@type": "Organization",
                "name": SITE_NAME,
                "url": DOMAIN,
                "logo": DOMAIN + logo_url,
                "sameAs": ["https://hepacertified.com/"],
            }
        ]
        if len(breadcrumbs_items) >= 2:
            json_ld.append(breadcrumb_schema(breadcrumbs_items))

        webpage_schema = {
            "@context": "https://schema.org",
            "@type": "WebPage",
            "name": post.post_title,
            "url": canonical,
            "description": meta_description,
        }

        og_type = "website"
        extra_head_tags = ""
        robots_directive = "index,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"
        published_iso = iso_date_token(post.post_date)
        modified_iso = iso_date_token(post.post_modified or post.post_date)

        if post.post_type == "post":
            og_type = "article"
            meta_bits: list[str] = []
            if published_iso:
                meta_bits.append(f'<meta property="article:published_time" content="{published_iso}">')
            if modified_iso:
                meta_bits.append(f'<meta property="article:modified_time" content="{modified_iso}">')
            extra_head_tags = "\n  ".join(meta_bits)
            article_schema: dict[str, Any] = {
                "@context": "https://schema.org",
                "@type": "Article",
                "headline": post.post_title,
                "mainEntityOfPage": canonical,
                "url": canonical,
                "description": meta_description,
                "author": {"@type": "Organization", "name": SITE_NAME},
                "publisher": {
                    "@type": "Organization",
                    "name": SITE_NAME,
                    "logo": {"@type": "ImageObject", "url": DOMAIN + logo_url},
                },
            }
            if article_image:
                article_schema["image"] = [absolute_url(article_image)]
            if published_iso:
                article_schema["datePublished"] = published_iso
            if modified_iso:
                article_schema["dateModified"] = modified_iso
            if taxonomy_lookup.get(post.post_id, {}).get("category"):
                article_schema["articleSection"] = taxonomy_lookup[post.post_id]["category"][0]
            json_ld.append(article_schema)
        elif post.post_type == "product":
            og_type = "product"
            product_schema: dict[str, Any] = {
                "@context": "https://schema.org",
                "@type": "Product",
                "name": post.post_title,
                "url": canonical,
                "description": meta_description,
                "image": [absolute_url(featured_img_url)] if featured_img_url else [DOMAIN + logo_url],
            }
            json_ld.append(product_schema)
        elif is_recommendation and recommendation_page_products:
            recommendation_schema = {
                "@context": "https://schema.org",
                "@type": "CollectionPage",
                "name": post.post_title,
                "url": canonical,
                "description": meta_description,
                "mainEntity": {
                    "@type": "ItemList",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": idx + 1,
                            "name": str(item.get("title", "")).strip(),
                            "url": absolute_url(product_detail_path(item)) or external_http_url(str(item.get("url", "")).strip()) or canonical,
                        }
                        for idx, item in enumerate(recommendation_page_products)
                        if str(item.get("title", "")).strip()
                    ],
                },
            }
            json_ld.append(recommendation_schema)
        else:
            json_ld.append(webpage_schema)

        if post.post_type == "page" and route == "blog":
            robots_directive = "noindex,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1"

        page_title = f"{display_title} | {blogname}" if display_title else blogname

        out_path = OUT_ROOT / ("index.html" if route == "" else f"{route}/index.html")
        write_text(
            out_path,
            layout_html(
                page_title=page_title,
                meta_description=meta_description,
                canonical=canonical,
                body_class=f"type-{post.post_type}",
                main_html=main_html,
                og_image=(DOMAIN + og_image) if og_image.startswith("/") else og_image,
                json_ld=json_ld,
                logo_url=logo_url,
                site_icon_url=site_icon_url,
                recommendations_nav_html=recommendation_dropdown_html,
                og_type=og_type,
                extra_head_tags=extra_head_tags,
                robots_directive=robots_directive,
            ),
        )
        exclude_from_index = post.post_type == "product" or (post.post_type == "page" and route == "blog")
        if not exclude_from_index:
            add_sitemap_url(canonical, post.post_modified or post.post_date)
        search_kind = "Page"
        if post.post_type == "post":
            search_kind = "Article"
        elif post.post_type == "product":
            search_kind = "Product"
        if not exclude_from_index:
            search_extra_text = f"{meta_description} {post.post_name}"
            if is_recommendation and post.post_type == "page":
                rec_slug_for_search = post_route.strip("/") or post.post_name
                rec_category = recommendation_category_for_slug(rec_slug_for_search)
                search_extra_text = (
                    f"{search_extra_text} {rec_category} "
                    f"{recommendation_search_text(post.post_title, rec_slug_for_search, rec_category)}"
                )
            add_search_entry(
                title=post.post_title or display_title or "Untitled",
                url=canonical,
                item_type=search_kind,
                extra_text=search_extra_text,
            )

        if post.post_type == "product" and post.post_name:
            product_alias = OUT_ROOT / "product" / post.post_name / "index.html"
            write_text(product_alias, out_path.read_text(encoding="utf-8"))

        # Keep /home/ page in place and map front page to root too.
        if post.post_id == page_on_front and post.post_name and post.post_name != "home":
            home_alias = OUT_ROOT / "home/index.html"
            write_text(home_alias, out_path.read_text(encoding="utf-8"))

    # Build dedicated product pages for each discovered product.
    product_cards: list[str] = []
    product_index_entries: list[dict[str, str]] = []
    product_use_case_labels: set[str] = set()
    product_room_labels: set[str] = set()
    product_feature_labels: set[str] = set()
    for entry in sorted(review_entries.values(), key=lambda x: x["title"].lower()):
        slug = entry["slug"]
        product_path = f"products/{slug}"
        canonical = build_canonical(product_path)
        amazon_url = external_http_url(entry.get("url", ""))

        image_html = ""
        image_url = entry.get("image") or ""
        if image_url:
            image_tag = f'<img src="{html.escape(image_url)}" alt="{html.escape(entry["title"])}" loading="lazy" decoding="async">'
            if amazon_url:
                image_tag = (
                    f'<a class="image-link" href="{html.escape(amazon_url)}" rel="sponsored nofollow noopener" target="_blank">{image_tag}</a>'
                )
            image_html = f"<figure class=\"featured\">{image_tag}</figure>"
            product_card_images.append(image_url)
            add_page_image(canonical, image_url)
            add_page_image(build_canonical("products"), image_url)

        cta = ""
        if amazon_url:
            cta = f'<p><a class="btn btn-large" href="{html.escape(amazon_url)}" rel="sponsored nofollow noopener" target="_blank">View on Amazon</a></p>'

        description = (entry.get("description") or "").strip()
        description_text = html_to_text(description).strip()
        product_is_indexable = bool(description_text) and description_text.casefold() != html_to_text(entry["title"]).strip().casefold()
        review_body = rewrite_urls(sanitize_html_content(description)) if description else ""
        if review_body:
            review_body = ensure_image_alt_attributes(review_body, fallback_alt=entry["title"])
        if review_body and not re.search(r"<[a-zA-Z!/]", review_body):
            review_body = f"<p>{html.escape(review_body)}</p>"
        if not review_body:
            review_body = (
                f"<p>{html.escape(entry['title'])}</p>"
                "<p>Check the retailer for current specifications, included filters, price, and availability. "
                "Use the related guides below to compare options before buying.</p>"
            )
        review_body = link_unlinked_images(review_body, amazon_url)
        product_focus_text = f"{entry['title']} {description}"
        product_related_articles = select_related_articles(
            product_focus_text,
            all_article_entries,
            limit=4,
        )
        product_related_articles_html = render_related_articles_block(
            product_related_articles,
            heading="Related Guides",
            intro="Compare your options with practical guides and explainers.",
        )
        product_related_use_cases = select_related_recommendations(
            product_focus_text,
            recommendation_entries,
            limit=6,
        )
        product_related_use_cases_html = render_related_recommendations_block(
            product_related_use_cases,
            heading="Recommended By Use Case",
        )
        breadcrumbs_items = build_breadcrumb_items(
            [
                ("Home", "/"),
                ("Products", "/products/"),
                (entry["title"], f"/{product_path}/"),
            ]
        )

        content = f"""
<article class="page-article review-article">
  {render_breadcrumbs(breadcrumbs_items)}
  <header class="content-header">
    <p class="eyebrow">Product</p>
    <h1>{html.escape(entry['title'])}</h1>
  </header>
  {image_html}
  <section class="content-section prose">
    {review_body}
    {cta}
  </section>
</article>
{product_related_articles_html}
{product_related_use_cases_html}
"""

        meta_description = normalize_meta_description(
            excerpt_from_content(description, 165) or entry["title"],
            fallback=f"{entry['title']} | {blogdesc}",
        )
        og_image = image_url or logo_url

        product_schema = {
            "@context": "https://schema.org",
            "@type": "Product",
            "name": entry["title"],
            "url": canonical,
            "image": [absolute_url(image_url)] if image_url else [DOMAIN + logo_url],
            "description": meta_description,
        }
        if entry.get("sku"):
            product_schema["sku"] = entry["sku"]

        json_ld = [
            {
                "@context": "https://schema.org",
                "@type": "Organization",
                "name": SITE_NAME,
                "url": DOMAIN,
                "logo": DOMAIN + logo_url,
                "sameAs": ["https://hepacertified.com/"],
            },
            breadcrumb_schema(breadcrumbs_items),
            product_schema,
        ]

        entry_post = posts.get(int(entry.get("post_id") or 0))
        entry_modified = site_lastmod
        if entry_post:
            entry_modified = entry_post.post_modified or entry_post.post_date

        html_doc = layout_html(
            page_title=f"{entry['title']} | {blogname}",
            meta_description=meta_description,
            canonical=canonical,
            body_class="type-product-detail",
            main_html=content,
            og_image=og_image if not og_image.startswith("/") else DOMAIN + og_image,
            json_ld=json_ld,
            logo_url=logo_url,
            site_icon_url=site_icon_url,
            recommendations_nav_html=recommendation_dropdown_html,
            og_type="product",
            robots_directive=("index" if product_is_indexable else "noindex") + ",follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1",
        )

        write_text(OUT_ROOT / product_path / "index.html", html_doc)
        if product_is_indexable:
            add_sitemap_url(canonical, entry_modified)
        product_index_entries.append(
            {
                "title": str(entry["title"]),
                "url": canonical,
                "image": absolute_url(image_url) if image_url else DOMAIN + logo_url,
            }
        )

        card_image_html = ""
        card_target = f"/{product_path}/"
        card_target_attrs = ""
        card_buy_link = (
            f'<a class="btn" href="{html.escape(amazon_url)}" rel="sponsored nofollow noopener" target="_blank">View on Amazon</a>'
            if amazon_url else ""
        )
        card_facets = product_filter_facets(entry)
        card_use_case_labels = card_facets.get("use_cases", [])
        card_room_labels = card_facets.get("rooms", [])
        card_feature_labels = card_facets.get("features", [])
        product_use_case_labels.update(card_use_case_labels)
        product_room_labels.update(card_room_labels)
        product_feature_labels.update(card_feature_labels)
        card_use_case_values = "|".join(slugify(label, "filter") for label in card_use_case_labels)
        card_room_values = "|".join(slugify(label, "filter") for label in card_room_labels)
        card_feature_values = "|".join(slugify(label, "filter") for label in card_feature_labels)
        facet_search_text = " ".join(
            [
                " ".join(card_use_case_labels),
                " ".join(card_room_labels),
                " ".join(card_feature_labels),
                "air purifier use case health need room and space feature",
            ]
        ).strip()
        add_search_entry(
            title=entry["title"],
            url=canonical,
            item_type="Product",
            extra_text=f"{meta_description} {facet_search_text}".strip(),
        )
        card_search_blob = " ".join(
            [
                str(entry.get("title", "")),
                html_to_text(str(entry.get("description", ""))),
                " ".join(card_use_case_labels),
                " ".join(card_room_labels),
                " ".join(card_feature_labels),
                "use case health need room feature",
            ]
        ).strip()
        if image_url:
            card_img = (
                f'<img src="{html.escape(image_url)}" alt="{html.escape(entry["title"])}" '
                'loading="lazy" decoding="async">'
            )
            card_image_html = f'<a class="image-link" href="{html.escape(card_target)}"{card_target_attrs}>{card_img}</a>'
        product_cards.append(
            f"""<article class="product-card" data-product-item data-product-search="{html.escape(card_search_blob.lower())}" data-product-use-cases="{html.escape(card_use_case_values)}" data-product-rooms="{html.escape(card_room_values)}" data-product-features="{html.escape(card_feature_values)}">
  <figure>{card_image_html}</figure>
  <h3><a href="{html.escape(card_target)}"{card_target_attrs}>{html.escape(entry['title'])}</a></h3>
  <p><a class="section-link" href="{html.escape(card_target)}">Product details</a></p>
  <p>{card_buy_link}</p>
</article>"""
        )

    article_index_entries = list(all_article_entries)
    articles_index_cards = [
        article_card_html(
            slug=str(entry.get("slug", "")),
            title=str(entry.get("title", "")),
            excerpt=str(entry.get("excerpt", "")),
            image_url=str(entry.get("image", "")),
            published_date=str(entry.get("date", "")),
            minutes_to_read=int(entry.get("minutes", 0) or 0),
        )
        for entry in article_index_entries
    ]

    # Build index pages
    article_total = len([p for p in published if p.post_type == "post"])
    product_total = len(review_entries)
    recommendation_total = len(recommendation_entries)

    featured_use_cases: list[dict[str, str]] = []
    seen_use_case_hrefs: set[str] = set()
    for _, grouped_items in recommendation_grouped_entries:
        for grouped_item in grouped_items[:3]:
            href = grouped_item["href"]
            if href in seen_use_case_hrefs:
                continue
            seen_use_case_hrefs.add(href)
            featured_use_cases.append(grouped_item)
            if len(featured_use_cases) >= 12:
                break
        if len(featured_use_cases) >= 12:
            break
    if not featured_use_cases:
        featured_use_cases = recommendation_entries[:12]

    use_case_chips_html = "".join(
        f'<a href="{html.escape(item["href"])}">{html.escape(item["title"])}</a>'
        for item in featured_use_cases
    )
    if not use_case_chips_html:
        use_case_chips_html = '<span class="use-case-empty">More use-case pages are coming soon.</span>'

    home_html = f"""
<section class="hero hero-home">
  <div class="hero-main">
    <p class="eyebrow">Air Quality Guides And Product Picks</p>
    <h1>Air Purifier Reviews &amp; Buying Guides</h1>
    <p class="hero-lead">{html.escape(HOME_DESCRIPTION)}</p>
    <p class="hero-links"><a class="btn btn-large" href="/products/">Browse Products</a> <a class="btn ghost" href="/articles/">Read Articles</a></p>
  </div>
  <aside class="hero-panel" aria-label="Site highlights">
    <p class="hero-panel-title">Quick Snapshot</p>
    <div class="hero-metrics">
      <div><strong>{article_total}</strong><span>Articles</span></div>
      <div><strong>{product_total}</strong><span>Products</span></div>
      <div><strong>{recommendation_total}</strong><span>Use Cases</span></div>
    </div>
    <p class="hero-panel-note">Use guides and product pages together for faster decisions.</p>
  </aside>
</section>
<section class="content-section home-products">
  <div class="section-head">
    <h2>Featured Products</h2>
    <a class="section-link" href="/products/">See all products</a>
  </div>
  <p class="section-intro">Explore product details and related guides, then check current prices on Amazon.</p>
  <div class="product-grid" data-featured-products>
    {''.join(product_cards[:12])}
  </div>
</section>
<section class="content-section home-use-cases">
  <div class="section-head">
    <h2>Start With Your Use Case</h2>
    <a class="section-link" href="/recommendations/">View all recommendations</a>
  </div>
  <div class="use-case-chips">
    {use_case_chips_html}
  </div>
</section>
<section class="content-section home-latest">
  <div class="section-head">
    <h2>Latest Articles</h2>
    <a class="section-link" href="/articles/">Browse all articles</a>
  </div>
  <p class="section-intro">Fresh comparisons, explainer guides, and practical buying advice from the latest published pieces.</p>
  <div class="article-grid article-grid-home">
    {''.join(reversed(articles_index_cards[-12:]))}
  </div>
</section>
"""

    home_canonical = f"{DOMAIN}/"
    home_latest_entries = list(reversed(article_index_entries[-12:]))
    home_product_items = product_index_entries[:12]
    home_json_ld: list[dict[str, Any]] = [
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "name": SITE_NAME,
            "url": DOMAIN,
            "logo": DOMAIN + logo_url,
            "sameAs": ["https://hepacertified.com/"],
        },
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": SITE_NAME,
            "url": DOMAIN,
            "description": HOME_DESCRIPTION,
        },
    ]
    if home_latest_entries:
        home_json_ld.append(
            {
                "@context": "https://schema.org",
                "@type": "ItemList",
                "name": "Latest Articles",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": idx + 1,
                        "name": str(entry.get("title", "")),
                        "url": build_canonical(str(entry.get("slug", "")).strip("/")),
                    }
                    for idx, entry in enumerate(home_latest_entries)
                    if str(entry.get("title", "")).strip() and str(entry.get("slug", "")).strip()
                ],
            }
        )
    if home_product_items:
        home_json_ld.append(
            {
                "@context": "https://schema.org",
                "@type": "ItemList",
                "name": "Featured Products",
                "itemListElement": [
                    {
                        "@type": "ListItem",
                        "position": idx + 1,
                        "name": str(item.get("title", "")),
                        "url": str(item.get("url", home_canonical)),
                    }
                    for idx, item in enumerate(home_product_items)
                    if str(item.get("title", "")).strip()
                ],
            }
        )
    write_text(
        OUT_ROOT / "index.html",
        layout_html(
            page_title=f"Air Purifier Reviews & Buying Guides | {blogname}",
            meta_description=HOME_DESCRIPTION,
            canonical=home_canonical,
            body_class="type-home",
            main_html=home_html,
            og_image=DOMAIN + logo_url,
            json_ld=home_json_ld,
            logo_url=logo_url,
            site_icon_url=site_icon_url,
            recommendations_nav_html=recommendation_dropdown_html,
        ),
    )
    add_sitemap_url(home_canonical, site_lastmod)
    add_search_entry(
        title=blogname,
        url=home_canonical,
        item_type="Home",
        extra_text=blogdesc,
    )
    for image_url in article_card_images[-15:]:
        add_page_image(home_canonical, image_url)
    for image_url in product_card_images[:12]:
        add_page_image(home_canonical, image_url)

    products_canonical = build_canonical("products")
    products_breadcrumbs = render_breadcrumbs(
        build_breadcrumb_items(
            [
                ("Home", "/"),
                ("Products", "/products/"),
            ]
        )
    )
    use_case_filter_options = options_html_from_labels(
        ordered_labels(product_use_case_labels, PRODUCT_USE_CASE_ORDER)
    )
    room_filter_options = options_html_from_labels(
        ordered_labels(product_room_labels, PRODUCT_ROOM_ORDER)
    )
    feature_filter_options = options_html_from_labels(
        ordered_labels(product_feature_labels, PRODUCT_FEATURE_ORDER)
    )
    write_text(
        OUT_ROOT / "products/index.html",
        layout_html(
            page_title=f"Air Purifier Products | {blogname}",
            meta_description=normalize_meta_description(
                "Compare air purifiers and replacement filters by room, use case, and features. Read product details and related guides before checking Amazon prices.",
                fallback=blogdesc,
            ),
            canonical=products_canonical,
            body_class="type-products",
            main_html=f"""
<section class="content-section">
  {products_breadcrumbs}
  <h1>Air Purifiers &amp; Replacement Filters</h1>
  <p>Explore product details and related buying guides. Filter by use case, room size, and key features, then check current prices and availability on Amazon.</p>
  <div class="product-filter-root" data-product-filter-root>
    <div class="product-filter-controls">
      <label class="product-filter-field is-search" for="product-search">
        <span>Search</span>
        <input id="product-search" type="search" class="rec-search-input" data-product-search placeholder="Type product, brand, allergy, smoke, Wi-Fi..." autocomplete="off">
      </label>
      <label class="product-filter-field" for="product-use-case">
        <span>Use Case</span>
        <select id="product-use-case" class="product-filter-select" data-product-use-case>
          <option value="">All use cases</option>
          {use_case_filter_options}
        </select>
      </label>
      <label class="product-filter-field" for="product-room">
        <span>Room</span>
        <select id="product-room" class="product-filter-select" data-product-room>
          <option value="">All room types</option>
          {room_filter_options}
        </select>
      </label>
      <label class="product-filter-field" for="product-feature">
        <span>Feature</span>
        <select id="product-feature" class="product-filter-select" data-product-feature>
          <option value="">All features</option>
          {feature_filter_options}
        </select>
      </label>
      <button class="btn ghost product-filter-reset" type="button" data-product-reset>Reset filters</button>
    </div>
    <p class="product-filter-summary" data-product-summary>Showing all {len(product_cards)} products.</p>
    <p class="rec-empty" data-product-empty hidden>No products match the selected filters.</p>
    <div class="product-grid">{''.join(product_cards)}</div>
  </div>
</section>
""",
            og_image=DOMAIN + logo_url,
            json_ld=[
                {
                    "@context": "https://schema.org",
                    "@type": "CollectionPage",
                    "name": "Products",
                    "url": products_canonical,
                    "description": "All discovered air purifier products with direct links.",
                },
                {
                    "@context": "https://schema.org",
                    "@type": "ItemList",
                    "name": "Product Index",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": idx + 1,
                            "name": str(item.get("title", "")),
                            "url": str(item.get("url", products_canonical)),
                        }
                        for idx, item in enumerate(product_index_entries)
                        if str(item.get("title", "")).strip()
                    ],
                },
                breadcrumb_schema(
                    build_breadcrumb_items(
                        [
                            ("Home", "/"),
                            ("Products", "/products/"),
                        ]
                    )
                ),
            ],
            logo_url=logo_url,
            site_icon_url=site_icon_url,
            recommendations_nav_html=recommendation_dropdown_html,
        ),
    )
    add_sitemap_url(products_canonical, site_lastmod)
    product_collection_terms = " ".join(
        sorted(product_use_case_labels | product_room_labels | product_feature_labels)
    )
    add_search_entry(
        title="Products",
        url=products_canonical,
        item_type="Collection",
        extra_text=(
            "air purifier products by use case health need room and space feature "
            f"{product_collection_terms}"
        ).strip(),
    )
    for image_url in product_card_images:
        add_page_image(products_canonical, image_url)

    recommendation_page_html = "<p>Recommendation pages will appear here as content is published.</p>"
    if recommendation_grouped_entries:
        recommendation_page_groups_html = "".join(
            (
                f"""<section class="rec-group" data-rec-group>
  <h2>{html.escape(category)}</h2>
  <div class="rec-links">"""
                + "".join(
                    f'<a href="{html.escape(item["href"])}" data-rec-item data-rec-text="{html.escape(item["search_text"])}">{html.escape(item["title"])}</a>'
                    for item in items
                )
                + "</div></section>"
            )
            for category, items in recommendation_grouped_entries
        )
        recommendation_page_html = f"""
<div class="rec-filter-root" data-rec-filter-root>
  <label class="rec-search-label" for="recommendation-search">Search by use case</label>
  <input id="recommendation-search" type="search" class="rec-search-input" data-rec-search placeholder="Type a room, need, or feature..." autocomplete="off">
  <p class="rec-empty" data-rec-empty hidden>No recommendation matches your search.</p>
  <div class="rec-groups">{recommendation_page_groups_html}</div>
</div>
"""

    recommendations_canonical = build_canonical("recommendations")
    recommendations_breadcrumbs = render_breadcrumbs(
        build_breadcrumb_items(
            [
                ("Home", "/"),
                ("Recommendations", "/recommendations/"),
            ]
        )
    )
    write_text(
        OUT_ROOT / "recommendations/index.html",
        layout_html(
            page_title=f"Recommendations | {blogname}",
            meta_description=normalize_meta_description(
                "Browse recommendation pages by air purifier use case.",
                fallback=blogdesc,
            ),
            canonical=recommendations_canonical,
            body_class="type-recommendations",
            main_html=f"""
<section class="content-section">
  {recommendations_breadcrumbs}
  <h1>Recommendations</h1>
  <p>Browse recommendation pages by health need, room and space, and features.</p>
  {recommendation_page_html}
</section>
""",
            og_image=DOMAIN + logo_url,
            json_ld=[
                {
                    "@context": "https://schema.org",
                    "@type": "CollectionPage",
                    "name": "Recommendations",
                    "url": recommendations_canonical,
                    "description": "Air purifier recommendations grouped by use case and feature.",
                },
                {
                    "@context": "https://schema.org",
                    "@type": "ItemList",
                    "name": "Recommendation Categories",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": idx + 1,
                            "name": item["title"],
                            "url": absolute_url(item["href"]),
                        }
                        for idx, item in enumerate(recommendation_entries)
                    ],
                },
                breadcrumb_schema(
                    build_breadcrumb_items(
                        [
                            ("Home", "/"),
                            ("Recommendations", "/recommendations/"),
                        ]
                    )
                ),
            ],
            logo_url=logo_url,
            site_icon_url=site_icon_url,
            recommendations_nav_html=recommendation_dropdown_html,
        ),
    )
    add_sitemap_url(recommendations_canonical, site_lastmod)
    recommendation_collection_terms = " ".join(
        sorted({entry["category"].lower() for entry in recommendation_entries})
    )
    add_search_entry(
        title="Recommendations",
        url=recommendations_canonical,
        item_type="Collection",
        extra_text=(
            "recommendations by use case health need room and space feature type "
            f"{recommendation_collection_terms}"
        ).strip(),
    )

    articles_canonical = build_canonical("articles")
    articles_breadcrumbs = render_breadcrumbs(
        build_breadcrumb_items(
            [
                ("Home", "/"),
                ("Articles", "/articles/"),
            ]
        )
    )
    latest_article_entries = list(reversed(article_index_entries))
    featured_article_html = '<p class="section-intro">Fresh articles will appear here as they are published.</p>'
    article_archive_cards_html = ""
    if latest_article_entries:
        lead = latest_article_entries[0]
        featured_article_html = (
            '<div class="article-spotlight">'
            + article_card_html(
                slug=str(lead.get("slug", "")),
                title=str(lead.get("title", "")),
                excerpt=str(lead.get("excerpt", "")),
                image_url=str(lead.get("image", "")),
                published_date=str(lead.get("date", "")),
                minutes_to_read=int(lead.get("minutes", 0) or 0),
                featured=True,
            )
            + "</div>"
        )
        article_archive_cards_html = "".join(
            article_card_html(
                slug=str(entry.get("slug", "")),
                title=str(entry.get("title", "")),
                excerpt=str(entry.get("excerpt", "")),
                image_url=str(entry.get("image", "")),
                published_date=str(entry.get("date", "")),
                minutes_to_read=int(entry.get("minutes", 0) or 0),
            )
            for entry in latest_article_entries[1:]
        )
    if not article_archive_cards_html:
        article_archive_cards_html = '<p class="section-intro">New articles are being prepared.</p>'

    write_text(
        OUT_ROOT / "articles/index.html",
        layout_html(
            page_title=f"Articles | {blogname}",
            meta_description=normalize_meta_description(
                "All published air quality and purifier articles in one place.",
                fallback=blogdesc,
            ),
            canonical=articles_canonical,
            body_class="type-articles",
            main_html=f"""
<section class="content-section articles-hero">
  {articles_breadcrumbs}
  <p class="eyebrow">Editorial Archive</p>
  <h1>Articles</h1>
  <p class="section-intro">Clean-air explainers, side-by-side comparisons, and buying guides from the full publication archive.</p>
  {featured_article_html}
</section>
<section class="content-section articles-collection">
  <div class="section-head">
    <h2>All Articles</h2>
    <p class="articles-count">{article_total} published</p>
  </div>
  <div class="article-grid">{article_archive_cards_html}</div>
</section>
""",
            og_image=DOMAIN + logo_url,
            json_ld=[
                {
                    "@context": "https://schema.org",
                    "@type": "CollectionPage",
                    "name": "Articles",
                    "url": articles_canonical,
                    "description": "Full article archive for air quality and purifier topics.",
                },
                {
                    "@context": "https://schema.org",
                    "@type": "ItemList",
                    "name": "Article Archive",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "position": idx + 1,
                            "name": str(entry.get("title", "")),
                            "url": build_canonical(str(entry.get("slug", "")).strip("/")),
                        }
                        for idx, entry in enumerate(latest_article_entries)
                        if str(entry.get("title", "")).strip() and str(entry.get("slug", "")).strip()
                    ],
                },
                breadcrumb_schema(
                    build_breadcrumb_items(
                        [
                            ("Home", "/"),
                            ("Articles", "/articles/"),
                        ]
                    )
                ),
            ],
            logo_url=logo_url,
            site_icon_url=site_icon_url,
            recommendations_nav_html=recommendation_dropdown_html,
        ),
    )
    add_sitemap_url(articles_canonical, site_lastmod)
    add_search_entry(
        title="Articles",
        url=articles_canonical,
        item_type="Collection",
        extra_text="air quality guides and articles",
    )
    for image_url in article_card_images:
        add_page_image(articles_canonical, image_url)

    # 404 page
    write_text(
        OUT_ROOT / "404.html",
        layout_html(
            page_title=f"Page Not Found | {blogname}",
            meta_description="The requested page could not be found.",
            canonical=build_canonical("404"),
            body_class="type-404",
            main_html="""
<section class="content-section">
  <h1>Page Not Found</h1>
  <p>The page you requested does not exist in this static build.</p>
  <p><a class="btn" href="/">Return to homepage</a></p>
</section>
""",
            og_image=DOMAIN + logo_url,
            json_ld=[],
            logo_url=logo_url,
            site_icon_url=site_icon_url,
            recommendations_nav_html=recommendation_dropdown_html,
            robots_directive="noindex,follow,max-image-preview:large,max-snippet:-1,max-video-preview:-1",
        ),
    )

    # robots/sitemap/Cloudflare helper files
    if f"{DOMAIN}/" not in sitemap_entries:
        add_sitemap_url(f"{DOMAIN}/", site_lastmod)
    unique_sitemap = sorted(set(sitemap_entries))
    sitemap_xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for url in unique_sitemap:
        sitemap_xml.append("  <url>")
        sitemap_xml.append(f"    <loc>{html.escape(url)}</loc>")
        lastmod = sitemap_lastmod.get(url, "")
        if lastmod:
            sitemap_xml.append(f"    <lastmod>{lastmod}</lastmod>")
        sitemap_xml.append("  </url>")
    sitemap_xml.append("</urlset>")
    write_text(OUT_ROOT / "sitemap.xml", "\n".join(sitemap_xml) + "\n")

    image_sitemap_xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">',
    ]
    for page_url in sorted(images_by_page):
        if page_url not in unique_sitemap:
            continue
        page_images = sorted(images_by_page[page_url])
        if not page_images:
            continue
        image_sitemap_xml.append("  <url>")
        image_sitemap_xml.append(f"    <loc>{html.escape(page_url)}</loc>")
        for image_loc in page_images:
            image_sitemap_xml.append("    <image:image>")
            image_sitemap_xml.append(f"      <image:loc>{html.escape(image_loc)}</image:loc>")
            image_sitemap_xml.append("    </image:image>")
        image_sitemap_xml.append("  </url>")
    image_sitemap_xml.append("</urlset>")
    write_text(OUT_ROOT / "sitemap-images.xml", "\n".join(image_sitemap_xml) + "\n")

    robots = f"""# Search indexing and user-requested discovery are welcome.
# Cloudflare's managed training restrictions are configured separately.
User-agent: Googlebot
User-agent: Bingbot
User-agent: DuckDuckBot
User-agent: Applebot
User-agent: Baiduspider
User-agent: PetalBot
User-agent: OAI-SearchBot
User-agent: ChatGPT-User
User-agent: PerplexityBot
User-agent: Perplexity-User
User-agent: Claude-SearchBot
User-agent: Claude-User
Allow: /

User-agent: *
Allow: /

Sitemap: {DOMAIN}/sitemap.xml
Sitemap: {DOMAIN}/sitemap-images.xml
"""
    write_text(OUT_ROOT / "robots.txt", robots)

    redirect_lines = [
        "/index.php / 301",
        "/home / 301",
        "/home/ / 301",
        "/blog /articles/ 301",
        "/blog/ /articles/ 301",
        "/reviews /products/ 301",
        "/reviews/ /products/ 301",
        "/reviews/* /products/:splat 301",
    ]
    for source_path in sorted(product_redirects):
        redirect_lines.append(f"{source_path} {product_redirects[source_path]} 301")
    redirects = "\n".join(redirect_lines) + "\n"
    write_text(OUT_ROOT / "_redirects", redirects)

    headers = """/*
  X-Frame-Options: SAMEORIGIN
  X-Content-Type-Options: nosniff
  Referrer-Policy: strict-origin-when-cross-origin
  Permissions-Policy: interest-cohort=()
  X-Robots-Tag: all
"""
    write_text(OUT_ROOT / "_headers", headers)

    search_index_entries.sort(key=lambda item: item["title"].lower())
    write_text(OUT_ROOT / "search-index.json", json.dumps(search_index_entries, ensure_ascii=False) + "\n")

    readme = f"""# airpurify.ing static site

This repository contains a static rebuild of the original WordPress site content and media.

## What was extracted

- Published WordPress pages/posts/products from the SQL export
- Product affiliate links from WooCommerce external product metadata
- Additional product feed records from `_cegg_data_Amazon` metadata
- Logo and media assets from `uploads/`
- SEO index files (`sitemap.xml`, `robots.txt`) and Cloudflare Pages files (`_redirects`, `_headers`)

## Regenerate site

```bash
python3 build_site.py
python3 validate_site.py
```

The builder uses the original WordPress SQL backup and reuses local uploads when
available. It overwrites generated output without deleting unrelated files.
Use `--output-dir /tmp/airpurify-preview --uploads-dir /path/to/uploads` to build
into a separate directory. Run `python3 validate_site.py /tmp/airpurify-preview`
to check that output. Article dates remain the original content dates.

See [DISCOVERABILITY.md](DISCOVERABILITY.md) for the crawl policy, deployment
checks, and remaining editorial improvements.

## Deploy to Cloudflare Pages

- Build command: *(none)*
- Build output directory: `/`
- Root directory: repository root

The project is fully static and ready for GitHub + Cloudflare Pages deployment.
"""
    write_text(OUT_ROOT / "README.md", readme)

    # styles
    styles = """/* Airy minimalist system */
.affiliate-notice {
  margin: 1rem 0;
  padding: 0.75rem 1rem;
  border: 1px solid #d3dce9;
  border-radius: 8px;
  background: #ffffff;
  color: #5f728f;
  font-size: 0.875rem;
}

:root {
  --bg: #f3f7fc;
  --surface: #ffffff;
  --ink: #1f2f47;
  --muted: #5f728f;
  --line: #d3dce9;
  --brand: #324b6e;
  --brand-soft: #e3ebf6;
  --shadow: 0 14px 40px rgba(50, 75, 110, 0.1);
  --radius: 16px;
}

* {
  box-sizing: border-box;
}

[hidden] {
  display: none !important;
}

html,
body {
  margin: 0;
  padding: 0;
  background:
    radial-gradient(1000px 500px at 10% -10%, #e4edf8 0%, transparent 60%),
    radial-gradient(900px 500px at 100% 0%, #edf3fb 0%, transparent 55%),
    var(--bg);
  color: var(--ink);
  font: 16px/1.65 "Avenir Next", "Helvetica Neue", Helvetica, Arial, sans-serif;
}

a {
  color: var(--brand);
  text-decoration-thickness: 1.2px;
  text-underline-offset: 2px;
}

a:hover {
  text-decoration-thickness: 2px;
}

.image-link {
  display: block;
  line-height: 0;
}

.image-link img {
  display: block;
}

.shell {
  width: min(1120px, 92vw);
  margin: 0 auto;
}

.site-header {
  position: sticky;
  top: 0;
  z-index: 10;
  backdrop-filter: blur(8px);
  background: color-mix(in srgb, var(--bg) 86%, white 14%);
  border-bottom: 1px solid var(--line);
}

.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.9rem 1.2rem;
  min-height: 76px;
  position: relative;
}

.brand img {
  width: clamp(128px, 17vw, 188px);
  height: auto;
}

.header-tools {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.85rem 1rem;
  flex: 1 1 auto;
  min-width: 0;
}

.main-nav {
  display: flex;
  gap: 1rem;
  flex-wrap: wrap;
  align-items: center;
}

.main-nav a {
  font-weight: 600;
  text-decoration: none;
  color: var(--ink);
  opacity: 0.88;
}

.main-nav a:hover {
  opacity: 1;
  color: var(--brand);
}

.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}

.site-search {
  position: relative;
  width: min(360px, 44vw);
  min-width: 210px;
}

.site-search-input {
  width: 100%;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: #fff;
  color: var(--ink);
  font: inherit;
  font-size: 0.93rem;
  padding: 0.42rem 0.85rem;
}

.site-search-input::placeholder {
  color: color-mix(in srgb, var(--muted) 84%, white 16%);
}

.site-search-input:focus {
  outline: 2px solid color-mix(in srgb, var(--brand) 30%, #fff 70%);
  outline-offset: 1px;
  border-color: var(--brand);
}

.search-results {
  position: absolute;
  top: calc(100% + 0.38rem);
  left: 0;
  right: 0;
  border: 1px solid var(--line);
  border-radius: 14px;
  background: #fff;
  box-shadow: var(--shadow);
  padding: 0.35rem;
  display: grid;
  gap: 0.12rem;
  z-index: 35;
  max-height: min(60vh, 440px);
  overflow: auto;
}

.search-hit {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.8rem;
  text-decoration: none;
  border-radius: 10px;
  padding: 0.42rem 0.48rem;
  color: var(--ink);
}

.search-hit:hover {
  background: color-mix(in srgb, var(--brand-soft) 60%, white 40%);
}

.search-hit-title {
  font-size: 0.9rem;
  line-height: 1.25;
}

.search-hit-meta {
  color: var(--muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.search-empty {
  margin: 0;
  padding: 0.5rem 0.5rem;
  color: var(--muted);
  font-size: 0.9rem;
}

.nav-dropdown {
  position: static;
}

.nav-dropdown > a::after {
  content: " v";
  font-size: 0.72rem;
  color: var(--muted);
}

.nav-dropdown.is-open > a,
.nav-dropdown:focus-within > a {
  color: var(--brand);
  opacity: 1;
}

.mega-menu {
  position: absolute;
  left: 0;
  right: 0;
  top: calc(100% - 1px);
  border: 1px solid var(--line);
  border-radius: 0 0 14px 14px;
  background: color-mix(in srgb, var(--surface) 92%, var(--bg) 8%);
  box-shadow: var(--shadow);
  padding: 0.95rem 1.1rem 1rem;
  opacity: 0;
  visibility: hidden;
  transform: translateY(8px);
  pointer-events: none;
  transition: opacity 160ms ease, transform 160ms ease, visibility 160ms ease;
  z-index: 30;
}

.nav-dropdown.is-open .mega-menu,
.nav-dropdown:focus-within .mega-menu {
  opacity: 1;
  visibility: visible;
  transform: translateY(0);
  pointer-events: auto;
}

.mega-heading {
  margin: 0;
  color: var(--muted);
  font-size: 0.78rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 700;
}

.mega-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.8rem 1rem;
  flex-wrap: wrap;
  margin-bottom: 0.75rem;
}

.rec-search-input {
  width: min(420px, 100%);
  border: 1px solid var(--line);
  border-radius: 11px;
  background: #fff;
  color: var(--ink);
  font: inherit;
  padding: 0.45rem 0.7rem;
}

.rec-search-input:focus {
  outline: 2px solid color-mix(in srgb, var(--brand) 35%, #fff 65%);
  outline-offset: 1px;
  border-color: var(--brand);
}

.mega-empty {
  margin: 0.15rem 0 0.75rem;
  color: var(--muted);
  font-size: 0.9rem;
}

.mega-groups {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 0.9rem;
  max-height: min(58vh, 520px);
  overflow: auto;
  padding-right: 0.2rem;
}

.mega-group {
  border: 1px solid var(--line);
  border-radius: 12px;
  background: color-mix(in srgb, var(--surface) 94%, var(--bg) 6%);
  padding: 0.6rem 0.72rem 0.72rem;
}

.mega-group h3 {
  margin: 0 0 0.48rem;
  color: var(--muted);
  font-size: 0.76rem;
  letter-spacing: 0.07em;
  text-transform: uppercase;
}

.mega-links {
  display: grid;
  gap: 0.35rem;
}

.mega-links a {
  display: block;
  padding: 0.08rem 0;
  color: var(--ink);
  opacity: 1;
  font-size: 0.9rem;
  text-decoration: none;
}

.mega-links a:hover {
  color: var(--brand);
}

[data-rec-item][hidden],
[data-rec-group][hidden],
[data-rec-empty][hidden] {
  display: none !important;
}

main.shell {
  padding: 2.2rem 0 4rem;
}

.hero {
  border: 1px solid var(--line);
  border-radius: calc(var(--radius) + 6px);
  padding: clamp(1.6rem, 2.3vw, 2.6rem);
  background:
    radial-gradient(580px 220px at 95% 0%, #dfe9f7 0%, transparent 70%),
    linear-gradient(145deg, #ffffff 0%, #f1f5fb 100%);
  box-shadow: var(--shadow);
  margin-bottom: 1.6rem;
}

.hero-home {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(220px, 0.85fr);
  gap: 1.1rem 1.2rem;
  align-items: stretch;
}

.hero h1 {
  margin: 0.2rem 0 0.6rem;
  font-size: clamp(1.8rem, 4.2vw, 3rem);
  line-height: 1.1;
  letter-spacing: -0.02em;
}

.hero p {
  max-width: 68ch;
}

.hero-lead {
  font-size: clamp(1rem, 1.35vw, 1.2rem);
}

.hero-links {
  margin-top: 1rem;
}

.hero-panel {
  border: 1px solid color-mix(in srgb, var(--line) 75%, white 25%);
  border-radius: 14px;
  background: color-mix(in srgb, #ffffff 86%, var(--brand-soft) 14%);
  padding: 0.9rem 1rem;
}

.hero-panel-title {
  margin: 0 0 0.62rem;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-size: 0.78rem;
  font-weight: 700;
}

.hero-metrics {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0.55rem;
}

.hero-metrics > div {
  border: 1px solid var(--line);
  border-radius: 11px;
  background: #fff;
  padding: 0.48rem 0.44rem;
  text-align: center;
}

.hero-metrics strong {
  display: block;
  font-size: 1.1rem;
  line-height: 1.1;
}

.hero-metrics span {
  display: block;
  margin-top: 0.18rem;
  color: var(--muted);
  font-size: 0.75rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.hero-panel-note {
  margin: 0.72rem 0 0;
  color: #3f5677;
  font-size: 0.9rem;
  line-height: 1.45;
}

.eyebrow {
  margin: 0;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 700;
}

.page-article,
.content-section {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow);
}

.page-article {
  padding: clamp(1.2rem, 2vw, 2rem);
}

.content-section {
  margin-top: 1rem;
  padding: clamp(1rem, 1.8vw, 1.5rem);
}

.section-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.5rem 1rem;
  flex-wrap: wrap;
}

.section-head h2 {
  margin: 0;
}

.section-link {
  color: var(--brand);
  text-decoration: none;
  font-size: 0.9rem;
  font-weight: 700;
}

.section-link:hover {
  text-decoration: underline;
}

.section-intro {
  margin: 0.45rem 0 0.9rem;
  color: #3f5677;
}

.breadcrumbs {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.3rem 0.45rem;
  margin: 0 0 0.75rem;
  font-size: 0.82rem;
  color: var(--muted);
}

.breadcrumbs a {
  color: var(--muted);
  text-decoration: none;
}

.breadcrumbs a:hover {
  color: var(--brand);
}

.crumb-sep {
  opacity: 0.55;
}

.home-use-cases {
  background:
    radial-gradient(680px 180px at 4% 0%, #e8eff9 0%, transparent 68%),
    #fff;
}

.use-case-chips {
  margin-top: 0.85rem;
  display: flex;
  flex-wrap: wrap;
  gap: 0.55rem 0.62rem;
}

.use-case-chips a,
.use-case-empty {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.34rem 0.7rem;
  background: color-mix(in srgb, var(--brand-soft) 40%, #fff 60%);
  color: var(--ink);
  text-decoration: none;
  font-size: 0.88rem;
}

.use-case-chips a:hover {
  border-color: color-mix(in srgb, var(--brand) 55%, var(--line) 45%);
  color: var(--brand);
}

.content-header h1 {
  margin: 0.45rem 0;
  font-size: clamp(1.45rem, 3.2vw, 2.25rem);
  letter-spacing: -0.02em;
  line-height: 1.15;
}

.meta {
  margin: 0.35rem 0;
  color: var(--muted);
  font-size: 0.95rem;
}

.featured {
  margin: 0.8rem 0 1.1rem;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--line);
}

.featured img {
  display: block;
  width: 100%;
  height: auto;
}

.article-page {
  display: grid;
  grid-template-columns: minmax(0, 1.18fr) minmax(300px, 0.82fr);
  grid-template-areas:
    "header media"
    "body body"
    "actions actions";
  gap: 1rem 1.2rem;
}

.article-page.no-featured {
  grid-template-columns: 1fr;
  grid-template-areas:
    "header"
    "body"
    "actions";
}

.article-header {
  grid-area: header;
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: clamp(1rem, 1.7vw, 1.4rem);
  background:
    radial-gradient(440px 190px at 100% 0%, #e7effa 0%, transparent 68%),
    linear-gradient(145deg, #ffffff 0%, #f5f8fd 100%);
}

.article-header h1 {
  margin: 0.45rem 0 0.5rem;
  font-size: clamp(1.8rem, 3.4vw, 2.5rem);
  line-height: 1.12;
  letter-spacing: -0.02em;
}

.article-intro {
  margin: 0;
  font-size: clamp(1rem, 1.3vw, 1.1rem);
  color: #304766;
  max-width: 62ch;
}

.article-meta-line {
  margin: 0.72rem 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.article-meta-line span {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.2rem 0.55rem;
  background: color-mix(in srgb, var(--brand-soft) 55%, white 45%);
  color: var(--ink);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.article-tax {
  margin: 0.62rem 0 0;
  color: var(--muted);
  font-size: 0.9rem;
}

.article-page > .featured {
  grid-area: media;
  margin: 0;
  align-self: start;
  min-height: 0;
  aspect-ratio: 1 / 1;
  background: #edf3fb;
  display: grid;
  place-items: center;
}

.article-page > .featured img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.article-page > .featured > .image-link {
  width: 100%;
  height: 100%;
  display: grid;
  place-items: center;
}

.article-page > .content-section {
  grid-column: 1 / -1;
  margin-top: 0;
}

.article-page .prose {
  background:
    linear-gradient(180deg, #ffffff 0%, #fbfdff 100%);
}

.article-page .prose > :first-child {
  margin-top: 0;
}

.article-actions {
  margin-top: 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem 1rem;
}

.article-actions-label {
  margin: 0;
  color: var(--muted);
  font-size: 0.88rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 700;
}

.article-actions-links {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem 0.5rem;
}

.recommendation-page {
  display: grid;
  gap: 1rem;
}

.recommendation-header {
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: clamp(1rem, 1.7vw, 1.35rem);
  background:
    radial-gradient(500px 170px at 100% 0%, #e6eef9 0%, transparent 70%),
    linear-gradient(145deg, #ffffff 0%, #f3f7fd 100%);
}

.recommendation-header h1 {
  margin: 0.42rem 0 0.45rem;
}

.recommendation-intro {
  margin: 0.15rem 0 0;
  max-width: 70ch;
  color: #304766;
  font-size: 1rem;
}

.recommendation-links {
  margin: 0.7rem 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem 0.65rem;
}

.recommendation-products .section-intro {
  margin-bottom: 0.85rem;
}

.rec-product-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
  gap: 0.9rem;
}

.rec-product-card {
  border: 1px solid var(--line);
  border-radius: 13px;
  background: #fff;
  padding: 0.8rem;
  display: grid;
  grid-template-columns: 108px 1fr;
  gap: 0.55rem 0.82rem;
  align-items: start;
}

.rec-product-card figure {
  margin: 0;
  width: 108px;
  height: 108px;
  border-radius: 11px;
  overflow: hidden;
  border: 1px solid var(--line);
  background: #eef3fa;
}

.rec-product-card img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.rec-product-copy {
  display: grid;
  gap: 0.35rem;
}

.rec-product-copy h3 {
  margin: 0;
  font-size: 0.95rem;
  line-height: 1.3;
}

.rec-product-copy h3 a {
  color: var(--ink);
  text-decoration: none;
}

.rec-product-copy h3 a:hover {
  color: var(--brand);
}

.rec-product-info {
  margin: 0;
  color: #3f5677;
  font-size: 0.89rem;
  line-height: 1.48;
}

.rec-product-chips {
  margin: 0.1rem 0 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.32rem;
}

.rec-product-chips span {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.13rem 0.46rem;
  background: color-mix(in srgb, var(--brand-soft) 50%, white 50%);
  color: var(--muted);
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 700;
}

.rec-product-copy > p:last-child {
  margin: 0.25rem 0 0;
}

.recommendation-page .recommendation-products .rec-product-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}

.recommendation-page .recommendation-products .rec-product-card {
  grid-template-columns: 132px 1fr;
  gap: 0.65rem 0.95rem;
  padding: 0.95rem;
  border-radius: 15px;
  background:
    linear-gradient(145deg, #ffffff 0%, #f5f8fe 100%);
  box-shadow: 0 1px 0 rgba(42, 64, 98, 0.05);
}

.recommendation-page .recommendation-products .rec-product-card figure {
  width: 132px;
  height: 132px;
  border-radius: 13px;
  background: #edf3fb;
}

.recommendation-page .recommendation-products .rec-product-copy {
  gap: 0.42rem;
}

.recommendation-page .recommendation-products .rec-product-copy h3 {
  font-size: 1.02rem;
  line-height: 1.32;
}

.recommendation-page .recommendation-products .rec-product-info {
  font-size: 0.92rem;
}

.recommendation-page .recommendation-products .rec-product-copy > p:last-child {
  margin-top: 0.34rem;
}

.articles-hero {
  background:
    radial-gradient(700px 230px at 100% -8%, #e2ebf8 0%, transparent 70%),
    #fff;
}

.articles-count {
  margin: 0;
  color: var(--muted);
  font-size: 0.87rem;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 700;
}

.article-spotlight {
  margin-top: 1rem;
}

.review-article {
  display: grid;
  grid-template-columns: minmax(150px, 210px) 1fr;
  grid-template-areas:
    "media header"
    "media body";
  gap: 0.9rem 1.2rem;
}

.review-article .content-header {
  grid-area: header;
}

.review-article .featured {
  grid-area: media;
  margin: 0;
}

.review-article .content-section {
  grid-area: body;
  margin-top: 0;
}

.prose h2,
.prose h3,
.prose h4 {
  line-height: 1.2;
  margin-top: 1.4em;
  margin-bottom: 0.45em;
}

.prose p,
.prose li {
  color: #2b3f5d;
  overflow-wrap: anywhere;
}

.prose img,
.prose video {
  max-width: 100%;
  height: auto;
}

.prose iframe {
  max-width: 100%;
  border: 0;
}

.prose a {
  overflow-wrap: anywhere;
}

/* Hide legacy inline affiliate SVG badges that render as large blocks in article prose. */
.prose > svg,
.prose > a > svg {
  display: none;
}

.product-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
  gap: 1rem;
}

.product-card {
  border: 1px solid var(--line);
  border-radius: 14px;
  background: #ffffff;
  padding: 0.92rem;
  display: grid;
  grid-template-columns: 112px 1fr;
  gap: 0.6rem 0.9rem;
  align-items: center;
}

.product-card figure {
  margin: 0;
  border-radius: 12px;
  overflow: hidden;
  background: #eef3fa;
  width: 112px;
  height: 112px;
  min-height: 112px;
  display: grid;
  place-items: center;
  grid-column: 1;
  grid-row: 1 / span 2;
}

.product-card img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.product-card h3 {
  margin: 0;
  font-size: 0.99rem;
  line-height: 1.3;
  grid-column: 2;
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.product-card > p {
  grid-column: 2;
  margin: 0.12rem 0 0;
}

.product-card .btn {
  font-size: 0.78rem;
  font-weight: 600;
  padding: 0.28rem 0.64rem;
}

.article-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1.08rem;
}

.article-grid-home {
  grid-template-columns: 1fr;
}

.article-card {
  border: 1px solid var(--line);
  border-radius: 16px;
  background: #ffffff;
  padding: 0.95rem 0.98rem;
  display: grid;
  grid-template-columns: clamp(146px, 21vw, 198px) minmax(0, 1fr);
  gap: 0.78rem 1rem;
  align-items: stretch;
}

.article-card.is-featured {
  grid-template-columns: clamp(170px, 24vw, 224px) minmax(0, 1fr);
  padding: 1rem;
  background:
    radial-gradient(540px 170px at 95% 0%, #e9f0fb 0%, transparent 70%),
    #ffffff;
}

.article-card figure {
  margin: 0;
  width: 100%;
  height: 100%;
  min-height: 0;
  aspect-ratio: 1 / 1;
  align-self: start;
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid var(--line);
  background: #edf3fb;
  display: grid;
  place-items: center;
  padding: 0.3rem;
}

.article-card.is-featured figure {
  width: 100%;
  min-height: 0;
  aspect-ratio: 1 / 1;
}

.article-card img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.article-card:not(.has-media) {
  grid-template-columns: 1fr;
}

.article-card.no-media {
  grid-template-columns: 1fr;
}

.article-card.no-media figure {
  display: none;
}

.article-copy {
  display: grid;
  gap: 0.36rem;
  align-content: center;
}

.article-card-meta {
  margin: 0;
  display: flex;
  flex-wrap: wrap;
  gap: 0.38rem;
}

.article-card-meta span {
  display: inline-block;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 0.14rem 0.48rem;
  background: color-mix(in srgb, var(--brand-soft) 52%, white 48%);
  color: var(--muted);
  font-size: 0.74rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  font-weight: 700;
}

.article-title {
  margin: 0;
  font-size: clamp(1.04rem, 1.45vw, 1.22rem);
  line-height: 1.22;
}

.article-card.is-featured .article-title {
  font-size: clamp(1.14rem, 1.9vw, 1.46rem);
}

.article-title a {
  color: var(--ink);
  text-decoration: none;
}

.article-title a:hover {
  color: var(--brand);
}

.article-excerpt {
  margin: 0;
  color: #3f5677;
  font-size: 0.96rem;
  line-height: 1.53;
  display: -webkit-box;
  -webkit-line-clamp: 5;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.article-card.is-featured .article-excerpt {
  -webkit-line-clamp: 6;
}

.article-link-row {
  margin: 0.25rem 0 0;
}

.article-link {
  display: inline-block;
  font-size: 0.87rem;
  font-weight: 700;
  color: var(--brand);
  text-decoration: none;
  border-bottom: 1px solid transparent;
}

.article-link:hover {
  border-bottom-color: currentColor;
}

.related-article-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
  gap: 0.85rem;
}

.related-article-card {
  border: 1px solid var(--line);
  border-radius: 13px;
  background: #fff;
  padding: 0.72rem;
  display: grid;
  grid-template-columns: 112px 1fr;
  gap: 0.55rem 0.8rem;
  align-items: start;
}

.related-article-card figure {
  margin: 0;
  width: 112px;
  height: 112px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--line);
  background: #edf3fb;
  display: grid;
  place-items: center;
  padding: 0.24rem;
}

.article-card figure.is-empty,
.product-card figure.is-empty,
.rec-product-card figure.is-empty,
.related-article-card figure.is-empty {
  display: none;
}

.article-card figure:empty,
.product-card figure:empty,
.rec-product-card figure:empty,
.related-article-card figure:empty {
  display: none;
}

.product-card.no-media,
.rec-product-card.no-media,
.related-article-card.no-media {
  grid-template-columns: 1fr;
}

.product-card.no-media h3,
.product-card.no-media > p {
  grid-column: 1;
}

.related-article-card img {
  width: 100%;
  height: 100%;
  object-fit: contain;
}

.related-article-copy {
  display: grid;
  gap: 0.3rem;
}

.related-article-meta {
  margin: 0;
  color: var(--muted);
  font-size: 0.74rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
}

.related-article-copy h3 {
  margin: 0;
  font-size: 0.95rem;
  line-height: 1.28;
}

.related-article-copy h3 a {
  color: var(--ink);
  text-decoration: none;
}

.related-article-copy h3 a:hover {
  color: var(--brand);
}

.related-article-copy p:last-child {
  margin: 0;
  color: #3f5677;
  font-size: 0.88rem;
}

.rec-filter-root {
  margin-top: 0.8rem;
}

.product-filter-root {
  margin-top: 0.85rem;
  display: grid;
  gap: 0.72rem;
}

.product-filter-controls {
  display: grid;
  grid-template-columns: minmax(220px, 1.5fr) repeat(3, minmax(140px, 1fr)) auto;
  gap: 0.55rem 0.62rem;
  align-items: end;
}

.product-filter-field {
  display: grid;
  gap: 0.25rem;
  min-width: 0;
}

.product-filter-field span {
  display: block;
  font-size: 0.75rem;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 700;
}

.product-filter-controls .rec-search-input,
.product-filter-select {
  width: 100%;
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: #fff;
  color: var(--ink);
  font: inherit;
  font-size: 0.92rem;
  padding: 0.44rem 0.66rem;
}

.product-filter-select {
  appearance: none;
  background-image:
    linear-gradient(45deg, transparent 50%, #6f8099 50%),
    linear-gradient(135deg, #6f8099 50%, transparent 50%),
    linear-gradient(to right, transparent, transparent);
  background-position:
    calc(100% - 16px) calc(50% - 3px),
    calc(100% - 11px) calc(50% - 3px),
    100% 0;
  background-size:
    5px 5px,
    5px 5px,
    2.2em 2.2em;
  background-repeat: no-repeat;
  padding-right: 2.2rem;
}

.product-filter-controls .rec-search-input:focus,
.product-filter-select:focus {
  outline: 2px solid color-mix(in srgb, var(--brand) 35%, #fff 65%);
  outline-offset: 1px;
  border-color: var(--brand);
}

.product-filter-reset {
  white-space: nowrap;
  padding: 0.44rem 0.82rem;
}

.product-filter-summary {
  margin: 0;
  color: var(--muted);
  font-size: 0.88rem;
}

.rec-search-label {
  display: block;
  margin-bottom: 0.35rem;
  font-size: 0.86rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 700;
}

.rec-empty {
  margin: 0.7rem 0 0.4rem;
  color: var(--muted);
  font-size: 0.95rem;
}

.rec-groups {
  margin-top: 0.85rem;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(245px, 1fr));
  gap: 0.95rem;
}

.rec-group {
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 0.8rem 0.85rem;
  background: #fff;
}

.rec-group h2 {
  margin: 0 0 0.55rem;
  font-size: 0.85rem;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  color: var(--muted);
}

.rec-links {
  display: grid;
  gap: 0.4rem;
}

.rec-links a {
  display: block;
  color: var(--ink);
  text-decoration: none;
  font-size: 0.95rem;
}

.rec-links a:hover {
  color: var(--brand);
}

.spec-list {
  margin: 0.5rem 0 1rem;
  padding-left: 1.1rem;
}

.spec-list li {
  margin-bottom: 0.2rem;
}

.btn {
  display: inline-block;
  border: 1px solid var(--brand);
  border-radius: 999px;
  background: var(--brand-soft);
  color: var(--brand);
  font-weight: 700;
  text-decoration: none;
  padding: 0.42rem 0.95rem;
}

.btn:hover {
  background: var(--brand);
  color: #fff;
  text-decoration: none;
}

.btn-large {
  padding: 0.62rem 1.1rem;
}

.btn.ghost {
  border-color: var(--line);
  background: #fff;
  color: var(--ink);
}

.site-footer {
  border-top: 1px solid var(--line);
  padding: 1.5rem 0 2.5rem;
  color: var(--muted);
  background: color-mix(in srgb, var(--bg) 70%, white 30%);
}

.footer-grid {
  display: grid;
  gap: 0.35rem;
}

@media (max-width: 980px) {
  .article-page {
    grid-template-columns: 1fr;
    grid-template-areas:
      "header"
      "media"
      "body"
      "actions";
  }

  .article-page > .featured {
    aspect-ratio: 1 / 1;
  }

  .article-card,
  .article-card.is-featured {
    grid-template-columns: clamp(160px, 30vw, 212px) minmax(0, 1fr);
  }

  .product-filter-controls {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .product-filter-field.is-search {
    grid-column: 1 / -1;
  }

  .product-filter-reset {
    justify-self: start;
  }

  .recommendation-page .recommendation-products .rec-product-grid {
    grid-template-columns: 1fr;
  }

  .recommendation-page .recommendation-products .rec-product-card {
    grid-template-columns: 116px 1fr;
    padding: 0.82rem;
    gap: 0.54rem 0.78rem;
  }

  .recommendation-page .recommendation-products .rec-product-card figure {
    width: 116px;
    height: 116px;
  }
}

@media (max-width: 760px) {
  .topbar {
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    padding: 0.7rem 0;
  }

  .header-tools {
    width: 100%;
    flex-direction: column;
    align-items: stretch;
    gap: 0.65rem;
  }

  .main-nav {
    gap: 0.85rem;
    width: 100%;
  }

  .site-search {
    width: 100%;
    min-width: 0;
  }

  .section-head {
    align-items: flex-start;
  }

  .hero-home {
    grid-template-columns: 1fr;
  }

  .hero-panel {
    padding: 0.75rem 0.8rem;
  }

  .mega-menu {
    display: none !important;
  }

  .nav-dropdown > a::after {
    content: "";
  }

  main.shell {
    padding-top: 1.2rem;
  }

  .review-article {
    grid-template-columns: 1fr;
    grid-template-areas:
      "header"
      "media"
      "body";
  }

  .product-card {
    grid-template-columns: 92px 1fr;
  }

  .product-card figure {
    width: 92px;
    height: 92px;
    min-height: 92px;
  }

  .article-page {
    grid-template-columns: 1fr;
    grid-template-areas:
      "header"
      "media"
      "body"
      "actions";
  }

  .article-header h1 {
    font-size: clamp(1.5rem, 5.3vw, 2rem);
  }

  .article-grid,
  .article-grid-home {
    grid-template-columns: 1fr;
  }

  .article-card,
  .article-card.is-featured {
    grid-template-columns: 108px 1fr;
    padding: 0.72rem;
    gap: 0.5rem 0.72rem;
  }

  .article-card figure,
  .article-card.is-featured figure {
    width: 108px;
    height: 108px;
    min-height: 108px;
  }

  .article-card:not(.has-media) {
    grid-template-columns: 1fr;
  }

  .article-actions {
    align-items: flex-start;
  }

  .recommendation-links {
    gap: 0.35rem 0.55rem;
  }

  .breadcrumbs {
    font-size: 0.78rem;
  }

  .related-article-grid {
    grid-template-columns: 1fr;
  }

  .related-article-card {
    grid-template-columns: 96px 1fr;
    gap: 0.46rem 0.64rem;
    padding: 0.64rem;
  }

  .related-article-card figure {
    width: 96px;
    height: 96px;
  }

  .rec-product-grid {
    grid-template-columns: 1fr;
  }

  .rec-product-card {
    grid-template-columns: 92px 1fr;
    padding: 0.68rem;
    gap: 0.48rem 0.68rem;
  }

  .rec-product-card figure {
    width: 92px;
    height: 92px;
  }

  .recommendation-page .recommendation-products .rec-product-card {
    grid-template-columns: 104px 1fr;
    padding: 0.72rem;
    gap: 0.5rem 0.72rem;
  }

  .recommendation-page .recommendation-products .rec-product-card figure {
    width: 104px;
    height: 104px;
  }

  .recommendation-page .recommendation-products .rec-product-copy h3 {
    font-size: 0.96rem;
  }

  .product-filter-controls {
    grid-template-columns: 1fr;
  }

  .product-filter-field.is-search {
    grid-column: auto;
  }

  .product-filter-reset {
    justify-self: start;
  }

  .rec-groups {
    grid-template-columns: 1fr;
  }

  .rec-search-input {
    width: 100%;
  }
}
"""
    write_text(ASSETS_DIR / "styles.css", styles)

    # Build report
    report = {
        "published_items": len(published),
        "product_posts": len(woo_products),
        "product_detail_pages": len(review_entries),
        "sitemap_urls": len(unique_sitemap),
        "front_page_id": page_on_front,
        "logo_url": logo_url,
        "site_icon_url": site_icon_url,
    }
    write_text(OUT_ROOT / "build-report.json", json.dumps(report, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUT_ROOT)
    local_uploads = OUT_ROOT / "assets/uploads"
    parser.add_argument("--uploads-dir", type=Path, default=local_uploads if local_uploads.exists() else UPLOADS_PATH)
    args = parser.parse_args()
    OUT_ROOT = args.output_dir.resolve()
    ASSETS_DIR = OUT_ROOT / "assets"
    UPLOADS_OUT = ASSETS_DIR / "uploads"
    UPLOADS_PATH = args.uploads_dir.resolve()
    main()
