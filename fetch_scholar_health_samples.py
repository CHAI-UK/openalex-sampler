#!/usr/bin/env python3
"""Fetch Google Scholar h5-core health papers and OpenAlex topic samples."""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from fetch_openalex_papers import (
    FetchConfig,
    FetchJson,
    JsonObject,
    OPENALEX_BASE_URL,
    OpenAlexError,
    build_select,
    clean_text,
    generate_sample_seed,
    get_openalex_api_key,
    id_for_filter,
    parse_paper,
    parse_publication_venue,
    setup_logging,
)


SCHOLAR_HEALTH_URL = "https://scholar.google.com/citations"
SCHOLAR_HEALTH_PARAMS = {
    "view_op": "top_venues",
    "hl": "en",
    "vq": "med",
}
TOP_HEALTH_JOURNALS = [
    "The New England Journal of Medicine",
    "The Lancet",
    "Cell",
    "JAMA",
    "Nature Medicine",
]
PAPER_FIELDS = ["title", "abstract", "publication_date", "publication_venue"]
PARENT_SELECT = (
    "id,doi,type,display_name,abstract_inverted_index,publication_date,"
    "primary_location,locations,primary_topic,referenced_works,cited_by_count"
)
REFERENCE_SELECT = "id,display_name,abstract_inverted_index,publication_date,primary_location,locations"
SLUG_MAX_LENGTH = 80
OPENALEX_MAX_RETRIES = 6
OPENALEX_RETRY_BASE_DELAY_SECONDS = 5.0
OPENALEX_MAX_RETRY_DELAY_SECONDS = 60.0


@dataclass(frozen=True)
class ScholarArticle:
    title: str
    year: int | None = None


@dataclass(frozen=True)
class ScholarJournal:
    name: str
    h5_index: int | None
    h5_median: int | None
    h5_core_url: str | None
    articles: list[ScholarArticle]


@dataclass(frozen=True)
class ScholarInput:
    source_method: str
    journals: list[ScholarJournal]
    scrape_failures: list[str]


@dataclass(frozen=True)
class OpenAlexMatch:
    status: str
    confidence: float
    work: JsonObject | None
    reason: str | None
    candidates: list[JsonObject]


