import json
import tempfile
import unittest
from pathlib import Path

from fetch_openalex_papers import (
    ConfigError,
    FetchConfig,
    OpenAlexError,
    build_select,
    build_work_filters,
    clean_text,
    fetch_papers,
    field_id_for_filter,
    load_config,
    looks_like_clean_abstract,
    looks_like_english,
    parse_paper,
    parse_location_venue,
    parse_publication_venue,
    reconstruct_abstract,
    resolve_field_id,
    resolve_topic_hierarchy,
    write_output,
)


class FetcherTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "count": 2,
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
            "per_page": 1,
            "require_english_text": True,
            "require_clean_text": True,
            "mailto": None,
        }
        values.update(overrides)
        return FetchConfig(**values)

    def topic_hierarchy(self):
        return {
            "domain": {
                "id": "https://openalex.org/domains/3",
                "display_name": "Physical Sciences",
            },
            "field": {
                "id": "https://openalex.org/fields/17",
                "display_name": "Computer Science",
            },
            "subfield": {
                "id": "https://openalex.org/subfields/1707",
                "display_name": "Computer Vision and Pattern Recognition",
            },
            "topic": {
                "id": "https://openalex.org/T10036",
                "display_name": "Advanced Neural Network Applications",
            },
        }

    def test_reconstruct_abstract_orders_words_by_position(self):
        inverted = {"world": [1], "Hello": [0], "again": [2]}
        self.assertEqual(reconstruct_abstract(inverted), "Hello world again")

    def test_reconstruct_abstract_returns_none_for_invalid_input(self):
        self.assertIsNone(reconstruct_abstract(None))
        self.assertIsNone(reconstruct_abstract({"Hello": ["0"]}))
        self.assertIsNone(reconstruct_abstract({"Hello": [1]}))

    def test_load_config_validates_missing_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"count": 2}), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_load_config_validates_count_and_unsupported_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            data = self.config(count=0, fields=["title", "doi"]).__dict__
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_resolve_field_id_requires_exact_match(self):
        def fetcher(url, params):
            self.assertTrue(url.endswith("/fields"))
            self.assertEqual(params["search"], "Computer Science")
            return {
                "results": [
                    {"id": "https://openalex.org/fields/17", "display_name": "Computer Science"}
                ]
            }

        self.assertEqual(
            resolve_field_id("Computer Science", fetcher),
            ("https://openalex.org/fields/17", "Computer Science"),
        )

    def test_resolve_field_id_rejects_no_exact_match(self):
        def fetcher(url, params):
            return {"results": [{"id": "x", "display_name": "Computer Engineering"}]}

        with self.assertRaises(OpenAlexError):
            resolve_field_id("Computer Science", fetcher)

    def test_resolve_field_id_rejects_ambiguous_exact_matches(self):
        def fetcher(url, params):
            return {
                "results": [
                    {"id": "x", "display_name": "Computer Science"},
                    {"id": "y", "display_name": "computer science"},
                ]
            }

        with self.assertRaises(OpenAlexError):
            resolve_field_id("Computer Science", fetcher)

    def test_resolve_topic_hierarchy_requires_exact_path_match(self):
        def fetcher(url, params):
            self.assertTrue(url.endswith("/topics"))
            self.assertEqual(params["search"], "Advanced Neural Network Applications")
            return {
                "results": [
                    {
                        "id": "https://openalex.org/T10036",
                        "display_name": "Advanced Neural Network Applications",
                        "domain": {
                            "id": "https://openalex.org/domains/3",
                            "display_name": "Physical Sciences",
                        },
                        "field": {
                            "id": "https://openalex.org/fields/17",
                            "display_name": "Computer Science",
                        },
                        "subfield": {
                            "id": "https://openalex.org/subfields/1707",
                            "display_name": "Computer Vision and Pattern Recognition",
                        },
                    }
                ]
            }

        self.assertEqual(resolve_topic_hierarchy(self.config(), fetcher), self.topic_hierarchy())

    def test_resolve_topic_hierarchy_rejects_wrong_subfield(self):
        def fetcher(url, params):
            return {
                "results": [
                    {
                        "id": "https://openalex.org/T10036",
                        "display_name": "Advanced Neural Network Applications",
                        "domain": {
                            "id": "https://openalex.org/domains/3",
                            "display_name": "Physical Sciences",
                        },
                        "field": {
                            "id": "https://openalex.org/fields/17",
                            "display_name": "Computer Science",
                        },
                        "subfield": {
                            "id": "https://openalex.org/subfields/1702",
                            "display_name": "Artificial Intelligence",
                        },
                    }
                ]
            }

        with self.assertRaises(OpenAlexError):
            resolve_topic_hierarchy(self.config(), fetcher)

    def test_parse_paper_skips_missing_required_fields(self):
        paper, missing = parse_paper({"display_name": ""}, self.config())
        self.assertIsNone(paper)
        self.assertEqual(missing, ["missing_title", "missing_abstract"])

    def test_parse_paper_includes_publication_date_and_venue(self):
        work = {
            "display_name": "A Computer Vision Paper",
            "abstract_inverted_index": {"This": [0], "works.": [1]},
            "publication_date": "2024-02-03",
            "primary_location": {
                "source": {
                    "display_name": "Proceedings of the IEEE/CVF Conference on Computer Vision"
                }
            },
        }
        paper, reasons = parse_paper(work, self.config())
        self.assertEqual(reasons, [])
        self.assertEqual(
            paper,
            {
                "title": "A Computer Vision Paper",
                "abstract": "This works.",
                "publication_date": "2024-02-03",
                "publication_venue": "Proceedings of the IEEE/CVF Conference on Computer Vision",
            },
        )

    def test_parse_paper_sets_optional_publication_fields_to_null_when_missing(self):
        work = {
            "display_name": "A Computer Vision Paper",
            "abstract_inverted_index": {"This": [0], "works.": [1]},
        }
        paper, reasons = parse_paper(work, self.config())
        self.assertEqual(reasons, [])
        self.assertIsNone(paper["publication_date"])
        self.assertIsNone(paper["publication_venue"])

    def test_parse_publication_venue_reads_primary_location_source(self):
        self.assertEqual(
            parse_publication_venue(
                {"primary_location": {"source": {"display_name": "<i>Nature</i>"}}}
            ),
            "Nature",
        )

    def test_parse_publication_venue_falls_back_to_raw_source_name(self):
        self.assertEqual(
            parse_publication_venue(
                {
                    "primary_location": {
                        "source": None,
                        "raw_source_name": "Proceedings of Example Conference",
                    }
                }
            ),
            "Proceedings of Example Conference",
        )

    def test_parse_publication_venue_falls_back_to_locations(self):
        self.assertEqual(
            parse_publication_venue(
                {
                    "primary_location": {"source": None, "raw_source_name": ""},
                    "locations": [
                        {"source": None, "raw_source_name": ""},
                        {
                            "source": {
                                "display_name": "arXiv (Cornell University)",
                            },
                            "raw_source_name": "",
                        },
                    ],
                }
            ),
            "arXiv (Cornell University)",
        )

    def test_parse_location_venue_prefers_source_display_name(self):
        self.assertEqual(
            parse_location_venue(
                {
                    "source": {"display_name": "arXiv (Cornell University)"},
                    "raw_source_name": "",
                }
            ),
            "arXiv (Cornell University)",
        )

    def test_looks_like_english_rejects_korean_abstract(self):
        korean = (
            "이커머스 업계에서 딥러닝 기반 추천 시스템은 사용자 경험 향상과 매출 "
            "증대에 중요한 역할을 한다."
        )
        self.assertFalse(looks_like_english(korean))
        self.assertTrue(looks_like_english("Deep learning recommendation systems improve user experience."))

    def test_parse_paper_skips_non_english_abstract(self):
        work = {
            "display_name": "A Solution for Cold Item Problem in Recommendation Systems",
            "abstract_inverted_index": {
                "이커머스": [0],
                "업계에서": [1],
                "추천": [2],
                "시스템은": [3],
                "중요한": [4],
                "역할을": [5],
                "한다.": [6],
            },
        }
        paper, reasons = parse_paper(work, self.config())
        self.assertIsNone(paper)
        self.assertEqual(reasons, ["non_english_abstract"])

    def test_looks_like_clean_abstract_rejects_latex_document_dump(self):
        dirty = (
            "\\documentclass[10pt]{article}<br> \\usepackage{amsmath}<br> "
            "\\begin{document}<br> \\section{ABSTRACT}<br> This is buried in a full document."
        )
        clean = (
            "This study proposes a graph-based method for classifying biomedical images. "
            "The results show improved accuracy across three benchmark datasets."
        )
        self.assertFalse(looks_like_clean_abstract(dirty))
        self.assertTrue(looks_like_clean_abstract(clean))

    def test_parse_paper_skips_unclean_abstract(self):
        work = {
            "display_name": "New Ideas On Super Nebulous By Hyper Nebbish",
            "abstract_inverted_index": {
                "\\documentclass[10pt]{article}<br>": [0],
                "\\usepackage{amsmath}<br>": [1],
                "\\begin{document}<br>": [2],
                "\\section{ABSTRACT}<br>": [3],
                "This": [4],
                "paper": [5],
                "contains": [6],
                "a": [7],
                "document": [8],
                "dump.": [9],
            },
        }
        paper, reasons = parse_paper(work, self.config())
        self.assertIsNone(paper)
        self.assertEqual(reasons, ["unclean_abstract"])

    def test_clean_text_unescapes_and_strips_simple_html_wrappers(self):
        raw = (
            "&lt;div class=\"section abstract\"&gt;&lt;div class=\"htmlview paragraph\"&gt;"
            "Connected vehicle data unlock <scp>compelling</scp> solutions.&lt;/div&gt;&lt;/div&gt;"
        )
        self.assertTrue(looks_like_clean_abstract(raw))
        self.assertEqual(clean_text(raw), "Connected vehicle data unlock compelling solutions.")

    def test_parse_paper_cleans_html_escaped_abstract(self):
        work = {
            "display_name": "Connected Vehicle Data Time Series Dependence",
            "abstract_inverted_index": {
                "&lt;div": [0],
                "class=\"section": [1],
                "abstract\"&gt;&lt;div": [2],
                "class=\"htmlview": [3],
                "paragraph\"&gt;Connected": [4],
                "vehicle": [5],
                "data": [6],
                "unlock": [7],
                "compelling": [8],
                "solutions.&lt;/div&gt;&lt;/div&gt;": [9],
            },
        }
        paper, reasons = parse_paper(work, self.config())
        self.assertEqual(reasons, [])
        self.assertEqual(
            paper,
            {
                "title": "Connected Vehicle Data Time Series Dependence",
                "abstract": "Connected vehicle data unlock compelling solutions.",
                "publication_date": None,
                "publication_venue": None,
            },
        )

    def test_build_query_parts(self):
        config = self.config()
        self.assertEqual(
            build_select(config.fields),
            "display_name,abstract_inverted_index,publication_date,primary_location,locations",
        )
        self.assertEqual(field_id_for_filter("https://openalex.org/fields/17"), "17")
        self.assertEqual(
            build_work_filters(config, self.topic_hierarchy()),
            "from_publication_date:2021-01-01,type:article,language:en,has_abstract:true,"
            "primary_topic.id:T10036",
        )

    def test_fetch_papers_uses_sample_seed_pages_and_select(self):
        calls = []

        def fetcher(url, params):
            calls.append((url, dict(params)))
            if params["page"] == 1:
                return {
                    "meta": {"count": 1000},
                    "results": [
                        {
                            "display_name": "One",
                            "abstract_inverted_index": {"A": [0], "paper": [1]},
                            "publication_date": "2023-01-01",
                            "primary_location": {"source": {"display_name": "Venue One"}},
                        }
                    ],
                }
            return {
                "meta": {"count": 1000},
                "results": [
                    {
                        "display_name": "Two",
                        "abstract_inverted_index": {"Another": [0], "paper": [1]},
                        "publication_date": "2023-01-02",
                        "primary_location": {"source": {"display_name": "Venue Two"}},
                    }
                ],
            }

        papers, stats = fetch_papers(self.config(per_page=1), self.topic_hierarchy(), fetcher)

        self.assertEqual(len(papers), 2)
        self.assertEqual(papers[0]["abstract"], "A paper")
        self.assertEqual(stats["written"], 2)
        self.assertEqual(calls[0][1]["sample"], 10)
        self.assertEqual(calls[0][1]["seed"], 123)
        self.assertEqual(calls[0][1]["page"], 1)
        self.assertEqual(calls[0][1]["per_page"], 1)
        self.assertEqual(
            calls[0][1]["select"],
            "display_name,abstract_inverted_index,publication_date,primary_location,locations",
        )
        self.assertEqual(calls[1][1]["page"], 2)

    def test_fetch_papers_generates_seed_when_sample_seed_is_null(self):
        calls = []

        def fetcher(url, params):
            calls.append((url, dict(params)))
            return {
                "meta": {"count": 1000},
                "results": [
                    {
                        "display_name": "One",
                        "abstract_inverted_index": {"A": [0], "paper": [1]},
                        "publication_date": "2023-01-01",
                        "primary_location": {"source": {"display_name": "Venue One"}},
                    }
                ],
            }

        papers, stats = fetch_papers(
            self.config(count=1, sample_seed=None),
            self.topic_hierarchy(),
            fetcher,
            seed_generator=lambda: 456,
        )

        self.assertEqual(len(papers), 1)
        self.assertEqual(stats["written"], 1)
        self.assertIsNone(stats["configured_sample_seed"])
        self.assertEqual(stats["effective_sample_seed"], 456)
        self.assertEqual(calls[0][1]["seed"], 456)

    def test_write_output_preserves_non_ascii_characters(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_path = Path(tmp) / "output" / "papers.json"
            write_output(
                output_path,
                self.config(),
                self.topic_hierarchy(),
                [
                    {
                        "title": "Curly quotes",
                        "abstract": "A “quoted” abstract.",
                        "publication_date": "2024-01-01",
                        "publication_venue": "Example Venue",
                    }
                ],
                {"requested": 1, "written": 1, "skipped": 0},
            )

            raw = output_path.read_text(encoding="utf-8")
            self.assertIn("“quoted”", raw)
            self.assertNotIn("\\u201c", raw)
            parsed = json.loads(raw)
            self.assertEqual(parsed["papers"][0]["abstract"], "A “quoted” abstract.")


if __name__ == "__main__":
    unittest.main()
