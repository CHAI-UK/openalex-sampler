import json
import tempfile
import unittest
from pathlib import Path

from fetch_openalex_papers import OpenAlexError
from fetch_medicine_topics import (
    MEDICINE_OUTPUT_FIELDS,
    build_medicine_config,
    build_topic_hierarchy,
    discover_medicine_topics,
    run_batch,
    safe_path_name,
)


def topic(topic_id, name, subfield_name="Oncology", subfield_id="2730"):
    return {
        "id": f"https://openalex.org/{topic_id}",
        "display_name": name,
        "domain": {
            "id": "https://openalex.org/domains/4",
            "display_name": "Health Sciences",
        },
        "field": {
            "id": "https://openalex.org/fields/27",
            "display_name": "Medicine",
        },
        "subfield": {
            "id": f"https://openalex.org/subfields/{subfield_id}",
            "display_name": subfield_name,
        },
    }


class MedicineBatchTests(unittest.TestCase):
    def test_discover_medicine_topics_uses_cursor_pagination(self):
        calls = []

        def fetcher(url, params):
            calls.append((url, dict(params)))
            if params["cursor"] == "*":
                return {
                    "meta": {"next_cursor": "next-page"},
                    "results": [topic("T1", "Cancer Treatment")],
                }
            return {
                "meta": {"next_cursor": None},
                "results": [topic("T2", "Tumor Immunology")],
            }

        topics = discover_medicine_topics(fetcher, api_key="test-api-key")

        self.assertEqual([item["display_name"] for item in topics], ["Cancer Treatment", "Tumor Immunology"])
        self.assertEqual(calls[0][0], "https://api.openalex.org/topics")
        self.assertEqual(calls[0][1]["filter"], "field.id:27")
        self.assertEqual(calls[0][1]["per_page"], 200)
        self.assertEqual(calls[0][1]["cursor"], "*")
        self.assertEqual(calls[0][1]["api_key"], "test-api-key")
        self.assertEqual(calls[1][1]["cursor"], "next-page")

    def test_discover_medicine_topics_rejects_unexpected_shape(self):
        def fetcher(url, params):
            return {"results": "not-a-list"}

        with self.assertRaises(OpenAlexError):
            discover_medicine_topics(fetcher)

    def test_safe_path_name_replaces_path_separators_and_controls(self):
        self.assertEqual(
            safe_path_name("  A/B\\C\nTopic  "),
            "A_B_C Topic",
        )

    def test_build_medicine_config_uses_batch_defaults(self):
        config = build_medicine_config(topic("T1", "Cancer Treatment"), Path("out"))

        self.assertEqual(config.count, 2000)
        self.assertEqual(config.from_publication_date, "2005-01-01")
        self.assertIsNone(config.to_publication_date)
        self.assertEqual(config.work_type, "article")
        self.assertEqual(config.language, "en")
        self.assertEqual(config.domain, "Health Sciences")
        self.assertEqual(config.field, "Medicine")
        self.assertEqual(config.subfield, "Oncology")
        self.assertEqual(config.topic, "Cancer Treatment")
        self.assertEqual(config.required_fields, ["title", "abstract"])
        self.assertEqual(config.fields, MEDICINE_OUTPUT_FIELDS)
        self.assertEqual(config.output_path, str(Path("out") / "Oncology" / "Cancer Treatment.json"))

    def test_build_topic_hierarchy_reuses_listed_topic_metadata(self):
        hierarchy = build_topic_hierarchy(topic("T1", "Cancer Treatment"))

        self.assertEqual(hierarchy["domain"]["display_name"], "Health Sciences")
        self.assertEqual(hierarchy["field"]["display_name"], "Medicine")
        self.assertEqual(hierarchy["subfield"]["display_name"], "Oncology")
        self.assertEqual(hierarchy["topic"]["id"], "https://openalex.org/T1")

    def test_run_batch_writes_success_partial_error_and_manifest(self):
        topics = [
            topic("T1", "Cancer Treatment", "Oncology", "2730"),
            topic("T2", "Emergency Care", "Emergency Medicine", "2711"),
            topic("T3", "Broken Topic", "Oncology", "2730"),
        ]

        discovered_with = []

        def discoverer(fetcher, api_key=None):
            discovered_with.append(api_key)
            return topics

        def paper_fetcher(config, hierarchy):
            if config.topic == "Broken Topic":
                raise OpenAlexError("boom")
            paper = {
                "title": config.topic,
                "abstract": "A paper.",
                "publication_date": "2024-01-01",
                "publication_venue": "Example Journal",
            }
            written = 2000 if config.topic == "Cancer Treatment" else 1
            return [paper] * written, {
                "requested": config.count,
                "written": written,
                "skipped": 3,
                "filters": "test",
                "select": "test",
            }

        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_batch(
                output_root=Path(tmp),
                api_key="test-api-key",
                discoverer=discoverer,
                paper_fetcher=paper_fetcher,
            )

            self.assertEqual(manifest["metadata"]["topic_count"], 3)
            self.assertEqual(manifest["metadata"]["subfield_count"], 2)
            statuses = [entry["status"] for entry in manifest["topics"]]
            self.assertEqual(statuses, ["written", "partial", "error"])
            self.assertTrue((Path(tmp) / "Oncology" / "Cancer Treatment.json").exists())
            self.assertTrue((Path(tmp) / "Emergency Medicine" / "Emergency Care.json").exists())
            self.assertTrue((Path(tmp) / "manifest.json").exists())
            self.assertEqual(discovered_with, ["test-api-key"])

    def test_run_batch_skips_existing_file_unless_overwrite(self):
        calls = []
        existing_payload = {
            "metadata": {"query": {"skipped": 4}},
            "papers": [{"title": "Existing", "abstract": "A"}],
        }

        def discoverer(fetcher, api_key=None):
            return [topic("T1", "Cancer Treatment")]

        def paper_fetcher(config, hierarchy):
            calls.append(config.topic)
            return [], {"requested": config.count, "written": 0, "skipped": 0}

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "Oncology"
            output.mkdir()
            (output / "Cancer Treatment.json").write_text(json.dumps(existing_payload), encoding="utf-8")

            manifest = run_batch(
                output_root=Path(tmp),
                discoverer=discoverer,
                paper_fetcher=paper_fetcher,
            )

            self.assertEqual(calls, [])
            self.assertEqual(manifest["topics"][0]["status"], "skipped_existing")
            self.assertEqual(manifest["topics"][0]["written"], 1)
            self.assertEqual(manifest["topics"][0]["skipped"], 4)

            run_batch(
                output_root=Path(tmp),
                overwrite=True,
                discoverer=discoverer,
                paper_fetcher=paper_fetcher,
            )

            self.assertEqual(calls, ["Cancer Treatment"])


if __name__ == "__main__":
    unittest.main()