def fetch_text(url: str, params: dict[str, Any] | None = None) -> str:
    query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
    request_url = f"{url}?{query}" if query else url
    request = Request(
        request_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 openalex-sampler/1.0 "
                "(compatible; research data collection)"
            )
        },
    )
    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise OpenAlexError(f"Google Scholar HTTP error {exc.code} for {request_url}") from exc
    except URLError as exc:
        raise OpenAlexError(f"Google Scholar request failed for {request_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OpenAlexError(f"Google Scholar request timed out for {request_url}") from exc


def fetch_openalex_json_with_retries(
    url: str,
    params: dict[str, Any],
    *,
    max_retries: int = OPENALEX_MAX_RETRIES,
    base_delay_seconds: float = OPENALEX_RETRY_BASE_DELAY_SECONDS,
    max_delay_seconds: float = OPENALEX_MAX_RETRY_DELAY_SECONDS,
    sleeper: Callable[[float], None] = time.sleep,
    fetch_once: Callable[[str, dict[str, Any]], JsonObject] | None = None,
) -> JsonObject:
    fetch_once = fetch_once or fetch_openalex_json_once
    for attempt in range(max_retries + 1):
        try:
            return fetch_once(url, params)
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= max_retries:
                raise _openalex_http_error(url, params, exc) from exc
            delay = _retry_delay(exc, attempt, base_delay_seconds)
            if delay > max_delay_seconds:
                raise _openalex_rate_limit_error(url, params, exc, delay, max_delay_seconds) from exc
            logging.warning(
                "OpenAlex HTTP %s; retrying in %.1fs (%s/%s)",
                exc.code,
                delay,
                attempt + 1,
                max_retries,
            )
            sleeper(delay)
        except (URLError, TimeoutError) as exc:
            if attempt >= max_retries:
                raise _openalex_request_error(url, params, exc) from exc
            delay = min(base_delay_seconds * (2**attempt), max_delay_seconds)
            logging.warning(
                "OpenAlex request failed; retrying in %.1fs (%s/%s): %s",
                delay,
                attempt + 1,
                max_retries,
                exc,
            )
            sleeper(delay)
    raise OpenAlexError("OpenAlex retry loop ended unexpectedly")


def fetch_openalex_json_once(url: str, params: dict[str, Any]) -> JsonObject:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request_url = f"{url}?{query}" if query else url
    safe_request_url = _request_url(url, params, redact_api_key=True)
    request = Request(request_url, headers={"User-Agent": "arcadia-openalex-fetcher/1.0"})
    with urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenAlexError(f"OpenAlex returned invalid JSON for {safe_request_url}: {exc}") from exc
    if not isinstance(data, dict):
        raise OpenAlexError(f"OpenAlex returned unexpected JSON shape for {safe_request_url}")
    return data


def _openalex_http_error(url: str, params: dict[str, Any], exc: HTTPError) -> OpenAlexError:
    request_url = _request_url(url, params, redact_api_key=True)
    return OpenAlexError(f"OpenAlex HTTP error {exc.code} for {request_url}")


def _openalex_request_error(url: str, params: dict[str, Any], exc: URLError | TimeoutError) -> OpenAlexError:
    request_url = _request_url(url, params, redact_api_key=True)
    reason = exc.reason if isinstance(exc, URLError) else exc
    return OpenAlexError(f"OpenAlex request failed for {request_url}: {reason}")


def _openalex_rate_limit_error(
    url: str,
    params: dict[str, Any],
    exc: HTTPError,
    requested_delay: float,
    max_delay: float,
) -> OpenAlexError:
    request_url = _request_url(url, params, redact_api_key=True)
    return OpenAlexError(
        "OpenAlex rate limit requested a long wait "
        f"({requested_delay:.0f}s, max configured {max_delay:.0f}s) for {request_url}. "
        "Try again later or set OPENALEX_API_KEY to a free OpenAlex API key "
        "for a higher daily limit."
    )


def _request_url(
    url: str,
    params: dict[str, Any],
    *,
    redact_api_key: bool = False,
) -> str:
    display_params = (
        {**params, "api_key": "REDACTED"}
        if redact_api_key and params.get("api_key")
        else params
    )
    query = urlencode({key: value for key, value in display_params.items() if value is not None})
    return f"{url}?{query}" if query else url


def _retry_delay(exc: HTTPError, attempt: int, base_delay_seconds: float) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return max(float(retry_after), 0.0)
        except ValueError:
            pass
    return base_delay_seconds * (2**attempt)


class _ScholarTopVenuesParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[dict[str, str | None]]] = []
        self._current_row: list[dict[str, str | None]] | None = None
        self._current_cell: dict[str, str | None] | None = None
        self._in_anchor = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = {"text": "", "href": None}
        elif tag == "a" and self._current_cell is not None:
            self._in_anchor = True
            href = attrs_dict.get("href")
            if href:
                self._current_cell["href"] = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_anchor = False
        elif tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_cell["text"] = re.sub(r"\s+", " ", self._current_cell["text"] or "").strip()
            self._current_row.append(self._current_cell)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell["text"] = (self._current_cell["text"] or "") + data


class _ScholarH5CoreParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            text = re.sub(r"\s+", " ", "".join(self._current_cell)).strip()
            self._current_row.append(text)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)


def load_scholar_input(
    fixture_path: str | Path | None,
    *,
    fetcher: Callable[[str, dict[str, Any] | None], str] = fetch_text,
) -> ScholarInput:
    failures: list[str] = []
    try:
        journals = scrape_scholar_health_journals(fetcher=fetcher)
        if journals:
            return ScholarInput("google_scholar", journals, failures)
    except OpenAlexError as exc:
        failures.append(str(exc))

    if fixture_path is None:
        raise OpenAlexError(
            "Could not scrape Google Scholar and no --scholar-fixture was provided"
        )

    return ScholarInput("fixture", load_scholar_fixture(fixture_path), failures)


