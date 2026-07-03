#!/usr/bin/env python3
"""Fetch Medicine papers for every OpenAlex Topic into Subfield folders."""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fetch_openalex_papers import (
    OPENALEX_BASE_URL,
    FetchConfig,
    JsonObject,
    OpenAlexError,
    fetch_json,
    fetch_papers,
    setup_logging,
    write_output,
)


MEDICINE_FIELD_ID = "27"
MEDICINE_DOMAIN = "Health Sciences"
MEDICINE_FIELD = "Medicine"
MEDICINE_OUTPUT_ROOT = Path("output/medicine_2005_present")
MEDICINE_OUTPUT_FIELDS = ["title", "abstract", "publication_date", "publication_venue"]
REQUESTED_PAPERS_PER_TOPIC = 2000
TOPIC_PAGE_SIZE = 200

TopicDiscoverer = Callable[[Callable[[str, dict[str, Any]], JsonObject]], list[JsonObject]]
PaperFetcher = Callable[[FetchConfig, JsonObject], tuple[list[JsonObject], JsonObject]]


def discover_medicine_topics(
    fetcher: Callable[[str, dict[str, Any]], JsonObject] = fetch_json,
    mailto: str | None = None,
) -> list[JsonObject]:
    topics: list[JsonObject] = []
    cursor: str | None = "*"

    while cursor:
        params: dict[str, Any] = {
            "filter": f"field.id:{MEDICINE_FIELD_ID}",
            "per_page": TOPIC_PAGE_SIZE,
            "cursor": cursor,
            "select": "id,display_name,domain,field,subfield",
        }
        if mailto:
            params["mailto"] = mailto

        data = fetcher(f"{OPENALEX_BASE_URL}/topics", params)
        results = data.get("results")
        if not isinstance(results, list):
            raise OpenAlexError("OpenAlex /topics response did not contain a results list")

        for item in results:
            if not isinstance(item, dict):
                raise OpenAlexError("OpenAlex /topics response contained a non-object topic")
            validate_topic(item)
            topics.append(item)

        meta = data.get("meta")
        if not isinstance(meta, dict):
            break
        next_cursor = meta.get("next_cursor")
        cursor = next_cursor if isinstance(next_cursor, str) and results else None

    topics.sort(
        key=lambda item: (
            topic_level(item, "subfield")["display_name"].casefold(),
            item["display_name"].casefold(),
        )
    )
    return topics


def validate_topic(topic: JsonObject) -> None:
    if not isinstance(topic.get("id"), str) or not isinstance(topic.get("display_name"), str):
        raise OpenAlexError("OpenAlex Topic metadata is missing id or display_name")
    for key in ("domain", "field", "subfield"):
        topic_level(topic, key)


def topic_level(topic: JsonObject, key: str) -> JsonObject:
    value = topic.get(key)
    if not isinstance(value, dict):
        raise OpenAlexError(f"OpenAlex Topic response did not contain {key} metadata")
    level_id = value.get("id")
    display_name = value.get("display_name")
    if not isinstance(level_id, str) or not isinstance(display_name, str):
        raise OpenAlexError(f"OpenAlex Topic {key} metadata is missing id or display_name")
    return {"id": level_id, "display_name": display_name}


def safe_path_name(name: str) -> str:
    cleaned = re.sub(r"[/\\]+", "_", name)
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.rstrip(".")
    return cleaned or "unnamed"


def output_path_for_topic(topic: JsonObject, output_root: Path) -> Path:
    subfield_name = topic_level(topic, "subfield")["display_name"]
    topic_name = topic["display_name"]
    return output_root / safe_path_name(subfield_name) / f"{safe_path_name(topic_name)}.json"


def build_medicine_config(
    topic: JsonObject,
    output_root: Path,
    mailto: str | None = None,
    sample_seed: int | None = None,
) -> FetchConfig:
    subfield_name = topic_level(topic, "subfield")["display_name"]
    output_path = output_path_for_topic(topic, output_root)
    return FetchConfig(
        count=REQUESTED_PAPERS_PER_TOPIC,
        from_publication_date="2005-01-01",
        to_publication_date=None,
        work_type="article",
        language="en",
        domain=MEDICINE_DOMAIN,
        field=MEDICINE_FIELD,
        subfield=subfield_name,
        topic=topic["display_name"],
        field_match="primary",
        sample_seed=sample_seed,
        required_fields=["title", "abstract"],
        fields=list(MEDICINE_OUTPUT_FIELDS),
        output_path=str(output_path),
        per_page=100,
        require_english_text=True,
        require_clean_text=True,
        mailto=mailto,
    )


def build_topic_hierarchy(topic: JsonObject) -> JsonObject:
    return {
        "domain": topic_level(topic, "domain"),
        "field": topic_level(topic, "field"),
        "subfield": topic_level(topic, "subfield"),
        "topic": {
            "id": topic["id"],
            "display_name": topic["display_name"],
        },
    }


