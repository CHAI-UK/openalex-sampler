import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

from fetch_scholar_health_samples import (
    OPENALEX_BASE_URL,
    ScholarArticle,
    ScholarInput,
    ScholarJournal,
    build_parent_topic_filters,
    clean_scholar_article_title,
    fetch_openalex_json_with_retries,
    fetch_references,
    five_year_start_date,
    load_scholar_fixture,
    normalize_title,
    openalex_search_titles,
    parent_record,
    resolve_parent_paper,
    run_workflow,
    sample_papers_before_parent,
    slugify,
    title_match_score,
    title_similarity,
)
from fetch_openalex_papers import FetchConfig


def abstract(words):
    return {word: [index] for index, word in enumerate(words.split())}


class ScholarHealthSampleTests(unittest.TestCase):
    def journal(self, **overrides):
        values = {
            "name": "Nature Medicine",
            "h5_index": 279,
            "h5_median": 459,
            "h5_core_url": None,
            "articles": [ScholarArticle("A Trial of Example Therapy", 2021)],
        }
        values.update(overrides)
        return ScholarJournal(**values)

    def parent_work(self, **overrides):
        values = {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.123/example",
            "display_name": "A Trial of Example Therapy",
            "abstract_inverted_index": abstract("This trial studies example therapy in patients."),
            "publication_date": "2021-03-01",
            "primary_location": {"source": {"display_name": "Nature Medicine"}},
            "locations": [],
            "primary_topic": {
                "id": "https://openalex.org/T123",
                "display_name": "Clinical Trials",
                "domain": {"id": "https://openalex.org/domains/4", "display_name": "Health Sciences"},
                "field": {"id": "https://openalex.org/fields/27", "display_name": "Medicine"},
                "subfield": {"id": "https://openalex.org/subfields/2701", "display_name": "General Medicine"},
            },
            "referenced_works": ["https://openalex.org/W10", "https://openalex.org/W11"],
            "cited_by_count": 100,
        }
        values.update(overrides)
        return values

    def test_load_scholar_json_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.json"
            path.write_text(
                json.dumps(
                    {
                        "journals": [
                            {
                                "name": "Nature Medicine",
                                "h5_index": 279,
                                "h5_median": 459,
                                "articles": [
                                    {"title": "A Trial of Example Therapy", "year": 2021},
                                    "A Second Paper",
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            journals = load_scholar_fixture(path)

        self.assertEqual(len(journals), 1)
        self.assertEqual(journals[0].name, "Nature Medicine")
        self.assertEqual(journals[0].h5_index, 279)
        self.assertEqual(journals[0].articles[0].title, "A Trial of Example Therapy")
        self.assertEqual(journals[0].articles[0].year, 2021)
        self.assertEqual(journals[0].articles[1].title, "A Second Paper")

    def test_fetch_openalex_json_with_retries_handles_429(self):
        headers = Message()
        headers["Retry-After"] = "0.25"
        calls = []
        sleeps = []

        def fetch_once(url, params):
            calls.append((url, dict(params)))
            if len(calls) == 1:
                raise HTTPError(url, 429, "Too Many Requests", headers, None)
            return {"results": []}

        result = fetch_openalex_json_with_retries(
            "https://api.openalex.org/works",
            {"search": "example"},
            max_retries=2,
            sleeper=sleeps.append,
            fetch_once=fetch_once,
        )

        self.assertEqual(result, {"results": []})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.25])

    def test_fetch_openalex_json_with_retries_eventually_raises(self):
        def fetch_once(url, params):
            raise HTTPError(url, 429, "Too Many Requests", Message(), None)

        with self.assertRaisesRegex(Exception, "OpenAlex HTTP error 429"):
            fetch_openalex_json_with_retries(
                "https://api.openalex.org/works",
                {"search": "example"},
                max_retries=1,
                sleeper=lambda seconds: None,
                fetch_once=fetch_once,
            )

    def test_fetch_openalex_json_with_retries_rejects_long_retry_after(self):
        headers = Message()
        headers["Retry-After"] = "40610"

        def fetch_once(url, params):
            raise HTTPError(url, 429, "Too Many Requests", headers, None)

        with self.assertRaisesRegex(Exception, "requested a long wait"):
            fetch_openalex_json_with_retries(
                "https://api.openalex.org/works",
                {"search": "example"},
                max_retries=2,
                max_delay_seconds=60,
                sleeper=lambda seconds: None,
                fetch_once=fetch_once,
            )

    def test_normalize_and_title_similarity(self):
        self.assertEqual(normalize_title("The <i>Trial</i>: of Therapy!"), "trial of therapy")
        self.assertGreater(
            title_similarity("A Trial of Example Therapy", "Trial of Example Therapy"),
            0.95,
        )
        self.assertEqual(
            title_match_score(
                "Critical Care Utilization for the COVID-19 Outbreak in Lombardy, Italy: "
                "Early Experience and Forecast During an Emergency Response",
                "Critical Care Utilization for the COVID-19 Outbreak in Lombardy, Italy",
            ),
            1.0,
        )
        self.assertEqual(openalex_search_titles("Main Title: Subtitle"), ["Main Title: Subtitle", "Main Title"])

    def test_clean_scholar_article_title_removes_author_and_citation_noise(self):
        self.assertEqual(clean_scholar_article_title("Title / Author"), "")
        self.assertEqual(
            clean_scholar_article_title(
                "Clinical Characteristics of Coronavirus Disease 2019 in China"
                "W Guan, Z Ni, Y Hu, W LiangNew England Journal of Medicine 382 (18), 1708-1720"
            ),
            "Clinical Characteristics of Coronavirus Disease 2019 in China",
        )
        self.assertEqual(
            clean_scholar_article_title(
                "The proximal origin of SARS-CoV-2"
                "KG Andersen, A Rambaut, WI LipkinNature Medicine 26 (4), 450-452"
            ),
            "The proximal origin of SARS-CoV-2",
        )
        self.assertEqual(
            clean_scholar_article_title(
                "SARS-CoV-2 Cell Entry Depends on ACE2 and TMPRSS2 and Is Blocked by a "
                "Clinically Proven Protease InhibitorM Hoffmann, H Kleine-Weber, S Schroeder"
                "Cell 181 (2), 271-280. e8"
            ),
            "SARS-CoV-2 Cell Entry Depends on ACE2 and TMPRSS2 and Is Blocked by a "
            "Clinically Proven Protease Inhibitor",
        )
        self.assertEqual(
            clean_scholar_article_title(
                "Targets of T Cell Responses to SARS-CoV-2 Coronavirus in Humans with "
                "COVID-19 Disease and Unexposed IndividualsA Grifoni, D Weiskopf"
                "Cell 181 (7), 1489-1501. e15"
            ),
            "Targets of T Cell Responses to SARS-CoV-2 Coronavirus in Humans with "
            "COVID-19 Disease and Unexposed Individuals",
        )
        self.assertEqual(
            clean_scholar_article_title(
                "Ferroptosis turns 10: Emerging mechanisms, physiological functions, "
                "and therapeutic applicationsBR StockwellCell 185 (14), 2401-2421"
            ),
            "Ferroptosis turns 10: Emerging mechanisms, physiological functions, "
            "and therapeutic applications",
        )
        self.assertEqual(
            clean_scholar_article_title("Hallmarks of aging: An expanding universeC López-Otín"),
            "Hallmarks of aging: An expanding universe",
        )
        self.assertEqual(
            clean_scholar_article_title("Persistent Symptoms in Patients After Acute COVID-19A Carfì"),
            "Persistent Symptoms in Patients After Acute COVID-19",
        )
        self.assertEqual(
            clean_scholar_article_title(
                "An inflammatory cytokine signature predicts COVID-19 severity and survivalDM Del Valle"
            ),
            "An inflammatory cytokine signature predicts COVID-19 severity and survival",
        )
        self.assertEqual(
            clean_scholar_article_title("Attributes and predictors of long COV"),
            "Attributes and predictors of long COVID",
        )

    def test_resolve_parent_paper_prefers_high_confidence_venue_match(self):
        calls = []

        def fetcher(url, params):
            calls.append((url, dict(params)))
            return {
                "results": [
                    self.parent_work(
                        id="https://openalex.org/W2",
                        display_name="A Trial of Example",
                        primary_location={"source": {"display_name": "Other Journal"}},
                        cited_by_count=1000,
                    ),
                    self.parent_work(),
                ]
            }

        match = resolve_parent_paper(
            ScholarArticle("A Trial of Example Therapy", 2021),
            self.journal(),
            fetcher=fetcher,
        )

        self.assertEqual(match.status, "resolved")
        self.assertEqual(match.work["id"], "https://openalex.org/W1")
        self.assertGreaterEqual(match.confidence, 0.99)
        self.assertEqual(calls[0][0], f"{OPENALEX_BASE_URL}/works")
        self.assertEqual(calls[0][1]["filter"], "language:en")

    def test_resolve_parent_paper_uses_main_title_fallback_for_subtitles(self):
        calls = []

        def fetcher(url, params):
            calls.append(dict(params))
            if params["search"].endswith("Emergency Response"):
                return {"results": []}
            return {
                "results": [
                    self.parent_work(
                        display_name="Critical Care Utilization for the COVID-19 Outbreak in Lombardy, Italy",
                        primary_location={"source": {"display_name": "JAMA"}},
                    )
                ]
            }

        article = ScholarArticle(
            "Critical Care Utilization for the COVID-19 Outbreak in Lombardy, Italy: "
            "Early Experience and Forecast During an Emergency Response",
            2021,
        )
        match = resolve_parent_paper(article, self.journal(name="JAMA"), fetcher=fetcher)

        self.assertEqual(match.status, "resolved")
        self.assertEqual(
            [call["search"] for call in calls],
            [
                article.title,
                "Critical Care Utilization for the COVID-19 Outbreak in Lombardy, Italy",
            ],
        )

    def test_resolve_parent_paper_rejects_low_confidence(self):
        def fetcher(url, params):
            return {
                "results": [
                    self.parent_work(
                        display_name="Completely Different Work",
                        primary_location={"source": {"display_name": "Other Journal"}},
                    )
                ]
            }

        match = resolve_parent_paper(
            ScholarArticle("A Trial of Example Therapy", 2021),
            self.journal(),
            fetcher=fetcher,
        )

        self.assertEqual(match.status, "unresolved")
        self.assertEqual(match.reason, "low_confidence_match")

    def test_resolve_parent_paper_allows_non_article_parent_types(self):
        def fetcher(url, params):
            self.assertEqual(params["filter"], "language:en")
            return {"results": [self.parent_work(type="letter")]}

        match = resolve_parent_paper(
            ScholarArticle("A Trial of Example Therapy", 2021),
            self.journal(),
            fetcher=fetcher,
        )

        self.assertEqual(match.status, "resolved")
        self.assertEqual(match.work["type"], "letter")

    def test_parent_record_allows_missing_openalex_abstract(self):
        record = parent_record(
            self.parent_work(abstract_inverted_index=None),
            ScholarArticle("A Trial of Example Therapy", 2021),
            self.journal(),
            1.0,
        )

        self.assertIsNotNone(record)
        self.assertEqual(record["title"], "A Trial of Example Therapy")
        self.assertIsNone(record["abstract"])

    def test_five_year_start_date_uses_cutoff_year_january_first(self):
        self.assertEqual(five_year_start_date("2021-03-01"), "2016-01-01")
        self.assertEqual(five_year_start_date("2020-02-29"), "2015-01-01")

    def test_build_parent_topic_filters(self):
        config = FetchConfig(
            count=2000,
            from_publication_date="1900-01-01",
            to_publication_date="2016-03-01",
            work_type="article",
            language="en",
            domain="Health Sciences",
            field="Medicine",
            subfield="General Medicine",
            topic="Clinical Trials",
            field_match="primary",
            sample_seed=42,
            required_fields=["title", "abstract"],
            fields=["title", "abstract", "publication_date", "publication_venue"],
            output_path="",
            per_page=100,
            require_english_text=True,
            require_clean_text=True,
        )

        self.assertEqual(
            build_parent_topic_filters(config, "https://openalex.org/T123"),
            "from_publication_date:1900-01-01,type:article,language:en,has_abstract:true,"
            "primary_topic.id:T123,to_publication_date:2016-03-01",
        )

    def test_fetch_references_batches_and_logs_missing(self):
        calls = []

        def fetcher(url, params):
            calls.append(dict(params))
            return {
                "results": [
                    {
                        "id": "https://openalex.org/W10",
                        "display_name": "Reference One",
                        "abstract_inverted_index": abstract("This is a reference."),
                        "publication_date": "2010-01-01",
                        "primary_location": {"source": {"display_name": "Journal One"}},
                    }
                ]
            }

        refs, stats = fetch_references(
            ["https://openalex.org/W10", "https://openalex.org/W11", "https://openalex.org/W10"],
            fetcher=fetcher,
            per_batch=100,
        )

        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["title"], "Reference One")
        self.assertEqual(stats["requested"], 2)
        self.assertEqual(stats["found"], 1)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(calls[0]["filter"], "openalex:W10|W11")

    def test_sample_papers_before_parent_builds_date_bounded_topic_sample(self):
        calls = []

        def fetcher(url, params):
            calls.append(dict(params))
            return {
                "meta": {"count": 25},
                "results": [
                    {
                        "display_name": "Older Topic Paper",
                        "abstract_inverted_index": abstract("This paper is old enough."),
                        "publication_date": "2015-01-01",
                        "primary_location": {"source": {"display_name": "Example Journal"}},
                    }
                ],
            }

        papers, stats = sample_papers_before_parent(
            self.parent_work(publication_date="2021-03-01"),
            count=1,
            sample_seed=7,
            per_page=100,
            fetcher=fetcher,
        )

        self.assertEqual(len(papers), 1)
        self.assertIn("primary_topic.id:T123", stats["filters"])
        self.assertIn("from_publication_date:2016-01-01", stats["filters"])
        self.assertIn("to_publication_date:2021-03-01", stats["filters"])
        self.assertEqual(calls[0]["sample"], 5)
        self.assertEqual(calls[0]["seed"], 7)

    def test_slugify_handles_collisions(self):
        used = set()
        self.assertEqual(slugify("The Example Paper!", used), "example-paper")
        self.assertEqual(slugify("Example Paper", used), "example-paper-2")

    def test_run_workflow_mixed_outcomes(self):
        parent = self.parent_work()
        dirty_parent = self.parent_work(
            id="https://openalex.org/W2",
            display_name="Hallmarks of aging: An expanding universe",
            abstract_inverted_index=abstract("This paper describes hallmarks of aging."),
            publication_date="2023-01-19",
            primary_location={"source": {"display_name": "Nature Medicine"}},
        )
        sample = {
            "display_name": "Older Topic Paper",
            "abstract_inverted_index": abstract("This paper is old enough."),
            "publication_date": "2015-01-01",
            "primary_location": {"source": {"display_name": "Example Journal"}},
        }
        reference = {
            "id": "https://openalex.org/W10",
            "display_name": "Reference One",
            "abstract_inverted_index": abstract("This is a reference."),
            "publication_date": "2010-01-01",
            "primary_location": {"source": {"display_name": "Journal One"}},
        }
        searches = []

        def fetcher(url, params):
            if params.get("search"):
                searches.append(params["search"])
            if params.get("search") == "A Trial of Example Therapy":
                return {"results": [parent]}
            if params.get("search") == "Hallmarks of aging: An expanding universe":
                return {"results": [dirty_parent]}
            if params.get("search") == "Missing Parent Paper":
                return {"results": []}
            if str(params.get("filter", "")).startswith("openalex:"):
                return {"results": [reference]}
            if "primary_topic.id:T123" in str(params.get("filter", "")):
                return {"meta": {"count": 1}, "results": [sample]}
            return {"results": []}

        scholar_input = ScholarInput(
            source_method="fixture",
            scrape_failures=["blocked"],
            journals=[
                self.journal(
                    articles=[
                        ScholarArticle("A Trial of Example Therapy", 2021),
                        ScholarArticle("Hallmarks of aging: An expanding universeC López-Otín", 2023),
                        ScholarArticle("Missing Parent Paper", 2021),
                    ]
                )
            ],
        )

        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_workflow(
                scholar_input,
                output_root=Path(tmp),
                count=1,
                sample_seed=11,
                per_page=100,
                fetcher=fetcher,
            )
            manifest_path = Path(tmp) / "manifest.json"
            folders = [path for path in Path(tmp).iterdir() if path.is_dir()]
            output_files = sorted(path.name for path in folders[0].iterdir())
            self.assertTrue(manifest_path.exists())

        self.assertIn("Hallmarks of aging: An expanding universe", searches)
        self.assertNotIn("Hallmarks of aging: An expanding universeC López-Otín", searches)
        self.assertEqual(len(folders), 2)
        self.assertIn("trial-of-example-therapy.json", output_files)
        self.assertNotIn("parent_trial-of-example-therapy.json", output_files)
        self.assertEqual(len(manifest["papers"]), 3)
        self.assertEqual(manifest["papers"][0]["status"], "resolved")
        self.assertEqual(manifest["papers"][0]["sample_stats"]["written"], 1)
        self.assertEqual(manifest["papers"][0]["reference_stats"]["accepted"], 1)
        self.assertEqual(manifest["papers"][1]["status"], "resolved")
        self.assertEqual(
            manifest["papers"][1]["raw_scholar_title"],
            "Hallmarks of aging: An expanding universeC López-Otín",
        )
        self.assertEqual(manifest["papers"][1]["scholar_title"], "Hallmarks of aging: An expanding universe")
        self.assertEqual(manifest["papers"][2]["status"], "unresolved")


if __name__ == "__main__":
    unittest.main()