def scrape_scholar_health_journals(
    *,
    fetcher: Callable[[str, dict[str, Any] | None], str] = fetch_text,
    max_journals: int = 5,
    articles_per_journal: int = 20,
) -> list[ScholarJournal]:
    html = fetcher(SCHOLAR_HEALTH_URL, SCHOLAR_HEALTH_PARAMS)
    parser = _ScholarTopVenuesParser()
    parser.feed(html)
    journals: list[ScholarJournal] = []

    for row in parser.rows:
        row_text = " ".join(str(cell.get("text") or "") for cell in row)
        if not any(name in row_text for name in TOP_HEALTH_JOURNALS):
            continue
        name = next(name for name in TOP_HEALTH_JOURNALS if name in row_text)
        numbers = [int(value) for value in re.findall(r"\b\d{2,4}\b", row_text)]
        href = next((cell.get("href") for cell in row if cell.get("href")), None)
        h5_index = numbers[0] if numbers else None
        h5_median = numbers[1] if len(numbers) > 1 else None
        h5_core_url = urljoin(SCHOLAR_HEALTH_URL, href) if isinstance(href, str) else None
        articles = scrape_h5_core_articles(h5_core_url, fetcher, articles_per_journal) if h5_core_url else []
        journals.append(
            ScholarJournal(
                name=name,
                h5_index=h5_index,
                h5_median=h5_median,
                h5_core_url=h5_core_url,
                articles=articles,
            )
        )
        if len(journals) >= max_journals:
            break

    if len(journals) < max_journals:
        raise OpenAlexError(f"Only found {len(journals)} Google Scholar journals")
    return journals


def scrape_h5_core_articles(
    url: str,
    fetcher: Callable[[str, dict[str, Any] | None], str],
    limit: int,
) -> list[ScholarArticle]:
    html = fetcher(url, None)
    parser = _ScholarH5CoreParser()
    parser.feed(html)
    articles: list[ScholarArticle] = []
    for row in parser.rows:
        text_cells = [cell for cell in row if cell]
        if not text_cells:
            continue
        title = clean_text(text_cells[0])
        title = clean_scholar_article_title(title)
        if not title or title.lower() in {"article", "title", "title / author"}:
            continue
        year = _first_year(" ".join(text_cells[1:]))
        articles.append(ScholarArticle(title=title, year=year))
        if len(articles) >= limit:
            break
    if len(articles) < limit:
        raise OpenAlexError(f"Only found {len(articles)} h5-core articles for {url}")
    return articles


def load_scholar_fixture(path: str | Path) -> list[ScholarJournal]:
    fixture_path = Path(path)
    if fixture_path.suffix.lower() == ".csv":
        return _load_scholar_csv_fixture(fixture_path)
    return _load_scholar_json_fixture(fixture_path)


def _load_scholar_json_fixture(path: Path) -> list[ScholarJournal]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw_journals = raw.get("journals") if isinstance(raw, dict) else raw
    if not isinstance(raw_journals, list):
        raise OpenAlexError("Scholar JSON fixture must contain a journals list")

    journals: list[ScholarJournal] = []
    for item in raw_journals:
        if not isinstance(item, dict):
            raise OpenAlexError("Each journal fixture entry must be an object")
        name = _required_str(item, "name")
        raw_articles = item.get("articles", item.get("papers", []))
        if not isinstance(raw_articles, list):
            raise OpenAlexError(f"Journal {name!r} must contain an articles list")
        articles = [_article_from_fixture(value) for value in raw_articles]
        journals.append(
            ScholarJournal(
                name=name,
                h5_index=_optional_int(item.get("h5_index")),
                h5_median=_optional_int(item.get("h5_median")),
                h5_core_url=item.get("h5_core_url") if isinstance(item.get("h5_core_url"), str) else None,
                articles=articles[:20],
            )
        )
    return journals[:5]


def _load_scholar_csv_fixture(path: Path) -> list[ScholarJournal]:
    journals_by_name: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row.get("journal") or row.get("journal_name") or row.get("name")
            title = row.get("title") or row.get("paper_title")
            if not name or not title:
                raise OpenAlexError("CSV fixture rows require journal/name and title columns")
            journal = journals_by_name.setdefault(
                name,
                {
                    "h5_index": _optional_int(row.get("h5_index")),
                    "h5_median": _optional_int(row.get("h5_median")),
                    "articles": [],
                },
            )
            journal["articles"].append(ScholarArticle(title=title, year=_optional_int(row.get("year"))))

    return [
        ScholarJournal(
            name=name,
            h5_index=value["h5_index"],
            h5_median=value["h5_median"],
            h5_core_url=None,
            articles=value["articles"][:20],
        )
        for name, value in list(journals_by_name.items())[:5]
    ]


