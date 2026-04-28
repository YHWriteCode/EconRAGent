from __future__ import annotations

from html import unescape
import re
from typing import Any
from urllib.parse import (
    parse_qs,
    parse_qsl,
    quote_plus,
    unquote,
    urlencode,
    urlparse,
    urlunparse,
)


def extract_markdown_text(
    markdown_payload: Any,
    *,
    prefer_fit_markdown: bool = True,
) -> str:
    if markdown_payload is None:
        return ""
    fit_markdown = (
        getattr(markdown_payload, "fit_markdown", None)
        if prefer_fit_markdown
        else None
    )
    if isinstance(fit_markdown, str) and fit_markdown.strip():
        return fit_markdown.strip()
    if isinstance(markdown_payload, str):
        return markdown_payload.strip()
    raw_markdown = getattr(markdown_payload, "raw_markdown", None)
    if isinstance(raw_markdown, str):
        return raw_markdown.strip()
    markdown = getattr(markdown_payload, "markdown", None)
    if isinstance(markdown, str):
        return markdown.strip()
    return str(markdown_payload).strip()


def build_excerpt(text: str, max_chars: int = 300) -> str:
    normalized = " ".join((text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def normalize_links(links: Any) -> list[str]:
    if links is None:
        return []
    if isinstance(links, list):
        return [str(item) for item in links if item]
    if isinstance(links, dict):
        normalized: list[str] = []
        for value in links.values():
            if isinstance(value, list):
                normalized.extend(str(item) for item in value if item)
            elif value:
                normalized.append(str(value))
        return normalized
    return [str(links)]


def sanitize_plain_text(text: str) -> str:
    return unescape((text or "").strip())


SEARCH_RESULT_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
TOKEN_PATTERN = re.compile(r"[a-zA-Z0-9]{2,}")

BLOCKED_RESULT_DOMAINS = {
    "duckduckgo.com",
    "external-content.duckduckgo.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "reddit.com",
    "linkedin.com",
    "instagram.com",
    "tiktok.com",
    "pinterest.com",
    "youtube.com",
    "m.youtube.com",
}

LOW_QUALITY_RESULT_DOMAINS = {
    "einnews.com",
}

# ── Source credibility tiers ──
# tier-1: authoritative encyclopedias & official government media  (+8)
# tier-2: mainstream news agencies & well-known financial media    (+5)
# tier-3: respected industry / institutional sources              (+3)
TRUSTED_DOMAINS: dict[str, int] = {
    # Tier-1 — encyclopedias & official media
    "wikipedia.org": 8,
    "en.wikipedia.org": 8,
    "zh.wikipedia.org": 8,
    "baike.baidu.com": 8,
    "people.com.cn": 8,
    "people.cn": 8,
    "xinhuanet.com": 8,
    "news.xinhuanet.com": 8,
    "xinhua.net": 8,
    "gov.cn": 8,
    "www.gov.cn": 8,
    "cctv.com": 8,
    "news.cn": 8,
    # Tier-2 — mainstream news & financial media
    "reuters.com": 5,
    "apnews.com": 5,
    "bbc.com": 5,
    "bbc.co.uk": 5,
    "nytimes.com": 5,
    "wsj.com": 5,
    "ft.com": 5,
    "economist.com": 5,
    "bloomberg.com": 5,
    "cnbc.com": 5,
    "theguardian.com": 5,
    "washingtonpost.com": 5,
    "chinadaily.com.cn": 5,
    "globaltimes.cn": 5,
    "caixin.com": 5,
    "yicai.com": 5,
    "jiemian.com": 5,
    "thepaper.cn": 5,
    "infzm.com": 5,
    "21jingji.com": 5,
    "stcn.com": 5,
    "cs.com.cn": 5,
    "nbd.com.cn": 5,
    # Tier-3 — institutional & industry sources
    "nature.com": 3,
    "science.org": 3,
    "sciencedirect.com": 3,
    "springer.com": 3,
    "imf.org": 3,
    "worldbank.org": 3,
    "oecd.org": 3,
    "un.org": 3,
    "who.int": 3,
    "stats.gov.cn": 3,
    "pbc.gov.cn": 3,
    "mof.gov.cn": 3,
    "cnki.net": 3,
    "ssrn.com": 3,
    "arxiv.org": 3,
    "britannica.com": 3,
}

GENERIC_PATH_SEGMENTS = {
    "tag",
    "tags",
    "topic",
    "topics",
    "category",
    "categories",
    "archive",
    "archives",
    "news",
    "news-list",
    "blog",
    "blogs",
    "search",
    "author",
}

TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "ref",
    "source",
    "src",
    "rut",
}

GENERIC_TITLE_PHRASES = {
    "latest news and updates",
    "news and updates",
    "official blog",
    "blog |",
    "press release",
}


def build_search_url(engine: str, query: str) -> str:
    normalized_engine = (engine or "duckduckgo").strip().lower()
    encoded_query = quote_plus(query or "")
    if normalized_engine == "duckduckgo":
        return f"https://duckduckgo.com/html/?q={encoded_query}"
    raise ValueError(f"Unsupported search engine for URL discovery: {engine}")


def _decode_search_redirect(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path == "/l/":
        query = parse_qs(parsed.query)
        uddg = query.get("uddg", [])
        if uddg:
            return unquote(uddg[0])
    return url


def _is_tracking_query_key(key: str) -> bool:
    normalized_key = (key or "").strip().lower()
    return bool(normalized_key) and (
        normalized_key.startswith("utm_") or normalized_key in TRACKING_QUERY_KEYS
    )


def canonicalize_url(url: str) -> str:
    normalized_url = (url or "").strip()
    if not normalized_url:
        return ""

    parsed = urlparse(normalized_url)
    if not parsed.scheme or not parsed.netloc:
        return normalized_url

    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").strip().lower().strip(".")
    if not hostname:
        return normalized_url

    netloc = hostname
    if parsed.username:
        credentials = parsed.username
        if parsed.password:
            credentials = f"{credentials}:{parsed.password}"
        netloc = f"{credentials}@{netloc}"

    default_port = (scheme == "http" and parsed.port == 80) or (
        scheme == "https" and parsed.port == 443
    )
    if parsed.port and not default_port:
        netloc = f"{netloc}:{parsed.port}"

    normalized_path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not normalized_path.startswith("/"):
        normalized_path = f"/{normalized_path}"
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")

    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_tracking_query_key(key)
    ]
    return urlunparse(
        (
            scheme,
            netloc,
            normalized_path,
            parsed.params,
            urlencode(filtered_query, doseq=True),
            "",
        )
    )


def _normalize_result_url(url: str) -> str:
    return canonicalize_url(url)


def _tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_PATTERN.findall(text or "")}


