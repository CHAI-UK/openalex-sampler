#!/usr/bin/env python3
"""Build a resumable, topic-partitioned OpenAlex works dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import duckdb
except ImportError:  # pragma: no cover - exercised by running without dependencies
    duckdb = None  # type: ignore[assignment]


class BuildError(RuntimeError):
    """Raised when the optimized snapshot cannot be built safely."""


OUTPUT_COLUMNS = (
    "id",
    "doi",
    "display_name",
    "abstract_inverted_index",
    "publication_date",
    "language",
    "type",
    "primary_topic",
    "primary_location",
    "locations",
    "referenced_works",
    "cited_by_count",
)
STATE_VERSION = 3
ID_INDEX_BUCKETS = 4096
TOPIC_BUCKETS = 64


def sql_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BuildError(f"Could not read {description} {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"{description.capitalize()} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"{description.capitalize()} must contain a JSON object")
    return value


def source_snapshot(input_path: str | Path) -> tuple[Path, dict[str, Any], list[str]]:
    root = Path(input_path).expanduser().resolve()
    if not root.is_dir():
        raise BuildError(f"Input snapshot directory does not exist: {root}")
    manifest = load_json(root / "manifest.json", "input manifest")
    if manifest.get("format") != "parquet" or manifest.get("entity") != "works":
        raise BuildError("Input manifest must describe works in Parquet format")
    if not isinstance(manifest.get("date"), str):
        raise BuildError("Input manifest is missing its date")
    if not isinstance(manifest.get("record_count"), int):
        raise BuildError("Input manifest is missing its record_count")

    files = sorted(
        str(path.relative_to(root))
        for path in root.glob("updated_date=*/*.parquet")
    )
    if not files:
        raise BuildError(f"No source Parquet files found under {root}")
    manifest_files = manifest.get("files")
    if isinstance(manifest_files, list) and len(files) != len(manifest_files):
        raise BuildError(
            f"Input snapshot has {len(files):,} local Parquet files but its manifest "
            f"lists {len(manifest_files):,}; complete the download before building"
        )
    return root, manifest, files


def chunked(values: list[str], size: int) -> Iterable[tuple[int, list[str]]]:
    for start in range(0, len(values), size):
        yield start // size, values[start : start + size]


def state_fingerprint(
    source_root: Path,
    manifest: dict[str, Any],
    files: list[str],
    batch_size: int,
) -> dict[str, Any]:
    file_digest = hashlib.sha256("\n".join(files).encode("utf-8")).hexdigest()
    return {
        "version": STATE_VERSION,
        "source_path": str(source_root),
        "source_manifest_date": manifest["date"],
        "source_record_count": manifest["record_count"],
        "source_file_count": len(files),
        "source_files_sha256": file_digest,
        "batch_size": batch_size,
        "phase": "staging",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def prepare_output(
    output_path: str | Path,
    expected_state: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    output = Path(output_path).expanduser().resolve()
    final_manifest = output / "manifest.json"
    if final_manifest.exists():
        manifest = load_json(final_manifest, "output manifest")
        if (
            manifest.get("layout") == "primary_topic"
            and manifest.get("source_snapshot", {}).get("date")
            == expected_state["source_manifest_date"]
        ):
            return output, {**expected_state, "phase": "complete"}
        raise BuildError(
            f"Output already contains a completed dataset from another snapshot: {output}"
        )

    state_path = output / ".build" / "state.json"
    if state_path.exists():
        state = load_json(state_path, "build state")
        checked_keys = (
            "version",
            "source_path",
            "source_manifest_date",
            "source_record_count",
            "source_file_count",
            "source_files_sha256",
            "batch_size",
        )
        mismatches = [
            key for key in checked_keys if state.get(key) != expected_state.get(key)
        ]
        if mismatches:
            raise BuildError(
                "Existing partial build does not match this invocation: "
                + ", ".join(mismatches)
            )
        return output, state

    if output.exists() and any(output.iterdir()):
        raise BuildError(
            f"Output directory is non-empty but has no resumable build state: {output}"
        )
    output.mkdir(parents=True, exist_ok=True)
    atomic_write_json(state_path, expected_state)
    return output, expected_state


def configure_duckdb(
    connection: Any,
    output: Path,
    threads: int,
    memory_limit: str,
    max_open_files: int = 8,
) -> None:
    temp_directory = output / ".build" / "duckdb-temp"
    temp_directory.mkdir(parents=True, exist_ok=True)
    connection.execute(f"SET threads = {threads}")
    connection.execute(f"SET memory_limit = {sql_literal(memory_limit)}")
    connection.execute(f"SET temp_directory = {sql_literal(temp_directory)}")
    connection.execute("SET preserve_insertion_order = false")
    connection.execute(f"SET partitioned_write_max_open_files = {max_open_files}")


def stage_batches(
    connection: Any,
    source_root: Path,
    files: list[str],
    output: Path,
    batch_size: int,
    compression: str,
) -> int:
    batches_root = output / ".build" / "batches"
    batches_root.mkdir(parents=True, exist_ok=True)
    total_batches = (len(files) + batch_size - 1) // batch_size
    started = time.monotonic()

    for batch_index, relative_files in chunked(files, batch_size):
        batch_name = f"batch_{batch_index:05d}"
        final_batch = batches_root / batch_name
        marker_path = final_batch / "_SUCCESS.json"
        if marker_path.exists():
            logging.info("Staging %s/%s already complete", batch_index + 1, total_batches)
            continue

        temporary_batch = batches_root / f".{batch_name}.tmp"
        if temporary_batch.exists():
            shutil.rmtree(temporary_batch)
        temporary_batch.mkdir(parents=True)
        source_files = [str(source_root / relative) for relative in relative_files]
        columns = ", ".join(f'"{column}"' for column in OUTPUT_COLUMNS)
        query = f"""
            COPY (
                SELECT
                    {columns},
                    regexp_extract(primary_topic.id, '([^/]+)$', 1) AS topic_id,
                    hash(primary_topic.id) % {TOPIC_BUCKETS} AS topic_bucket
                FROM read_parquet(?, hive_partitioning = false)
                WHERE id IS NOT NULL
                  AND abstract_inverted_index IS NOT NULL
                  AND primary_topic.id IS NOT NULL
            )
            TO {sql_literal(temporary_batch)} (
                FORMAT PARQUET,
                COMPRESSION {compression},
                PARTITION_BY (topic_bucket),
                FILENAME_PATTERN 'part_{{uuidv7}}',
                ROW_GROUP_SIZE 2048
            )
        """
        logging.info(
            "Staging batch %s/%s (%s source files)",
            batch_index + 1,
            total_batches,
            len(source_files),
        )
        try:
            result = connection.execute(query, [source_files]).fetchone()
        except Exception as exc:
            raise BuildError(f"Failed while staging {batch_name}: {exc}") from exc
        row_count = int(result[0]) if result else 0
        atomic_write_json(
            temporary_batch / "_SUCCESS.json",
            {
                "batch": batch_index,
                "source_files": relative_files,
                "row_count": row_count,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        os.replace(temporary_batch, final_batch)
        elapsed = time.monotonic() - started
        logging.info(
            "Staged batch %s/%s: %s rows (elapsed %.1f minutes)",
            batch_index + 1,
            total_batches,
            f"{row_count:,}",
            elapsed / 60,
        )

    return total_batches


def staged_buckets(output: Path) -> dict[str, list[Path]]:
    buckets: dict[str, list[Path]] = {}
    pattern = ".build/batches/batch_*/topic_bucket=*/*.parquet"
    for parquet_path in output.glob(pattern):
        bucket = parquet_path.parent.name.removeprefix("topic_bucket=")
        buckets.setdefault(bucket, []).append(parquet_path)
    return buckets


def read_topic(connection: Any, parquet_files: list[Path]) -> dict[str, Any]:
    try:
        row = connection.execute(
            "SELECT primary_topic FROM read_parquet(?, hive_partitioning = false) LIMIT 1",
            [[str(path) for path in parquet_files]],
        ).fetchone()
    except Exception as exc:
        raise BuildError(f"Could not read topic metadata: {exc}") from exc
    if not row or not isinstance(row[0], dict):
        raise BuildError("Staged topic partition is missing primary_topic metadata")
    topic = dict(row[0])
    topic.pop("score", None)
    return topic


def compact_topics(
    connection: Any,
    output: Path,
    compression: str,
) -> dict[str, dict[str, Any]]:
    bucket_inputs = staged_buckets(output)
    catalog_path = output / ".build" / "topics.json"
    catalog_payload = load_json(catalog_path, "topic build catalog") if catalog_path.exists() else {}
    topics = catalog_payload.get("topics", {})
    if not isinstance(topics, dict):
        raise BuildError("Topic build catalog has an invalid topics object")

    completed_root = output / ".build" / "completed-buckets"
    completed_root.mkdir(parents=True, exist_ok=True)
    completed_buckets = {
        path.stem for path in completed_root.glob("*.json")
    }
    all_buckets = sorted(set(bucket_inputs) | completed_buckets, key=int)
    if not all_buckets:
        raise BuildError("Staging produced no topic buckets")

    compacting = output / ".build" / "compacting"
    compacting.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    for position, bucket in enumerate(all_buckets, start=1):
        input_files = sorted(bucket_inputs.get(bucket, []))
        completion_marker = completed_root / f"{bucket}.json"
        if completion_marker.exists():
            for path in input_files:
                path.unlink(missing_ok=True)
            continue
        if not input_files:
            raise BuildError(f"Topic bucket {bucket} has no staged files to compact")

        temporary_bucket = compacting / f"topic_bucket={bucket}"
        if temporary_bucket.exists():
            shutil.rmtree(temporary_bucket)
        temporary_bucket.mkdir(parents=True)
        columns = ", ".join(f'"{column}"' for column in OUTPUT_COLUMNS)
        query = f"""
            COPY (
                SELECT {columns}, topic_id
                FROM read_parquet(?, hive_partitioning = false)
                ORDER BY topic_id, publication_date, id
            )
            TO {sql_literal(temporary_bucket)} (
                FORMAT PARQUET,
                COMPRESSION {compression},
                PARTITION_BY (topic_id),
                FILENAME_PATTERN 'part_{{uuidv7}}',
                ROW_GROUP_SIZE 122880
            )
        """
        logging.info(
            "Compacting topic bucket %s/%s: %s (%s staged files)",
            position,
            len(all_buckets),
            bucket,
            len(input_files),
        )
        try:
            connection.execute(query, [[str(path) for path in input_files]])
        except Exception as exc:
            raise BuildError(f"Failed while compacting topic bucket {bucket}: {exc}") from exc

        generated_topics = sorted(temporary_bucket.glob("topic_id=*"))
        if not generated_topics:
            raise BuildError(f"Topic bucket {bucket} produced no topic partitions")
        for temporary_topic in generated_topics:
            topic_id = temporary_topic.name.removeprefix("topic_id=")
            final_dir = output / "data" / temporary_topic.name
            generated_files = sorted(temporary_topic.glob("*.parquet"))
            if not generated_files:
                raise BuildError(f"Compacted topic {topic_id} contains no Parquet files")
            if final_dir.exists():
                if topic_id not in topics:
                    topics[topic_id] = read_topic(
                        connection, sorted(final_dir.glob("*.parquet"))
                    )
                shutil.rmtree(temporary_topic)
            else:
                topics[topic_id] = read_topic(connection, generated_files)
                final_dir.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary_topic, final_dir)
        atomic_write_json(catalog_path, {"topics": topics})
        atomic_write_json(
            completion_marker,
            {
                "topic_bucket": int(bucket),
                "topic_count": len(generated_topics),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        for path in input_files:
            path.unlink(missing_ok=True)
        shutil.rmtree(temporary_bucket, ignore_errors=True)
        logging.info(
            "Compacted topic bucket %s/%s (elapsed %.1f minutes)",
            position,
            len(all_buckets),
            (time.monotonic() - started) / 60,
        )
    return topics


def build_work_id_index(
    connection: Any,
    output: Path,
    compression: str,
    *,
    bucket_count: int = ID_INDEX_BUCKETS,
) -> dict[str, Any]:
    """Build a compact id-to-topic lookup without duplicating full work records."""
    final_index = output / "work-id-index"
    marker_path = final_index / "_SUCCESS.json"
    if marker_path.exists():
        return load_json(marker_path, "work ID index marker")
    if final_index.exists():
        raise BuildError(
            f"Work ID index exists without a completion marker: {final_index}"
        )

    topic_files = sorted(output.glob("data/topic_id=*/*.parquet"))
    if not topic_files:
        raise BuildError("Cannot build work ID index because no compacted topic files exist")
    temporary_index = output / ".build" / "work-id-index.tmp"
    if temporary_index.exists():
        shutil.rmtree(temporary_index)
    temporary_index.mkdir(parents=True)

    query = f"""
        COPY (
            SELECT
                id,
                regexp_extract(primary_topic.id, '([^/]+)$', 1) AS topic_id,
                hash(id) % {bucket_count} AS id_bucket
            FROM read_parquet(?, hive_partitioning = false)
            WHERE id IS NOT NULL AND primary_topic.id IS NOT NULL
        )
        TO {sql_literal(temporary_index)} (
            FORMAT PARQUET,
            COMPRESSION {compression},
            PARTITION_BY (id_bucket),
            FILENAME_PATTERN 'part_{{uuidv7}}',
            ROW_GROUP_SIZE 122880
        )
    """
    logging.info(
        "Building work ID index from %s topic files into %s buckets",
        len(topic_files),
        bucket_count,
    )
    try:
        result = connection.execute(query, [[str(path) for path in topic_files]]).fetchone()
    except Exception as exc:
        raise BuildError(f"Failed while building work ID index: {exc}") from exc
    row_count = int(result[0]) if result else 0
    marker = {
        "layout": "hash_bucket",
        "hash_function": "duckdb_hash",
        "duckdb_version": duckdb.__version__,
        "bucket_count": bucket_count,
        "row_count": row_count,
        "columns": ["id", "topic_id"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(temporary_index / "_SUCCESS.json", marker)
    os.replace(temporary_index, final_index)
    logging.info("Work ID index complete: %s rows", f"{row_count:,}")
    return marker


def finalize_build(
    output: Path,
    state: dict[str, Any],
    topics: dict[str, dict[str, Any]],
    source_manifest: dict[str, Any],
    work_id_index: dict[str, Any],
) -> None:
    batch_markers = list(output.glob(".build/batches/batch_*/_SUCCESS.json"))
    optimized_record_count = sum(
        int(load_json(path, "batch marker").get("row_count", 0))
        for path in batch_markers
    )
    if work_id_index.get("row_count") != optimized_record_count:
        raise BuildError(
            "Work ID index row count does not match the optimized topic records: "
            f"{work_id_index.get('row_count')} != {optimized_record_count}"
        )
    topic_values = [topics[key] for key in sorted(topics)]
    atomic_write_json(output / "topics.json", {"topics": topic_values})
    manifest = {
        "date": source_manifest["date"],
        "format": "parquet",
        "entity": "works",
        "layout": "primary_topic",
        "record_count": optimized_record_count,
        "topic_count": len(topic_values),
        "source_snapshot": {
            "path": state["source_path"],
            "date": state["source_manifest_date"],
            "record_count": state["source_record_count"],
            "file_count": state["source_file_count"],
            "files_sha256": state["source_files_sha256"],
        },
        "columns": list(OUTPUT_COLUMNS),
        "work_id_index": {
            "path": "work-id-index",
            **work_id_index,
        },
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_write_json(output / "manifest.json", manifest)
    shutil.rmtree(output / ".build", ignore_errors=True)


def build_topic_partitions(
    input_path: str | Path,
    output_path: str | Path,
    *,
    batch_size: int = 32,
    threads: int = 2,
    memory_limit: str = "8GB",
    compression: str = "ZSTD",
    max_open_files: int = 4,
) -> Path:
    if duckdb is None:
        raise BuildError("DuckDB is not installed; run: python -m pip install -r requirements.txt")
    if batch_size <= 0:
        raise BuildError("batch_size must be positive")
    if threads <= 0:
        raise BuildError("threads must be positive")
    if max_open_files <= 0:
        raise BuildError("max_open_files must be positive")
    if compression not in {"ZSTD", "SNAPPY"}:
        raise BuildError("compression must be ZSTD or SNAPPY")

    source_root, source_manifest, files = source_snapshot(input_path)
    expected_state = state_fingerprint(source_root, source_manifest, files, batch_size)
    output, state = prepare_output(output_path, expected_state)
    if state.get("phase") == "complete":
        logging.info("Optimized dataset is already complete: %s", output)
        return output

    connection = duckdb.connect(database=":memory:")
    try:
        configure_duckdb(
            connection,
            output,
            threads,
            memory_limit,
            max_open_files,
        )
        stage_batches(
            connection,
            source_root,
            files,
            output,
            batch_size,
            compression,
        )
        state["phase"] = "compacting"
        atomic_write_json(output / ".build" / "state.json", state)
        topics = compact_topics(connection, output, compression)
        state["phase"] = "indexing"
        atomic_write_json(output / ".build" / "state.json", state)
        work_id_index = build_work_id_index(connection, output, compression)
        finalize_build(output, state, topics, source_manifest, work_id_index)
    finally:
        connection.close()
    logging.info("Topic-partitioned dataset complete: %s", output)
    return output


def setup_logging(log_file: str | Path | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        path = Path(log_file).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
        force=True,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a resumable OpenAlex works dataset partitioned by primary topic."
    )
    parser.add_argument("--input", required=True, help="Original works-parquet directory")
    parser.add_argument("--output", required=True, help="New optimized output directory")
    parser.add_argument("--batch-size", type=int, default=32, help="Source files per checkpoint")
    parser.add_argument("--threads", type=int, default=2, help="DuckDB worker threads")
    parser.add_argument("--memory-limit", default="8GB", help="DuckDB memory limit")
    parser.add_argument(
        "--max-open-files",
        type=int,
        default=4,
        help="Maximum simultaneously open topic writers (lower uses less memory)",
    )
    parser.add_argument(
        "--compression", choices=("ZSTD", "SNAPPY"), default="ZSTD"
    )
    parser.add_argument("--log-file", help="Optional persistent log file")
    return parser.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    setup_logging(args.log_file)
    try:
        build_topic_partitions(
            args.input,
            args.output,
            batch_size=args.batch_size,
            threads=args.threads,
            memory_limit=args.memory_limit,
            compression=args.compression,
            max_open_files=args.max_open_files,
        )
        return 0
    except (BuildError, OSError) as exc:
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