def _article_from_fixture(value: Any) -> ScholarArticle:
    if isinstance(value, str):
        return ScholarArticle(title=value)
    if not isinstance(value, dict):
        raise OpenAlexError("Article fixture entries must be strings or objects")
    return ScholarArticle(title=_required_str(value, "title"), year=_optional_int(value.get("year")))


def _required_str(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OpenAlexError(f"Fixture entry is missing non-empty {key!r}")
    return value.strip()


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_year(text: str) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", text)
    return int(match.group(0)) if match else None


def clean_scholar_article_title(value: str) -> str:
    title = clean_text(value)
    if title.casefold() == "title / author":
        return ""

    author_match = re.search(
        r"(?=[A-Z]{1,4}\s+(?:[a-z]{2,4}\s+)?[A-Z][A-Za-z'’.-]+,\s+[A-Z]{1,4}\s+)",
        title,
    )
    if author_match and author_match.start() >= 12:
        title = title[: author_match.start()]

    for journal in TOP_HEALTH_JOURNALS:
        match = re.search(
            rf"{re.escape(journal)}\s+\d{{1,4}}\s*\(",
            title,
            flags=re.IGNORECASE,
        )
        if match and match.start() > 0:
            title = title[: match.start()]
            break

    trailing_single_author = re.search(
        r"[A-Z]{1,4}\s+(?:[a-z]{2,4}\s+)?[A-Z][A-Za-z'’.-]+$",
        title,
    )
    if trailing_single_author and trailing_single_author.start() >= 12:
        title = title[: trailing_single_author.start()]

    title = strip_appended_author_tail(title)
    title = re.sub(r"\blong COV$", "long COVID", title)
    return title.strip(" .,-")


def strip_appended_author_tail(title: str) -> str:
    match = re.search(
        r"(?P<tail>[A-Z]{1,4}\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){0,3})$",
        title,
    )
    if not match or match.start("tail") < 12:
        return title

    prefix = title[: match.start("tail")]
    if not prefix or not (prefix[-1].islower() or prefix[-1].isdigit()):
        return title
    return prefix


def normalize_title(title: str) -> str:
    normalized = clean_text(title).casefold()
    normalized = re.sub(r"[\W_]+", " ", normalized)
    normalized = re.sub(r"\b(the|a|an)\b", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def title_similarity(left: str, right: str) -> float:
    normalized_left = normalize_title(left)
    normalized_right = normalize_title(right)
    if not normalized_left or not normalized_right:
        return 0.0
    return SequenceMatcher(None, normalized_left, normalized_right).ratio()


def title_match_score(scholar_title: str, openalex_title: str) -> float:
    score = title_similarity(scholar_title, openalex_title)
    main_title = scholar_title.split(":", 1)[0].strip()
    if main_title != scholar_title and normalize_title(main_title) == normalize_title(openalex_title):
        score = 1.0
    return score


def same_venue(expected_journal: str, work: JsonObject) -> bool:
    venue = parse_publication_venue(work)
    if not venue:
        return False
    return normalize_title(expected_journal) == normalize_title(venue)


def score_openalex_candidate(article: ScholarArticle, journal: ScholarJournal, work: JsonObject) -> float:
    title = work.get("display_name")
    if not isinstance(title, str):
        return 0.0
    score = title_match_score(article.title, title) * 0.78
    if same_venue(journal.name, work):
        score += 0.17
    if article.year is not None:
        publication_date = work.get("publication_date")
        if isinstance(publication_date, str) and publication_date.startswith(str(article.year)):
            score += 0.05
    return min(score, 1.0)


def resolve_parent_paper(
    article: ScholarArticle,
    journal: ScholarJournal,
    *,
    fetcher: FetchJson = fetch_openalex_json_with_retries,
    min_confidence: float = 0.86,
    api_key: str | None = None,
) -> OpenAlexMatch:
    candidates_by_id: dict[str, JsonObject] = {}
    for search_title in openalex_search_titles(article.title):
        params: dict[str, Any] = {
            "search": search_title,
            "filter": "language:en",
            "per_page": 10,
            "select": PARENT_SELECT,
        }
        if api_key:
            params["api_key"] = api_key
        data = fetcher(f"{OPENALEX_BASE_URL}/works", params)
        results = data.get("results")
        if not isinstance(results, list):
            raise OpenAlexError("OpenAlex /works search response did not contain a results list")
        for work in results:
            if not isinstance(work, dict):
                continue
            key = work.get("id") if isinstance(work.get("id"), str) else json.dumps(work, sort_keys=True)
            candidates_by_id.setdefault(key, work)

    candidates = list(candidates_by_id.values())
    scored = [
        {
            "id": work.get("id"),
            "title": work.get("display_name"),
            "publication_date": work.get("publication_date"),
            "publication_venue": parse_publication_venue(work),
            "type": work.get("type"),
            "confidence": round(score_openalex_candidate(article, journal, work), 4),
            "cited_by_count": work.get("cited_by_count"),
        }
        for work in candidates
    ]
    if not candidates:
        return OpenAlexMatch("unresolved", 0.0, None, "no_openalex_candidates", scored)

    best = max(
        candidates,
        key=lambda work: (
            score_openalex_candidate(article, journal, work),
            work.get("cited_by_count") if isinstance(work.get("cited_by_count"), int) else -1,
        ),
    )
    confidence = score_openalex_candidate(article, journal, best)
    if confidence < min_confidence:
        return OpenAlexMatch(
            "unresolved",
            confidence,
            None,
            "low_confidence_match",
            sorted(scored, key=lambda item: item["confidence"], reverse=True),
        )
    return OpenAlexMatch(
        "resolved",
        confidence,
        best,
        None,
        sorted(scored, key=lambda item: item["confidence"], reverse=True),
    )


def openalex_search_titles(title: str) -> list[str]:
    titles = [title]
    main_title = title.split(":", 1)[0].strip()
    if main_title and main_title != title:
        titles.append(main_title)
    return list(dict.fromkeys(titles))


def parent_record(
    work: JsonObject,
    article: ScholarArticle,
    journal: ScholarJournal,
    confidence: float,
) -> JsonObject | None:
    parsed, reasons = parse_paper(work, _parent_config())
    if parsed is None:
        logging.warning("Skipping parent %s: %s", work.get("id"), ", ".join(reasons))
        return None
    for field in PAPER_FIELDS:
        parsed.setdefault(field, None)
    parsed.update(
        {
            "openalex_id": work.get("id"),
            "doi": work.get("doi"),
            "type": work.get("type"),
            "matched_scholar_title": article.title,
            "journal": journal.name,
            "primary_topic": work.get("primary_topic"),
            "match_confidence": round(confidence, 4),
        }
    )
    return parsed


def reference_ids(work: JsonObject) -> list[str]:
    values = work.get("referenced_works")
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str) and value.strip()]