def run_batch(
    output_root: Path = MEDICINE_OUTPUT_ROOT,
    overwrite: bool = False,
    mailto: str | None = None,
    sample_seed: int | None = None,
    discoverer: TopicDiscoverer = discover_medicine_topics,
    paper_fetcher: PaperFetcher = fetch_papers,
    fetcher: Callable[[str, dict[str, Any]], JsonObject] = fetch_json,
) -> JsonObject:
    topics = discoverer(fetcher)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_entries: list[JsonObject] = []
    used_paths: set[Path] = set()

    for index, topic in enumerate(topics, start=1):
        config = build_medicine_config(topic, output_root, mailto=mailto, sample_seed=sample_seed)
        hierarchy = build_topic_hierarchy(topic)
        output_path = unique_output_path(Path(config.output_path), topic, used_paths)
        used_paths.add(output_path)
        if str(output_path) != config.output_path:
            config = FetchConfig(**{**config.__dict__, "output_path": str(output_path)})

        logging.info(
            "[%s/%s] %s > %s",
            index,
            len(topics),
            config.subfield,
            config.topic,
        )

        if output_path.exists() and not overwrite:
            manifest_entries.append(skipped_existing_entry(config, hierarchy, output_path))
            write_manifest(output_root, topics, manifest_entries, overwrite)
            continue

        try:
            papers, stats = paper_fetcher(config, hierarchy)
            write_output(output_path, config, hierarchy, papers, stats)
            written = int(stats.get("written", len(papers)))
            status = "written" if written >= config.count else "partial"
            manifest_entries.append(manifest_entry(config, hierarchy, output_path, status, stats))
        except Exception as exc:
            logging.error("Failed %s > %s: %s", config.subfield, config.topic, exc)
            manifest_entries.append(
                manifest_entry(
                    config,
                    hierarchy,
                    output_path,
                    "error",
                    {
                        "requested": config.count,
                        "written": 0,
                        "skipped": 0,
                    },
                    error=str(exc),
                )
            )

        write_manifest(output_root, topics, manifest_entries, overwrite)

    manifest = build_manifest(output_root, topics, manifest_entries, overwrite)
    write_manifest(output_root, topics, manifest_entries, overwrite)
    return manifest


def unique_output_path(path: Path, topic: JsonObject, used_paths: set[Path]) -> Path:
    if path not in used_paths:
        return path
    topic_id = topic["id"].rstrip("/").rsplit("/", 1)[-1]
    return path.with_name(f"{path.stem} ({safe_path_name(topic_id)}){path.suffix}")


def skipped_existing_entry(config: FetchConfig, hierarchy: JsonObject, output_path: Path) -> JsonObject:
    written = 0
    skipped = 0
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        papers = payload.get("papers")
        if isinstance(papers, list):
            written = len(papers)
        query = payload.get("metadata", {}).get("query", {})
        if isinstance(query, dict) and isinstance(query.get("skipped"), int):
            skipped = query["skipped"]
    except (OSError, json.JSONDecodeError):
        pass

    return manifest_entry(
        config,
        hierarchy,
        output_path,
        "skipped_existing",
        {
            "requested": config.count,
            "written": written,
            "skipped": skipped,
        },
    )


def manifest_entry(
    config: FetchConfig,
    hierarchy: JsonObject,
    output_path: Path,
    status: str,
    stats: JsonObject,
    error: str | None = None,
) -> JsonObject:
    entry: JsonObject = {
        "status": status,
        "subfield": config.subfield,
        "topic": config.topic,
        "topic_id": hierarchy["topic"]["id"],
        "output_path": str(output_path),
        "requested": config.count,
        "written": int(stats.get("written", 0)),
        "skipped": int(stats.get("skipped", 0)),
    }
    if error:
        entry["error"] = error
    return entry


def build_manifest(
    output_root: Path,
    topics: list[JsonObject],
    entries: list[JsonObject],
    overwrite: bool,
) -> JsonObject:
    subfields = {
        topic_level(topic, "subfield")["display_name"]
        for topic in topics
    }
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": "OpenAlex",
            "domain": MEDICINE_DOMAIN,
            "field": MEDICINE_FIELD,
            "field_id": f"https://openalex.org/fields/{MEDICINE_FIELD_ID}",
            "from_publication_date": "2005-01-01",
            "to_publication_date": None,
            "work_type": "article",
            "language": "en",
            "requested_per_topic": REQUESTED_PAPERS_PER_TOPIC,
            "topic_count": len(topics),
            "subfield_count": len(subfields),
            "output_root": str(output_root),
            "overwrite": overwrite,
        },
        "topics": entries,
    }


def write_manifest(
    output_root: Path,
    topics: list[JsonObject],
    entries: list[JsonObject],
    overwrite: bool,
) -> None:
    manifest = build_manifest(output_root, topics, entries, overwrite)
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch up to 2000 OpenAlex Medicine article papers for every Topic."
    )
    parser.add_argument(
        "--output-root",
        default=str(MEDICINE_OUTPUT_ROOT),
        help="Root directory for Subfield folders and manifest.json.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Refetch Topic files that already exist.",
    )
    parser.add_argument(
        "--mailto",
        help="Optional email address to include in OpenAlex API requests.",
    )
    parser.add_argument(
        "--sample-seed",
        type=int,
        help="Optional fixed OpenAlex sample seed for every Topic.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv or sys.argv[1:])
    try:
        manifest = run_batch(
            output_root=Path(args.output_root),
            overwrite=args.overwrite,
            mailto=args.mailto,
            sample_seed=args.sample_seed,
        )
    except (OpenAlexError, OSError) as exc:
        logging.error("%s", exc)
        return 1

    logging.info(
        "Done: topics=%s outputs=%s manifest=%s",
        manifest["metadata"]["topic_count"],
        len(manifest["topics"]),
        Path(args.output_root) / "manifest.json",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