def _path_segments(url: str) -> list[str]:
    parsed = urlparse(url)
    return [segment.lower() for segment in parsed.path.split("/") if segment]


def _is_blocked_result_domain(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(netloc == domain or netloc.endswith(f".{domain}") for domain in BLOCKED_RESULT_DOMAINS)


def _is_low_quality_result_domain(url: str) -> bool:
    netloc = urlparse(url).netloc.lower()
    return any(
        netloc == domain or netloc.endswith(f".{domain}")
        for domain in LOW_QUALITY_RESULT_DOMAINS
    )


def _trusted_domain_bonus(url: str) -> int:
    """Return credibility bonus for a URL based on TRUSTED_DOMAINS tiers."""
    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc_no_www = netloc[4:]
    else:
        netloc_no_www = netloc
    # Exact match first (e.g. "en.wikipedia.org")
    bonus = TRUSTED_DOMAINS.get(netloc, 0) or TRUSTED_DOMAINS.get(netloc_no_www, 0)
    if bonus:
        return bonus
    # Suffix match (e.g. "economy.people.com.cn" → "people.com.cn")
    for domain, tier_bonus in TRUSTED_DOMAINS.items():
        if netloc.endswith(f".{domain}") or netloc_no_www.endswith(f".{domain}"):
            return tier_bonus
    return 0


def _score_discovered_result(query: str, title: str, url: str) -> tuple[float, dict[str, Any]]:
    query_tokens = _tokenize(query)
    title_tokens = _tokenize(title)
    url_tokens = _tokenize(url.replace("-", " "))
    matched_title_tokens = query_tokens & title_tokens
    matched_url_tokens = query_tokens & url_tokens

    score = float(len(matched_title_tokens) * 3 + len(matched_url_tokens))
    segments = _path_segments(url)
    article_like = False

    if re.search(r"/20\d{2}/\d{2}/\d{2}/", url):
        score += 4
        article_like = True

    if segments:
        last_segment = segments[-1]
        if len(segments) >= 2 and "-" in last_segment and len(last_segment) >= 12:
            score += 3
            article_like = True
        if len(segments) == 1 and "-" not in last_segment and len(last_segment) <= 24:
            score -= 4
        if last_segment in GENERIC_PATH_SEGMENTS:
            score -= 5
        if any(segment in GENERIC_PATH_SEGMENTS for segment in segments[:-1]):
            score -= 2
    else:
        score -= 4

    netloc = urlparse(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    domain_root = netloc.split(".")[0] if netloc else ""
    if domain_root and domain_root in query_tokens:
        score += 3

    title_lower = title.lower()
    if any(token in title_lower for token in ("latest", "news", "update", "policy", "report")):
        score += 1
    if any(phrase in title_lower for phrase in GENERIC_TITLE_PHRASES):
        score -= 3

    # Source credibility bonus
    credibility_bonus = _trusted_domain_bonus(url)
    score += credibility_bonus

    return score, {
        "match_count": len(matched_title_tokens) + len(matched_url_tokens),
        "matched_title_tokens": sorted(matched_title_tokens),
        "matched_url_tokens": sorted(matched_url_tokens),
        "article_like": article_like,
        "credibility_bonus": credibility_bonus,
    }


def _is_generic_listing_result(title: str, url: str, *, article_like: bool) -> bool:
    if article_like:
        return False

    title_lower = (title or "").strip().lower()
    segments = _path_segments(url)
    if any(phrase in title_lower for phrase in GENERIC_TITLE_PHRASES):
        return True
    if title_lower.endswith("latest news") or title_lower.endswith("latest updates"):
        return True
    if segments:
        last_segment = segments[-1]
        if last_segment in GENERIC_PATH_SEGMENTS:
            return True
        if len(segments) <= 2 and any(segment in GENERIC_PATH_SEGMENTS for segment in segments):
            return True
    return False


def extract_search_results_from_markdown(
    markdown: str,
    *,
    query: str = "",
    top_k: int = 5,
    per_domain_limit: int = 2,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for title, raw_url in SEARCH_RESULT_LINK_PATTERN.findall(markdown or ""):
        decoded_url = _normalize_result_url(_decode_search_redirect(raw_url).strip())
        if not decoded_url.startswith("http"):
            continue
        parsed = urlparse(decoded_url)
        if not parsed.netloc:
            continue
        if _is_blocked_result_domain(decoded_url):
            continue
        normalized_title = sanitize_plain_text(title)
        if decoded_url in seen:
            continue
        seen.add(decoded_url)
        score, signals = _score_discovered_result(query, normalized_title, decoded_url)
        generic_listing = _is_generic_listing_result(
            normalized_title,
            decoded_url,
            article_like=bool(signals.get("article_like", False)),
        )
        credibility_bonus = signals.get("credibility_bonus", 0)
        results.append(
            {
                "title": normalized_title,
                "url": decoded_url,
                "domain": parsed.netloc.lower(),
                "score": score,
                "low_quality_domain": _is_low_quality_result_domain(decoded_url),
                "generic_listing": generic_listing,
                "trusted_source": credibility_bonus > 0,
                **signals,
            }
        )

    results.sort(
        key=lambda item: (
            -item["score"],
            not item.get("trusted_source", False),
            item["low_quality_domain"],
            item["generic_listing"],
            not item["article_like"],
            -item["match_count"],
            item["url"],
        )
    )

    def _public_item(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": item["title"],
            "url": item["url"],
            "score": item["score"],
            "match_count": item["match_count"],
            "article_like": item["article_like"],
            "trusted_source": item.get("trusted_source", False),
            "credibility_bonus": item.get("credibility_bonus", 0),
        }

    primary_candidates = [
        item
        for item in results
        if not item["low_quality_domain"]
        and not item["generic_listing"]
        and (item.get("trusted_source") or item["article_like"] or item["match_count"] >= 2)
    ]
    fallback_candidates = results

    selected: list[dict[str, Any]] = []
    selected_urls: set[str] = set()
    domain_counts: dict[str, int] = {}

    def _append_from(candidates: list[dict[str, Any]]) -> None:
        for item in candidates:
            if len(selected) >= top_k:
                return
            if item["url"] in selected_urls:
                continue
            domain = item["domain"]
            if domain_counts.get(domain, 0) >= max(1, per_domain_limit):
                continue
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            selected_urls.add(item["url"])
            selected.append(_public_item(item))

    _append_from(primary_candidates)
    _append_from(fallback_candidates)
    return selected