def fetch_references(
    ids: list[str],
    *,
    fetcher: FetchJson = fetch_openalex_json_with_retries,
    per_batch: int = 100,
    api_key: str | None = None,
) -> tuple[list[JsonObject], JsonObject]:
    references: list[JsonObject] = []
    found = 0
    skipped = 0
    skip_reasons: dict[str, int] = {}
    unique_ids = list(dict.fromkeys(ids))

    for start in range(0, len(unique_ids), per_batch):
        batch = unique_ids[start : start + per_batch]
        filter_ids = "|".join(id_for_filter(value) for value in batch)
        params: dict[str, Any] = {
            "filter": f"openalex:{filter_ids}",
            "per_page": len(batch),
            "select": REFERENCE_SELECT,
        }
        if api_key:
            params["api_key"] = api_key
        data = fetcher(f"{OPENALEX_BASE_URL}/works", params)
        results = data.get("results")
        if not isinstance(results, list):
            raise OpenAlexError("OpenAlex reference response did not contain a results list")
        found += len(results)
        for work in results:
            if not isinstance(work, dict):
                skipped += 1
                continue
            parsed, reasons = parse_paper(work, _base_config())
            if parsed is None:
                skipped += 1
                for reason in reasons:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                logging.warning("Skipping reference: %s", ", ".join(reasons))
                continue
            references.append(parsed)

    stats = {
        "requested": len(unique_ids),
        "found": found,
        "accepted": len(references),
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "missing": max(len(unique_ids) - found, 0),
    }
    return references, stats


