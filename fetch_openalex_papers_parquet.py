#!/usr/bin/env python3
"""Sample OpenAlex papers from a local Parquet snapshot."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised by running without dependencies
    duckdb = None  # type: ignore[assignment]

from fetch_openalex_papers import (
    ConfigError,
    FetchConfig,
    JsonObject,
    generate_sample_seed,
    load_config,
    parse_paper,
    setup_logging,
    write_output,
)


class SnapshotError(RuntimeError):
    """Raised when a local OpenAlex snapshot cannot be queried."""


PARQUET_COLUMNS = {
    "title": ["display_name"],
    "abstract": ["abstract_inverted_index"],
    "publication_date": ["publication_date"],
    "publication_venue": ["primary_location", "locations"],
}


def load_parquet_config(path: str | Path) -> tuple[FetchConfig, str | None]:
    """Load the shared fetch config and the local-only snapshot path."""
    config = load_config(path, max_count=None)
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read snapshot_path from {config_path}: {exc}") from exc

    snapshot_path = raw.get("snapshot_path")
    if snapshot_path is not None and (
        not isinstance(snapshot_path, str) or not snapshot_path.strip()
    ):
        raise ConfigError("snapshot_path must be a non-empty string when provided")
    return config, snapshot_path


def validate_snapshot(path: str | Path) -> tuple[Path, JsonObject]:
    snapshot_path = Path(path).expanduser().resolve()
    if not snapshot_path.is_dir():
        raise SnapshotError(f"Snapshot directory does not exist: {snapshot_path}")

    manifest_path = snapshot_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SnapshotError(f"Could not read snapshot manifest {manifest_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"Snapshot manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SnapshotError("Snapshot manifest must contain a JSON object")
    if manifest.get("format") != "parquet" or manifest.get("entity") != "works":
        raise SnapshotError("Snapshot manifest must describe works in Parquet format")
    if not isinstance(manifest.get("date"), str):
        raise SnapshotError("Snapshot manifest is missing its date")
    if not isinstance(manifest.get("record_count"), int):
        raise SnapshotError("Snapshot manifest is missing its record_count")

    layout = manifest.get("layout", "updated_date")
    if layout == "primary_topic":
        if not (snapshot_path / "topics.json").is_file():
            raise SnapshotError("Topic-partitioned snapshot is missing topics.json")
        if not next(snapshot_path.glob("data/topic_id=*/*.parquet"), None):
            raise SnapshotError(f"No topic-partitioned Parquet files found under {snapshot_path}")
    elif layout == "updated_date":
        if not next(snapshot_path.glob("updated_date=*/*.parquet"), None):
            raise SnapshotError(f"No Parquet files found under {snapshot_path}")
    else:
        raise SnapshotError(f"Unsupported snapshot layout: {layout!r}")
    return snapshot_path, manifest


def snapshot_glob_for_config(
    snapshot_path: Path,
    manifest: JsonObject,
    config: FetchConfig,
) -> str:
    if manifest.get("layout", "updated_date") == "updated_date":
        return str(snapshot_path / "updated_date=*" / "*.parquet")

    topics_path = snapshot_path / "topics.json"
    try:
        catalog = json.loads(topics_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SnapshotError(f"Could not read topic catalog {topics_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"Topic catalog is not valid JSON: {exc}") from exc
    topics = catalog.get("topics") if isinstance(catalog, dict) else None
    if not isinstance(topics, list):
        raise SnapshotError("Topic catalog must contain a topics list")

    matches = [
        topic
        for topic in topics
        if isinstance(topic, dict)
        and _matches_topic_path(topic, config)
        and isinstance(topic.get("id"), str)
    ]
    if not matches:
        raise SnapshotError(
            "No exact topic partition found for "
            f"{config.domain} > {config.field} > {config.subfield} > {config.topic}"
        )
    if len(matches) > 1:
        raise SnapshotError(f"Multiple topic partitions matched {config.topic!r}")

    topic_id = matches[0]["id"].rstrip("/").rsplit("/", 1)[-1]
    if not topic_id or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in topic_id):
        raise SnapshotError(f"Unsafe topic id in topic catalog: {topic_id!r}")
    parquet_glob = snapshot_path / "data" / f"topic_id={topic_id}" / "*.parquet"
    if not next(parquet_glob.parent.glob("*.parquet"), None):
        raise SnapshotError(f"Topic partition contains no Parquet files: {parquet_glob.parent}")
    return str(parquet_glob)


def _matches_topic_path(topic: JsonObject, config: FetchConfig) -> bool:
    def name(value: Any) -> str | None:
        return value.get("display_name") if isinstance(value, dict) else None

    values = (
        (name(topic.get("domain")), config.domain),
        (name(topic.get("field")), config.field),
        (name(topic.get("subfield")), config.subfield),
        (topic.get("display_name"), config.topic),
    )
    return all(isinstance(actual, str) and actual.casefold() == expected.casefold() for actual, expected in values)


def selected_columns(config: FetchConfig) -> list[str]:
    columns = ["id", "primary_topic"]
    for field in config.fields:
        columns.extend(PARQUET_COLUMNS[field])
    return list(dict.fromkeys(columns))


def build_snapshot_query(config: FetchConfig) -> tuple[str, list[Any]]:
    columns = selected_columns(config)
    select_expressions = [
        "CAST(publication_date AS VARCHAR) AS publication_date"
        if column == "publication_date"
        else column
        for column in columns
    ]
    select_expressions.append("COUNT(*) OVER () AS total_matching_snapshot_count")

    filters = [
        "publication_date >= CAST(? AS DATE)",
        '"type" = ?',
        "language = ?",
        "abstract_inverted_index IS NOT NULL",
        "id IS NOT NULL",
        "lower(primary_topic.domain.display_name) = lower(?)",
        "lower(primary_topic.field.display_name) = lower(?)",
        "lower(primary_topic.subfield.display_name) = lower(?)",
        "lower(primary_topic.display_name) = lower(?)",
    ]
    params: list[Any] = [
        config.from_publication_date,
        config.work_type,
        config.language,
        config.domain,
        config.field,
        config.subfield,
        config.topic,
    ]
    if config.to_publication_date:
        filters.insert(1, "publication_date <= CAST(? AS DATE)")
        params.insert(1, config.to_publication_date)

    query = f"""
        SELECT {', '.join(select_expressions)}
        FROM read_parquet(?, hive_partitioning = false)
        WHERE {' AND '.join(filters)}
        ORDER BY hash(id || ':' || CAST(? AS VARCHAR)), id
        LIMIT ?
    """
    return query, params


def decode_snapshot_work(row: JsonObject) -> tuple[JsonObject | None, str | None]:
    """Convert snapshot-specific scalar representations to API-shaped values."""
    work = dict(row)
    inverted = work.get("abstract_inverted_index")
    if isinstance(inverted, str):
        try:
            inverted = json.loads(inverted)
        except json.JSONDecodeError:
            return None, "malformed_abstract_json"
        if not isinstance(inverted, dict):
            return None, "malformed_abstract_json"
        work["abstract_inverted_index"] = inverted

    publication_date = work.get("publication_date")
    if isinstance(publication_date, (date, datetime)):
        work["publication_date"] = publication_date.date().isoformat() if isinstance(
            publication_date, datetime
        ) else publication_date.isoformat()
    return work, None


def topic_hierarchy_from_work(work: JsonObject) -> JsonObject:
    primary_topic = work.get("primary_topic")
    if not isinstance(primary_topic, dict):
        raise SnapshotError("Matching work is missing primary_topic metadata")

    hierarchy: JsonObject = {}
    for level in ("domain", "field", "subfield"):
        value = primary_topic.get(level)
        if not isinstance(value, dict):
            raise SnapshotError(f"Matching work is missing primary_topic.{level}")
        level_id = value.get("id")
        display_name = value.get("display_name")
        if not isinstance(level_id, str) or not isinstance(display_name, str):
            raise SnapshotError(f"primary_topic.{level} is missing id or display_name")
        hierarchy[level] = {"id": level_id, "display_name": display_name}

    topic_id = primary_topic.get("id")
    topic_name = primary_topic.get("display_name")
    if not isinstance(topic_id, str) or not isinstance(topic_name, str):
        raise SnapshotError("primary_topic is missing id or display_name")
    hierarchy["topic"] = {"id": topic_id, "display_name": topic_name}
    return hierarchy


def fetch_papers_from_snapshot(
    config: FetchConfig,
    snapshot: str | Path,
    *,
    seed_generator=generate_sample_seed,
) -> tuple[list[JsonObject], JsonObject, JsonObject]:
    if duckdb is None:
        raise SnapshotError("DuckDB is not installed; run: python -m pip install -r requirements.txt")

    snapshot_path, manifest = validate_snapshot(snapshot)
    parquet_glob = snapshot_glob_for_config(snapshot_path, manifest, config)
    effective_seed = config.sample_seed if config.sample_seed is not None else seed_generator()
    sample_size = max(config.count, config.count * 5)
    query, filter_params = build_snapshot_query(config)
    params = [parquet_glob, *filter_params, effective_seed, sample_size]

    logging.info("Scanning snapshot %s", snapshot_path)
    logging.info("Candidate sample size=%s seed=%s", sample_size, effective_seed)
    papers: list[JsonObject] = []
    skipped = 0
    skip_reasons: dict[str, int] = {}
    sampled_seen = 0
    topic_hierarchy: JsonObject | None = None
    total_matching: int | None = None
    connection = None
    try:
        connection = duckdb.connect(database=":memory:")
        result = connection.execute(query, params)
        column_names = [description[0] for description in result.description]

        while len(papers) < config.count:
            batch = result.fetchmany(10_000)
            if not batch:
                break
            for values in batch:
                row = dict(zip(column_names, values))
                if topic_hierarchy is None:
                    topic_hierarchy = topic_hierarchy_from_work(row)
                    total_matching = row.get("total_matching_snapshot_count")
                row.pop("total_matching_snapshot_count", None)
                sampled_seen += 1
                work, decode_error = decode_snapshot_work(row)
                if work is None:
                    skipped += 1
                    reason = decode_error or "invalid_snapshot_work"
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    logging.warning("Skipping work: %s", reason)
                    continue
                paper, reasons = parse_paper(work, config)
                if paper is None:
                    skipped += 1
                    for reason in reasons:
                        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    continue
                papers.append(paper)
                if len(papers) >= config.count:
                    break
    except SnapshotError:
        raise
    except Exception as exc:
        raise SnapshotError(f"Could not query OpenAlex Parquet snapshot: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()

    if topic_hierarchy is None or total_matching is None:
        raise SnapshotError(
            "No works matched the configured publication, type, language, and topic filters"
        )

    if len(papers) < config.count:
        logging.warning(
            "Only %s valid papers were found after sampling %s candidates (requested %s)",
            len(papers),
            sampled_seen,
            config.count,
        )

    stats: JsonObject = {
        "requested": config.count,
        "sample_size": sample_size,
        "configured_sample_seed": config.sample_seed,
        "effective_sample_seed": effective_seed,
        "written": len(papers),
        "skipped": skipped,
        "skip_reasons": skip_reasons,
        "sampled_seen": sampled_seen,
        "total_matching_snapshot_count": total_matching,
        "snapshot_path": str(snapshot_path),
        "snapshot_manifest_date": manifest.get("date"),
        "snapshot_record_count": manifest.get("record_count"),
        "selected_columns": selected_columns(config),
        "filters": {
            "from_publication_date": config.from_publication_date,
            "to_publication_date": config.to_publication_date,
            "type": config.work_type,
            "language": config.language,
            "domain": config.domain,
            "field": config.field,
            "subfield": config.subfield,
            "topic": config.topic,
            "has_abstract": True,
        },
    }
    return papers, stats, topic_hierarchy


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sample papers from a local OpenAlex Parquet snapshot."
    )
    parser.add_argument("--config", default="openalex_config.json")
    parser.add_argument("--snapshot", help="Override config.snapshot_path")
    parser.add_argument("--output", help="Override config.output_path")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    setup_logging()
    args = parse_args(argv or sys.argv[1:])
    try:
        config, configured_snapshot = load_parquet_config(args.config)
        snapshot = args.snapshot or configured_snapshot
        if not snapshot:
            raise ConfigError("Provide snapshot_path in the config or pass --snapshot")
        output_path = args.output or config.output_path

        papers, stats, topic_hierarchy = fetch_papers_from_snapshot(config, snapshot)
        write_output(output_path, config, topic_hierarchy, papers, stats)
        logging.info(
            "Done: requested=%s written=%s skipped=%s output=%s",
            stats["requested"],
            stats["written"],
            stats["skipped"],
            output_path,
        )
        return 0
    except (ConfigError, SnapshotError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
