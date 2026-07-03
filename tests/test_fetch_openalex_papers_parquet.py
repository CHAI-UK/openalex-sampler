import json
import tempfile
import unittest
from pathlib import Path

try:
    import duckdb
except ImportError:
    duckdb = None

from fetch_openalex_papers import ConfigError, FetchConfig, load_config
from build_topic_partitions import (
    build_topic_partitions,
    configure_duckdb,
    prepare_output,
    source_snapshot,
    stage_batches,
    state_fingerprint,
)
from fetch_openalex_papers_parquet import (
    SnapshotError,
    decode_snapshot_work,
    fetch_papers_from_snapshot,
    run,
    validate_snapshot,
)


@unittest.skipIf(duckdb is None, "DuckDB is not installed")
class ParquetFetcherTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "count": 3,
            "from_publication_date": "2021-01-01",
            "to_publication_date": None,
            "work_type": "article",
            "language": "en",
            "domain": "Physical Sciences",
            "field": "Computer Science",
            "subfield": "Computer Vision and Pattern Recognition",
            "topic": "Advanced Neural Network Applications",
            "field_match": "primary",
            "sample_seed": 123,
            "required_fields": ["title", "abstract"],
            "fields": ["title", "abstract", "publication_date", "publication_venue"],
            "output_path": "papers.json",
            "per_page": 100,
            "require_english_text": True,
            "require_clean_text": True,
        }
        values.update(overrides)
        return FetchConfig(**values)

    def make_snapshot(self, root: Path, *, rows=20, abstract_json=None):
        snapshot = root / "works-parquet"
        partition = snapshot / "updated_date=2026-01-01"
        partition.mkdir(parents=True)
        (snapshot / "manifest.json").write_text(
            json.dumps(
                {
                    "date": "2026-01-01",
                    "format": "parquet",
                    "entity": "works",
                    "record_count": rows,
                }
            ),
            encoding="utf-8",
        )
        abstract = abstract_json or '{"A":[0],"paper":[1]}'
        connection = duckdb.connect()
        connection.execute(
            """
            CREATE TABLE works AS
            SELECT
                'https://openalex.org/W' || i AS id,
                'https://doi.org/10.1234/example.' || i AS doi,
                'Paper ' || i AS display_name,
                ? AS abstract_inverted_index,
                DATE '2024-01-01' + CAST(i AS INTEGER) AS publication_date,
                'en' AS language,
                'article' AS type,
                struct_pack(
                    id := 'https://openalex.org/T10036',
                    display_name := 'Advanced Neural Network Applications',
                    score := CAST(0.987 AS FLOAT),
                    subfield := struct_pack(
                        id := 'https://openalex.org/subfields/1707',
                        display_name := 'Computer Vision and Pattern Recognition'
                    ),
                    field := struct_pack(
                        id := 'https://openalex.org/fields/17',
                        display_name := 'Computer Science'
                    ),
                    domain := struct_pack(
                        id := 'https://openalex.org/domains/3',
                        display_name := 'Physical Sciences'
                    )
                ) AS primary_topic,
                struct_pack(
                    source := struct_pack(display_name := 'Venue One'),
                    raw_source_name := ''
                ) AS primary_location,
                [struct_pack(
                    source := struct_pack(display_name := 'Venue One'),
                    raw_source_name := ''
                )] AS locations,
                CASE
                    WHEN i = 0 THEN []::VARCHAR[]
                    ELSE ['https://openalex.org/W' || (i - 1)]
                END AS referenced_works,
                CAST(100 - i AS INTEGER) AS cited_by_count
            FROM range(?) AS generated(i)
            """,
            [abstract, rows],
        )
        output = partition / "part_0000.parquet"
        connection.execute(f"COPY works TO '{output}' (FORMAT PARQUET)")
        connection.close()
        return snapshot

    def test_fetches_filters_decodes_and_reports_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = self.make_snapshot(Path(tmp))
            papers, stats, hierarchy = fetch_papers_from_snapshot(
                self.config(), snapshot
            )

            self.assertEqual(len(papers), 3)
            self.assertEqual(papers[0]["abstract"], "A paper")
            self.assertEqual(papers[0]["publication_venue"], "Venue One")
            self.assertEqual(stats["total_matching_snapshot_count"], 20)
            self.assertEqual(stats["snapshot_manifest_date"], "2026-01-01")
            self.assertEqual(hierarchy["topic"]["id"], "https://openalex.org/T10036")

    def test_seeded_sampling_is_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = self.make_snapshot(Path(tmp), rows=100)
            first, _, _ = fetch_papers_from_snapshot(self.config(), snapshot)
            again, _, _ = fetch_papers_from_snapshot(self.config(), snapshot)
            different, _, _ = fetch_papers_from_snapshot(
                self.config(sample_seed=456), snapshot
            )

            self.assertEqual(first, again)
            self.assertNotEqual(first, different)

    def test_malformed_abstract_is_skipped_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = self.make_snapshot(Path(tmp), rows=2, abstract_json="not-json")
            papers, stats, _ = fetch_papers_from_snapshot(self.config(count=1), snapshot)
            self.assertEqual(papers, [])
            self.assertEqual(stats["skip_reasons"], {"malformed_abstract_json": 2})

    def test_zero_matching_works_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = self.make_snapshot(Path(tmp))
            with self.assertRaisesRegex(SnapshotError, "No works matched"):
                fetch_papers_from_snapshot(self.config(language="fr"), snapshot)

    def test_date_range_and_each_structured_filter_are_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = self.make_snapshot(Path(tmp), rows=10)
            papers, stats, _ = fetch_papers_from_snapshot(
                self.config(
                    count=10,
                    from_publication_date="2024-01-05",
                    to_publication_date="2024-01-07",
                ),
                snapshot,
            )
            self.assertEqual(len(papers), 3)
            self.assertEqual(stats["total_matching_snapshot_count"], 3)

            mismatches = {
                "work_type": "book",
                "language": "fr",
                "domain": "Health Sciences",
                "field": "Medicine",
                "subfield": "Oncology",
                "topic": "Bone health and treatments",
            }
            for key, value in mismatches.items():
                with self.subTest(filter=key), self.assertRaises(SnapshotError):
                    fetch_papers_from_snapshot(self.config(**{key: value}), snapshot)

    def test_missing_snapshot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SnapshotError, "does not exist"):
                validate_snapshot(Path(tmp) / "missing")

    def test_invalid_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "works-parquet"
            snapshot.mkdir()
            (snapshot / "manifest.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(SnapshotError, "works in Parquet"):
                validate_snapshot(snapshot)

    def test_topic_partition_builder_output_is_queryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self.make_snapshot(root, rows=20)
            optimized = root / "works-by-topic-parquet"
            build_topic_partitions(
                snapshot,
                optimized,
                batch_size=1,
                threads=2,
                memory_limit="1GB",
            )

            manifest = json.loads((optimized / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["layout"], "primary_topic")
            self.assertEqual(manifest["record_count"], 20)
            self.assertEqual(manifest["topic_count"], 1)
            self.assertEqual(manifest["work_id_index"]["row_count"], 20)
            topic_catalog = json.loads(
                (optimized / "topics.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("score", topic_catalog["topics"][0])
            topic_file = next(
                optimized.glob("data/topic_id=T10036/*.parquet"),
                None,
            )
            self.assertIsNotNone(topic_file)
            row = duckdb.connect().execute(
                """
                SELECT doi, referenced_works, cited_by_count
                FROM read_parquet(?)
                WHERE id = 'https://openalex.org/W1'
                """,
                [str(topic_file)],
            ).fetchone()
            self.assertEqual(row[0], "https://doi.org/10.1234/example.1")
            self.assertEqual(row[1], ["https://openalex.org/W0"])
            self.assertEqual(row[2], 99)

            index_rows = duckdb.connect().execute(
                "SELECT id, topic_id FROM read_parquet(?) ORDER BY id",
                [str(optimized / "work-id-index/id_bucket=*/*.parquet")],
            ).fetchall()
            self.assertEqual(len(index_rows), 20)
            self.assertEqual(index_rows[0][1], "T10036")

            papers, stats, _ = fetch_papers_from_snapshot(self.config(), optimized)
            self.assertEqual(len(papers), 3)
            self.assertEqual(stats["total_matching_snapshot_count"], 20)

    def test_topic_partition_builder_resumes_after_staging(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self.make_snapshot(root, rows=5)
            optimized_path = root / "works-by-topic-parquet"
            source_root, manifest, files = source_snapshot(snapshot)
            expected = state_fingerprint(source_root, manifest, files, 1)
            optimized, _ = prepare_output(optimized_path, expected)
            connection = duckdb.connect()
            try:
                configure_duckdb(connection, optimized, 1, "1GB")
                stage_batches(
                    connection,
                    source_root,
                    files,
                    optimized,
                    1,
                    "ZSTD",
                )
            finally:
                connection.close()

            marker = optimized / ".build/batches/batch_00000/_SUCCESS.json"
            self.assertTrue(marker.is_file())
            build_topic_partitions(
                snapshot,
                optimized,
                batch_size=1,
                threads=1,
                memory_limit="1GB",
            )
            self.assertTrue((optimized / "manifest.json").is_file())
            self.assertFalse((optimized / ".build").exists())

    def test_cli_overrides_snapshot_and_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = self.make_snapshot(root)
            output = root / "result.json"
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps({**self.config(count=1).__dict__, "snapshot_path": "wrong"}),
                encoding="utf-8",
            )
            self.assertEqual(
                run(
                    [
                        "--config",
                        str(config_path),
                        "--snapshot",
                        str(snapshot),
                        "--output",
                        str(output),
                    ]
                ),
                0,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["papers"]), 1)
            self.assertEqual(payload["metadata"]["query"]["snapshot_path"], str(snapshot))


class SharedConfigTests(unittest.TestCase):
    def test_local_config_can_exceed_api_limit(self):
        values = ParquetFetcherTests().config(count=10_001).__dict__
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(values), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)
            self.assertEqual(load_config(path, max_count=None).count, 10_001)

    def test_decode_snapshot_work_normalizes_dates(self):
        work, reason = decode_snapshot_work(
            {
                "abstract_inverted_index": '{"Hello":[0]}',
                "publication_date": __import__("datetime").date(2024, 2, 3),
            }
        )
        self.assertIsNone(reason)
        self.assertEqual(work["abstract_inverted_index"], {"Hello": [0]})
        self.assertEqual(work["publication_date"], "2024-02-03")


if __name__ == "__main__":
    unittest.main()