def five_year_start_date(publication_date: str) -> str:
    parsed = date.fromisoformat(publication_date)
    return date(parsed.year - 5, 1, 1).isoformat()


def topic_hierarchy_from_parent(work: JsonObject) -> JsonObject | None:
    topic = work.get("primary_topic")
    if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
        return None
    return {
        "domain": _topic_level_or_unknown(topic.get("domain")),
        "field": _topic_level_or_unknown(topic.get("field")),
        "subfield": _topic_level_or_unknown(topic.get("subfield")),
        "topic": {"id": topic["id"], "display_name": topic.get("display_name")},
    }


def _topic_level_or_unknown(value: Any) -> JsonObject:
    if isinstance(value, dict):
        return {"id": value.get("id"), "display_name": value.get("display_name")}
    return {"id": None, "display_name": None}


def sample_papers_before_parent(
    parent_work: JsonObject,
    *,
    count: int,
    sample_seed: int | None,
    per_page: int,
    fetcher: FetchJson = fetch_openalex_json_with_retries,
    seed_generator: Callable[[], int] = generate_sample_seed,
    api_key: str | None = None,
) -> tuple[list[JsonObject], JsonObject]:
    publication_date = parent_work.get("publication_date")
    if not isinstance(publication_date, str):
        raise OpenAlexError("Parent work is missing publication_date")
    topic_hierarchy = topic_hierarchy_from_parent(parent_work)
    if topic_hierarchy is None:
        raise OpenAlexError("Parent work is missing primary_topic")

    config = FetchConfig(
        count=count,
        from_publication_date=five_year_start_date(publication_date),
        to_publication_date=publication_date,
        work_type="article",
        language="en",
        domain=str(topic_hierarchy["domain"].get("display_name") or ""),
        field=str(topic_hierarchy["field"].get("display_name") or ""),
        subfield=str(topic_hierarchy["subfield"].get("display_name") or ""),
        topic=str(topic_hierarchy["topic"].get("display_name") or ""),
        field_match="primary",
        sample_seed=sample_seed,
        required_fields=["title", "abstract"],
        fields=PAPER_FIELDS,
        output_path="",
        per_page=per_page,
        require_english_text=True,
        require_clean_text=True,
        api_key=api_key,
    )
    return fetch_topic_sample(config, topic_hierarchy, fetcher=fetcher, seed_generator=seed_generator)


def fetch_topic_sample(
    config: FetchConfig,
    topic_hierarchy: JsonObject,
    *,
    fetcher: FetchJson = fetch_openalex_json_with_retries,
    seed_generator: Callable[[], int] = generate_sample_seed,
) -> tuple[list[JsonObject], JsonObject]:
    topic = topic_hierarchy.get("topic")
    if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
        raise OpenAlexError("Resolved topic hierarchy is missing topic id")
    filters = build_parent_topic_filters(config, topic["id"])
    select = build_select(config.fields)
    papers: list[JsonObject] = []
    sample_size = min(10_000, max(config.count, config.count * 5))
    effective_sample_seed = config.sample_seed if config.sample_seed is not None else seed_generator()
    skipped = 0
    page = 1
    total_seen = 0
    total_available: int | None = None

    while len(papers) < config.count:
        params: dict[str, Any] = {
            "filter": filters,
            "sample": sample_size,
            "seed": effective_sample_seed,
            "page": page,
            "per_page": config.per_page,
            "select": select,
        }
        if config.api_key:
            params["api_key"] = config.api_key
        data = fetcher(f"{OPENALEX_BASE_URL}/works", params)
        results = data.get("results")
        if not isinstance(results, list):
            raise OpenAlexError("OpenAlex /works response did not contain a results list")
        meta = data.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("count"), int):
            total_available = meta["count"]
        if not results:
            break
        total_seen += len(results)
        for work in results:
            if not isinstance(work, dict):
                skipped += 1
                continue
            parsed, reasons = parse_paper(work, config)
            if parsed is None:
                skipped += 1
                logging.warning("Skipping sampled work: %s", ", ".join(reasons))
                continue
            papers.append(parsed)
            if len(papers) >= config.count:
                break
        if page * config.per_page >= sample_size:
            break
        page += 1

    return papers, {
        "requested": config.count,
        "sample_size": sample_size,
        "configured_sample_seed": config.sample_seed,
        "effective_sample_seed": effective_sample_seed,
        "written": len(papers),
        "skipped": skipped,
        "sampled_seen": total_seen,
        "total_matching_openalex_count": total_available,
        "filters": filters,
        "select": select,
    }


