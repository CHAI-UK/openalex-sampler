#!/usr/bin/env python3
"""Fetch a reproducible random sample of papers from OpenAlex."""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


OPENALEX_BASE_URL = "https://api.openalex.org"
SUPPORTED_FIELDS = {"title", "abstract", "publication_date", "publication_venue"}
OPENALEX_SELECT_FIELDS = {
    "title": "display_name",
    "abstract": "abstract_inverted_index",
    "publication_date": "publication_date",
    "publication_venue": ["primary_location", "locations"],
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LATEX_DOCUMENT_PATTERNS = (
    "\\documentclass",
    "\\begin{document}",
    "\\end{document}",
    "\\usepackage",
    "\\newtheorem",
    "\\pagestyle",
    "\\fancyhead",
    "\\fancyfoot",
)

JsonObject = dict[str, Any]
FetchJson = Callable[[str, dict[str, Any]], JsonObject]
SeedGenerator = Callable[[], int]


class ConfigError(ValueError):
    """Raised when the fetcher config is invalid."""


class OpenAlexError(RuntimeError):
    """Raised when an OpenAlex request or response cannot be handled."""


@dataclass(frozen=True)
class FetchConfig:
    count: int
    from_publication_date: str
    to_publication_date: str | None
    work_type: str
    language: str
    domain: str
    field: str
    subfield: str
    topic: str
    field_match: str
    sample_seed: int | None
    required_fields: list[str]
    fields: list[str]
    output_path: str
    per_page: int
    require_english_text: bool
    require_clean_text: bool
    mailto: str | None = None


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def load_config(
    path: str | Path,
    *,
    max_count: int | None = 10_000,
) -> FetchConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Could not read config file {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {config_path} is not valid JSON: {exc}") from exc

    required_keys = {
        "count",
        "from_publication_date",
        "to_publication_date",
        "work_type",
        "language",
        "domain",
        "field",
        "subfield",
        "topic",
        "field_match",
        "sample_seed",
        "required_fields",
        "fields",
        "output_path",
        "per_page",
    }
    missing = sorted(required_keys - raw.keys())
    if missing:
        raise ConfigError(f"Missing required config keys: {', '.join(missing)}")

    config = FetchConfig(
        count=raw["count"],
        from_publication_date=raw["from_publication_date"],
        to_publication_date=raw["to_publication_date"],
        work_type=raw["work_type"],
        language=raw["language"],
        domain=raw["domain"],
        field=raw["field"],
        subfield=raw["subfield"],
        topic=raw["topic"],
        field_match=raw["field_match"],
        sample_seed=raw["sample_seed"],
        required_fields=list(raw["required_fields"]),
        fields=list(raw["fields"]),
        output_path=raw["output_path"],
        per_page=raw["per_page"],
        require_english_text=raw.get("require_english_text", raw["language"] == "en"),
        require_clean_text=raw.get("require_clean_text", True),
        mailto=raw.get("mailto"),
    )
    validate_config(config, max_count=max_count)
    return config


def validate_config(config: FetchConfig, *, max_count: int | None = 10_000) -> None:
    if not isinstance(config.count, int) or config.count <= 0:
        raise ConfigError("count must be a positive integer")
    if max_count is not None and config.count > max_count:
        raise ConfigError(
            f"count must be {max_count:,} or lower because OpenAlex sample is limited "
            f"to {max_count:,}"
        )
    if not isinstance(config.per_page, int) or not 1 <= config.per_page <= 100:
        raise ConfigError("per_page must be an integer between 1 and 100")
    if config.sample_seed is not None and not isinstance(config.sample_seed, int):
        raise ConfigError("sample_seed must be an integer or null")
    if not _is_date(config.from_publication_date):
        raise ConfigError("from_publication_date must use YYYY-MM-DD format")
    if config.to_publication_date is not None and not _is_date(config.to_publication_date):
        raise ConfigError("to_publication_date must be null or use YYYY-MM-DD format")
    if config.field_match != "primary":
        raise ConfigError("field_match must be 'primary' in this version")
    if not config.work_type:
        raise ConfigError("work_type must be a non-empty string")
    if not isinstance(config.language, str) or not config.language:
        raise ConfigError("language must be a non-empty string, for example 'en'")
    if not isinstance(config.require_english_text, bool):
        raise ConfigError("require_english_text must be true or false")
    if not isinstance(config.require_clean_text, bool):
        raise ConfigError("require_clean_text must be true or false")
    if not config.domain:
        raise ConfigError("domain must be a non-empty string")
    if not config.field:
        raise ConfigError("field must be a non-empty string")
    if not config.subfield:
        raise ConfigError("subfield must be a non-empty string")
    if not config.topic:
        raise ConfigError("topic must be a non-empty string")
    if not config.fields:
        raise ConfigError("fields must contain at least one field")

    unsupported_fields = sorted(set(config.fields) - SUPPORTED_FIELDS)
    if unsupported_fields:
        raise ConfigError(f"Unsupported output fields: {', '.join(unsupported_fields)}")

    unsupported_required = sorted(set(config.required_fields) - SUPPORTED_FIELDS)
    if unsupported_required:
        raise ConfigError(f"Unsupported required fields: {', '.join(unsupported_required)}")

    missing_required_outputs = sorted(set(config.required_fields) - set(config.fields))
    if missing_required_outputs:
        raise ConfigError(
            "required_fields must also be present in fields: "
            + ", ".join(missing_required_outputs)
        )


def _is_date(value: Any) -> bool:
    return isinstance(value, str) and bool(DATE_RE.match(value))


def fetch_json(url: str, params: dict[str, Any]) -> JsonObject:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    request_url = f"{url}?{query}" if query else url
    request = Request(request_url, headers={"User-Agent": "arcadia-openalex-fetcher/1.0"})
    try:
        with urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise OpenAlexError(f"OpenAlex HTTP error {exc.code} for {request_url}") from exc
    except URLError as exc:
        raise OpenAlexError(f"OpenAlex request failed for {request_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise OpenAlexError(f"OpenAlex request timed out for {request_url}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise OpenAlexError(f"OpenAlex returned invalid JSON for {request_url}: {exc}") from exc
    if not isinstance(data, dict):
        raise OpenAlexError(f"OpenAlex returned unexpected JSON shape for {request_url}")
    return data


def resolve_field_id(
    field_name: str,
    fetcher: FetchJson = fetch_json,
    mailto: str | None = None,
) -> tuple[str, str]:
    params: dict[str, Any] = {
        "search": field_name,
        "per_page": 25,
        "select": "id,display_name",
    }
    if mailto:
        params["mailto"] = mailto

    data = fetcher(f"{OPENALEX_BASE_URL}/fields", params)
    results = data.get("results")
    if not isinstance(results, list):
        raise OpenAlexError("OpenAlex /fields response did not contain a results list")

    matches = [
        item
        for item in results
        if isinstance(item, dict)
        and isinstance(item.get("display_name"), str)
        and item["display_name"].casefold() == field_name.casefold()
        and isinstance(item.get("id"), str)
    ]
    if not matches:
        raise OpenAlexError(f"No exact OpenAlex Field match found for {field_name!r}")
    if len(matches) > 1:
        raise OpenAlexError(f"Multiple exact OpenAlex Field matches found for {field_name!r}")

    return matches[0]["id"], matches[0]["display_name"]


def resolve_topic_hierarchy(
    config: FetchConfig,
    fetcher: FetchJson = fetch_json,
) -> JsonObject:
    params: dict[str, Any] = {
        "search": config.topic,
        "per_page": 25,
    }
    if config.mailto:
        params["mailto"] = config.mailto

    data = fetcher(f"{OPENALEX_BASE_URL}/topics", params)
    results = data.get("results")
    if not isinstance(results, list):
        raise OpenAlexError("OpenAlex /topics response did not contain a results list")

    matches = [
        item
        for item in results
        if isinstance(item, dict)
        and _matches_name(item, "display_name", config.topic)
        and _matches_nested_name(item, "domain", config.domain)
        and _matches_nested_name(item, "field", config.field)
        and _matches_nested_name(item, "subfield", config.subfield)
        and isinstance(item.get("id"), str)
    ]
    if not matches:
        raise OpenAlexError(
            "No exact OpenAlex Topic match found for "
            f"{config.domain} > {config.field} > {config.subfield} > {config.topic}"
        )
    if len(matches) > 1:
        raise OpenAlexError(f"Multiple exact OpenAlex Topic matches found for {config.topic!r}")

    topic = matches[0]
    return {
        "domain": _topic_level(topic, "domain"),
        "field": _topic_level(topic, "field"),
        "subfield": _topic_level(topic, "subfield"),
        "topic": {
            "id": topic["id"],
            "display_name": topic["display_name"],
        },
    }


def _matches_name(item: JsonObject, key: str, expected_name: str) -> bool:
    value = item.get(key)
    return isinstance(value, str) and value.casefold() == expected_name.casefold()


def _matches_nested_name(item: JsonObject, key: str, expected_name: str) -> bool:
    value = item.get(key)
    return (
        isinstance(value, dict)
        and isinstance(value.get("display_name"), str)
        and value["display_name"].casefold() == expected_name.casefold()
    )


def _topic_level(topic: JsonObject, key: str) -> JsonObject:
    value = topic.get(key)
    if not isinstance(value, dict):
        raise OpenAlexError(f"OpenAlex Topic response did not contain {key} metadata")
    level_id = value.get("id")
    display_name = value.get("display_name")
    if not isinstance(level_id, str) or not isinstance(display_name, str):
        raise OpenAlexError(f"OpenAlex Topic {key} metadata is missing id or display_name")
    return {"id": level_id, "display_name": display_name}


def reconstruct_abstract(inverted_index: Any) -> str | None:
    if not isinstance(inverted_index, dict) or not inverted_index:
        return None

    max_position = -1
    for positions in inverted_index.values():
        if not isinstance(positions, list):
            return None
        for position in positions:
            if not isinstance(position, int) or position < 0:
                return None
            max_position = max(max_position, position)

    if max_position < 0:
        return None

    words: list[str | None] = [None] * (max_position + 1)
    for word, positions in inverted_index.items():
        if not isinstance(word, str):
            return None
        for position in positions:
            words[position] = word

    if any(word is None for word in words):
        return None
    return " ".join(word for word in words if word is not None)


def looks_like_english(text: str) -> bool:
    if _contains_cjk_text(text):
        return False

    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False

    latin_letters = [char for char in letters if _is_latin_letter(char)]
    return len(latin_letters) / len(letters) >= 0.85


def _contains_cjk_text(text: str) -> bool:
    return any(
        "\u3040" <= char <= "\u30ff"
        or "\u3400" <= char <= "\u4dbf"
        or "\u4e00" <= char <= "\u9fff"
        or "\uac00" <= char <= "\ud7af"
        for char in text
    )


def _is_latin_letter(char: str) -> bool:
    return (
        "A" <= char <= "Z"
        or "a" <= char <= "z"
        or "\u00c0" <= char <= "\u024f"
        or "\u1e00" <= char <= "\u1eff"
    )


def looks_like_clean_abstract(text: str) -> bool:
    lower_text = html.unescape(text).lower()
    if any(pattern in lower_text for pattern in LATEX_DOCUMENT_PATTERNS):
        return False
    if lower_text.count("<br>") > 5:
        return False
    if len(re.findall(r"<[a-z][^>]*>", lower_text)) > 5:
        return False
    if len(re.findall(r"\\[a-zA-Z]+", text)) > 12:
        return False
    return True


def clean_text(text: str) -> str:
    cleaned = html.unescape(text)
    cleaned = re.sub(r"<br\s*/?>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?(?:div|span|p|section|article)[^>]*>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</?[a-z][^>]*>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def parse_paper(work: JsonObject, config: FetchConfig) -> tuple[JsonObject | None, list[str]]:
    parsed: JsonObject = {}
    skip_reasons: list[str] = []

    if "title" in config.fields:
        title = work.get("display_name")
        if isinstance(title, str) and title.strip():
            parsed["title"] = clean_text(title)
        elif "title" in config.required_fields:
            skip_reasons.append("missing_title")

    if "abstract" in config.fields:
        abstract = reconstruct_abstract(work.get("abstract_inverted_index"))
        if abstract:
            parsed["abstract"] = clean_text(abstract)
        elif "abstract" in config.required_fields:
            skip_reasons.append("missing_abstract")

    if "publication_date" in config.fields:
        publication_date = work.get("publication_date")
        if isinstance(publication_date, str) and publication_date.strip():
            parsed["publication_date"] = publication_date
        elif "publication_date" in config.required_fields:
            skip_reasons.append("missing_publication_date")
        else:
            parsed["publication_date"] = None

    if "publication_venue" in config.fields:
        publication_venue = parse_publication_venue(work)
        if publication_venue:
            parsed["publication_venue"] = publication_venue
        elif "publication_venue" in config.required_fields:
            skip_reasons.append("missing_publication_venue")
        else:
            parsed["publication_venue"] = None

    if config.require_english_text and config.language == "en":
        for field in ("title", "abstract"):
            value = parsed.get(field)
            if isinstance(value, str) and not looks_like_english(value):
                skip_reasons.append(f"non_english_{field}")

    if config.require_clean_text:
        abstract = parsed.get("abstract")
        if isinstance(abstract, str) and not looks_like_clean_abstract(abstract):
            skip_reasons.append("unclean_abstract")

    if skip_reasons:
        return None, skip_reasons
    return parsed, []


def parse_publication_venue(work: JsonObject) -> str | None:
    primary_location = work.get("primary_location")
    if isinstance(primary_location, dict):
        venue = parse_location_venue(primary_location)
        if venue:
            return venue

    locations = work.get("locations")
    if isinstance(locations, list):
        for location in locations:
            if isinstance(location, dict):
                venue = parse_location_venue(location)
                if venue:
                    return venue
    return None


def parse_location_venue(location: JsonObject) -> str | None:
    source = location.get("source")
    if isinstance(source, dict):
        display_name = source.get("display_name")
        if isinstance(display_name, str) and display_name.strip():
            return clean_text(display_name)

    raw_source_name = location.get("raw_source_name")
    if isinstance(raw_source_name, str) and raw_source_name.strip():
        return clean_text(raw_source_name)
    return None


def build_work_filters(config: FetchConfig, topic_hierarchy: JsonObject) -> str:
    topic = topic_hierarchy.get("topic")
    if not isinstance(topic, dict) or not isinstance(topic.get("id"), str):
        raise OpenAlexError("Resolved topic hierarchy is missing topic id")
    topic_filter_id = id_for_filter(topic["id"])
    filters = [
        f"from_publication_date:{config.from_publication_date}",
        f"type:{config.work_type}",
        f"language:{config.language}",
        "has_abstract:true",
        f"primary_topic.id:{topic_filter_id}",
    ]
    if config.to_publication_date:
        filters.append(f"to_publication_date:{config.to_publication_date}")
    return ",".join(filters)


def field_id_for_filter(field_id: str) -> str:
    return id_for_filter(field_id)


def id_for_filter(openalex_id: str) -> str:
    return openalex_id.rstrip("/").rsplit("/", 1)[-1]


def build_select(fields: list[str]) -> str:
    openalex_fields: list[str] = []
    for field in fields:
        selected_fields = OPENALEX_SELECT_FIELDS[field]
        if isinstance(selected_fields, list):
            openalex_fields.extend(selected_fields)
        else:
            openalex_fields.append(selected_fields)
    return ",".join(dict.fromkeys(openalex_fields))


def generate_sample_seed() -> int:
    return secrets.randbelow(2_147_483_647)


def fetch_papers(
    config: FetchConfig,
    topic_hierarchy: JsonObject,
    fetcher: FetchJson = fetch_json,
    seed_generator: SeedGenerator = generate_sample_seed,
) -> tuple[list[JsonObject], JsonObject]:
    filters = build_work_filters(config, topic_hierarchy)
    select = build_select(config.fields)
    papers: list[JsonObject] = []
    sample_size = min(10_000, max(config.count, config.count * 5))
    effective_sample_seed = (
        config.sample_seed if config.sample_seed is not None else seed_generator()
    )
    skipped = 0
    page = 1
    total_seen = 0
    total_available: int | None = None

    logging.info("Effective filters: %s", filters)
    logging.info("Random sample size=%s seed=%s", sample_size, effective_sample_seed)

    while len(papers) < config.count:
        params: dict[str, Any] = {
            "filter": filters,
            "sample": sample_size,
            "seed": effective_sample_seed,
            "page": page,
            "per_page": config.per_page,
            "select": select,
        }
        if config.mailto:
            params["mailto"] = config.mailto

        logging.info("Requesting works page %s with per_page=%s", page, config.per_page)
        data = fetcher(f"{OPENALEX_BASE_URL}/works", params)
        results = data.get("results")
        if not isinstance(results, list):
            raise OpenAlexError("OpenAlex /works response did not contain a results list")

        meta = data.get("meta")
        if isinstance(meta, dict) and isinstance(meta.get("count"), int):
            total_available = meta["count"]

        if not results:
            logging.info("No more sampled results returned on page %s", page)
            break

        total_seen += len(results)
        for work in results:
            if not isinstance(work, dict):
                skipped += 1
                logging.warning("Skipping non-object work result on page %s", page)
                continue
            paper, missing = parse_paper(work, config)
            if paper is None:
                skipped += 1
                logging.warning(
                    "Skipping work: %s",
                    ", ".join(missing),
                )
                continue
            papers.append(paper)
            if len(papers) >= config.count:
                break

        logging.info(
            "Page %s complete: accepted=%s skipped=%s sampled_seen=%s",
            page,
            len(papers),
            skipped,
            total_seen,
        )

        if page * config.per_page >= sample_size:
            break
        page += 1

    stats: JsonObject = {
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
    return papers, stats


def write_output(
    output_path: str | Path,
    config: FetchConfig,
    topic_hierarchy: JsonObject,
    papers: list[JsonObject],
    stats: JsonObject,
) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "OpenAlex",
            "config": {
                "count": config.count,
                "from_publication_date": config.from_publication_date,
                "to_publication_date": config.to_publication_date,
                "work_type": config.work_type,
                "language": config.language,
                "domain": config.domain,
                "field": config.field,
                "subfield": config.subfield,
                "topic": config.topic,
                "field_match": config.field_match,
                "sample_seed": config.sample_seed,
                "required_fields": config.required_fields,
                "fields": config.fields,
                "per_page": config.per_page,
                "require_english_text": config.require_english_text,
                "require_clean_text": config.require_clean_text,
                "mailto": config.mailto,
            },
            "resolved_topic_hierarchy": topic_hierarchy,
            "query": stats,
        },
        "papers": papers,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch papers from OpenAlex into JSON.")
    parser.add_argument(
        "--config",
        default="openalex_config.json",
        help="Path to the JSON config file.",
    )
    parser.add_argument(
        "--output",
        help="Optional output JSON path overriding config.output_path.",
    )
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv or sys.argv[1:])

    try:
        config = load_config(args.config)
        output_path = args.output or config.output_path
        logging.info("Loaded config from %s", args.config)

        topic_hierarchy = resolve_topic_hierarchy(config)
        logging.info(
            "Resolved topic hierarchy: %s > %s > %s > %s (%s)",
            topic_hierarchy["domain"]["display_name"],
            topic_hierarchy["field"]["display_name"],
            topic_hierarchy["subfield"]["display_name"],
            topic_hierarchy["topic"]["display_name"],
            topic_hierarchy["topic"]["id"],
        )

        papers, stats = fetch_papers(config, topic_hierarchy)
        logging.info("Writing %s papers to %s", len(papers), output_path)
        write_output(output_path, config, topic_hierarchy, papers, stats)
        logging.info(
            "Done: requested=%s written=%s skipped=%s output=%s",
            stats["requested"],
            stats["written"],
            stats["skipped"],
            output_path,
        )
        return 0
    except (ConfigError, OpenAlexError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