def build_parent_topic_filters(config: FetchConfig, topic_id: str) -> str:
    filters = [
        f"from_publication_date:{config.from_publication_date}",
        f"type:{config.work_type}",
        f"language:{config.language}",
        "has_abstract:true",
        f"primary_topic.id:{id_for_filter(topic_id)}",
    ]
    if config.to_publication_date:
        filters.append(f"to_publication_date:{config.to_publication_date}")
    return ",".join(filters)


def slugify(title: str, used: set[str] | None = None) -> str:
    slug = normalize_title(title)
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")[:SLUG_MAX_LENGTH].strip("-")
    if not slug:
        slug = "paper"
    if used is None:
        return slug
    candidate = slug
    suffix = 2
    while candidate in used:
        suffix_text = f"-{suffix}"
        candidate = f"{slug[: SLUG_MAX_LENGTH - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used.add(candidate)
    return candidate


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_workflow(
    scholar_input: ScholarInput,
    *,
    output_root: Path,
    count: int,
    sample_seed: int | None,
    per_page: int,
    fetcher: FetchJson = fetch_openalex_json_with_retries,
    seed_generator: Callable[[], int] = generate_sample_seed,
    api_key: str | None = None,
) -> JsonObject:
    used_slugs: set[str] = set()
    manifest: JsonObject = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scholar_source_method": scholar_input.source_method,
        "scrape_failures": scholar_input.scrape_failures,
        "journals": [journal_to_manifest(journal) for journal in scholar_input.journals],
        "papers": [],
        "unresolved": [],
    }

    for journal in scholar_input.journals:
        for raw_article in journal.articles[:20]:
            cleaned_title = clean_scholar_article_title(raw_article.title)
            if not cleaned_title:
                entry = {
                    "journal": journal.name,
                    "scholar_title": raw_article.title,
                    "scholar_year": raw_article.year,
                    "status": "unresolved",
                    "match_confidence": 0.0,
                    "reason": "empty_cleaned_scholar_title",
                    "candidates": [],
                }
                manifest["unresolved"].append(entry)
                manifest["papers"].append(entry)
                continue
            article = ScholarArticle(title=cleaned_title, year=raw_article.year)
            logging.info("Resolving %s / %s", journal.name, article.title)
            match = resolve_parent_paper(article, journal, fetcher=fetcher, api_key=api_key)
            entry: JsonObject = {
                "journal": journal.name,
                "scholar_title": article.title,
                "raw_scholar_title": (
                    raw_article.title if raw_article.title != article.title else None
                ),
                "scholar_year": article.year,
                "status": match.status,
                "match_confidence": round(match.confidence, 4),
                "reason": match.reason,
                "candidates": match.candidates,
            }
            if match.work is None:
                manifest["unresolved"].append(entry)
                manifest["papers"].append(entry)
                continue
            parent = parent_record(match.work, article, journal, match.confidence)
            if parent is None:
                entry["status"] = "unresolved"
                entry["reason"] = "missing_required_parent_fields"
                manifest["unresolved"].append(entry)
                manifest["papers"].append(entry)
                continue
            topic_hierarchy = topic_hierarchy_from_parent(match.work)
            if topic_hierarchy is None:
                entry["status"] = "unresolved"
                entry["reason"] = "missing_primary_topic"
                manifest["unresolved"].append(entry)
                manifest["papers"].append(entry)
                continue

            slug = slugify(parent["title"], used_slugs)
            folder = output_root / slug
            references, reference_stats = fetch_references(reference_ids(match.work), fetcher=fetcher, api_key=api_key)
            try:
                sampled, sample_stats = sample_papers_before_parent(
                    match.work,
                    count=count,
                    sample_seed=sample_seed,
                    per_page=per_page,
                    fetcher=fetcher,
                    seed_generator=seed_generator,
                    api_key=api_key,
                )
            except OpenAlexError as exc:
                sampled = []
                sample_stats = {"error": str(exc), "requested": count, "written": 0}

            write_json(folder / f"{slug}.json", parent)
            write_json(folder / f"papers_before_{slug}.json", sampled)
            write_json(folder / f"references_{slug}.json", references)

            entry.update(
                {
                    "status": "resolved",
                    "openalex_id": parent.get("openalex_id"),
                    "doi": parent.get("doi"),
                    "parent_title": parent.get("title"),
                    "publication_date": parent.get("publication_date"),
                    "publication_venue": parent.get("publication_venue"),
                    "primary_topic": topic_hierarchy["topic"],
                    "output_folder": str(folder),
                    "sample_stats": sample_stats,
                    "reference_stats": reference_stats,
                }
            )
            manifest["papers"].append(entry)

    write_json(output_root / "manifest.json", manifest)
    return manifest


def journal_to_manifest(journal: ScholarJournal) -> JsonObject:
    return {
        "name": journal.name,
        "h5_index": journal.h5_index,
        "h5_median": journal.h5_median,
        "h5_core_url": journal.h5_core_url,
        "articles": [
            {"title": article.title, "year": article.year}
            for article in journal.articles[:20]
        ],
    }


def _base_config() -> FetchConfig:
    return FetchConfig(
        count=1,
        from_publication_date="1900-01-01",
        to_publication_date=None,
        work_type="article",
        language="en",
        domain="",
        field="",
        subfield="",
        topic="",
        field_match="primary",
        sample_seed=None,
        required_fields=["title", "abstract"],
        fields=PAPER_FIELDS,
        output_path="",
        per_page=100,
        require_english_text=True,
        require_clean_text=True,
        api_key=None,
    )


def _parent_config() -> FetchConfig:
    config = _base_config()
    return FetchConfig(
        count=config.count,
        from_publication_date=config.from_publication_date,
        to_publication_date=config.to_publication_date,
        work_type=config.work_type,
        language=config.language,
        domain=config.domain,
        field=config.field,
        subfield=config.subfield,
        topic=config.topic,
        field_match=config.field_match,
        sample_seed=config.sample_seed,
        required_fields=["title"],
        fields=config.fields,
        output_path=config.output_path,
        per_page=config.per_page,
        require_english_text=config.require_english_text,
        require_clean_text=config.require_clean_text,
        api_key=config.api_key,
    )


def default_output_root() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path("output") / "scholar_health_samples" / stamp


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch Google Scholar health h5-core papers and OpenAlex topic samples."
    )
    parser.add_argument("--scholar-fixture", help="JSON or CSV fallback with journals and h5-core titles.")
    parser.add_argument("--output-root", default=None, help="Output directory. Defaults to timestamped output/.")
    parser.add_argument("--count", type=int, default=2000, help="Sample papers per resolved parent paper.")
    parser.add_argument("--sample-seed", type=int, default=None, help="Optional OpenAlex sample seed.")
    parser.add_argument("--per-page", type=int, default=100, help="OpenAlex page size, max 100.")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv or sys.argv[1:])
    if args.count <= 0:
        logging.error("--count must be positive")
        return 1
    if not 1 <= args.per_page <= 100:
        logging.error("--per-page must be between 1 and 100")
        return 1

    output_root = Path(args.output_root) if args.output_root else default_output_root()
    try:
        scholar_input = load_scholar_input(args.scholar_fixture)
        manifest = run_workflow(
            scholar_input,
            output_root=output_root,
            count=args.count,
            sample_seed=args.sample_seed,
            per_page=args.per_page,
            api_key=get_openalex_api_key(),
        )
        logging.info(
            "Done: resolved=%s unresolved=%s manifest=%s",
            len([item for item in manifest["papers"] if item["status"] == "resolved"]),
            len(manifest["unresolved"]),
            output_root / "manifest.json",
        )
        return 0
    except (OpenAlexError, OSError, json.JSONDecodeError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
