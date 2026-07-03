# OpenAlex Sampling Toolkit

A collection of Python command-line tools for building research-paper samples
using OpenAlex data:

| Script | Purpose |
| --- | --- |
| [`fetch_openalex_papers.py`](#openalex-api-sample) | Fetch a configurable random sample from the OpenAlex API. |
| [`fetch_medicine_topics.py`](#medicine-topic-batch) | Fetch an OpenAlex API paper sample for each Medicine Topic. |
| [`fetch_scholar_health_samples.py`](#google-scholar-health-samples) | Resolve Google Scholar Health h5-core papers in OpenAlex and build related samples. |
| [`fetch_openalex_papers_parquet.py`](#local-parquet-sample) | Sample from a locally stored OpenAlex snapshot. |
| [`build_topic_partitions.py`](#local-parquet-sample) | Build a topic-partitioned dataset for faster local Parquet sampling. |

## Shared Sampling Behavior

The API and local OpenAlex samplers share filters for:

- publication date range
- work type
- language
- Domain > Field > Subfield > Topic
- required fields, currently title and abstract

They also clean common noisy metadata, including HTML wrappers, LaTeX document
dumps, and missing abstracts.

## OpenAlex API Key

A free API key provides 10× the daily usage allowance of unauthenticated
requests. Get one from [OpenAlex API settings](https://openalex.org/settings/api),
then copy the environment template:

```bash
cp .env.example .env
```

Add your key to `.env`:

```dotenv
OPENALEX_API_KEY=your-api-key
```

All scripts that call the OpenAlex API load this variable automatically. An
exported `OPENALEX_API_KEY` takes precedence over the value in `.env`.

## OpenAlex API Sample

`fetch_openalex_papers.py` fetches a configurable random sample directly from
the OpenAlex API.

### Usage

Run the general-purpose API sampler with a config file:

```bash
python fetch_openalex_papers.py --config config/openalex_config_cs.json
```

The config controls the output path. Generated JSON files should go in `output/`,
which the script creates automatically.

You can also override the output path from the command line:

```bash
python fetch_openalex_papers.py --config config/openalex_config_cs.json --output output/papers_computer_vision.json
```

### Configuration and Options

This repo uses separate config files for different fetches, for example
[config/openalex_config_cs.json](config/openalex_config_cs.json) and
[config/openalex_config_medicine.json](config/openalex_config_medicine.json).

Useful fields to change:

- `count`: number of valid papers to write
- `from_publication_date` / `to_publication_date`: date range
- `domain`, `field`, `subfield`, `topic`: OpenAlex topic hierarchy
- `fields`: output fields, e.g. `title`, `abstract`, `publication_date`, `publication_venue`
- `sample_seed`: set to `null` for a fresh random sample each run, or an integer for reproducible output
- `output_path`: JSON file to write, usually inside `output/`

The script overwrites `output_path` if it already exists, so use separate config files or `--output` for separate runs.

### Output

The JSON file contains:

- `metadata`: config, resolved OpenAlex topic hierarchy, query details, and counts
- `papers`: list of parsed paper records

Each paper currently looks like:

```json
{
  "title": "...",
  "abstract": "...",
  "publication_date": "2024-01-01",
  "publication_venue": "..."
}
```

## Medicine Topic Batch

`fetch_medicine_topics.py` discovers all Topics under the OpenAlex Medicine
Field, then writes up to 2,000 English article papers per Topic from 2005 onward.

### Usage

Run the batch sampler:

```bash
python fetch_medicine_topics.py
```

Existing Topic files are skipped by default, so running the same command resumes
an interrupted batch. Use `--overwrite` to refetch them:

```bash
python fetch_medicine_topics.py --overwrite
```

### Configuration and Options

- `--output-root`: output directory for Subfield folders and `manifest.json`
- `--overwrite`: refetch existing Topic JSON files
- `--sample-seed`: fixed OpenAlex sample seed to reuse for every Topic

### Output

Output is grouped by Subfield under `output/medicine_2005_present/` by default:

```text
output/medicine_2005_present/
  Oncology/
    Cancer Treatment.json
  Emergency Medicine/
    Emergency Care.json
  manifest.json
```

The script updates `manifest.json` after each Topic. Each entry records the
Topic, Subfield, output path, requested and written paper counts, skipped paper
count, and status.

## Google Scholar Health Samples

`fetch_scholar_health_samples.py` resolves Google Scholar Health & Medical
Sciences h5-core papers in OpenAlex, then builds related paper samples.

### Usage

```bash
python fetch_scholar_health_samples.py --output-root output/scholar_health_samples/run_001
```

### Configuration and Options

The script tries to read the top Health & Medical Sciences journals and h5-core
paper titles from Google Scholar. Scholar may block automated access, so for
repeatable runs you can provide a hand-prepared JSON or CSV fixture:

```bash
python fetch_scholar_health_samples.py \
  --scholar-fixture path/to/scholar_health_top_papers.json \
  --output-root output/scholar_health_samples/run_001
```

A JSON fixture should look like:

```json
{
  "journals": [
    {
      "name": "Nature Medicine",
      "h5_index": 279,
      "h5_median": 459,
      "articles": [
        {"title": "Example h5-core paper title", "year": 2021}
      ]
    }
  ]
}
```

- `--scholar-fixture`: JSON or CSV input used instead of scraping Google Scholar
- `--output-root`: output directory; defaults to a timestamped directory under `output/`
- `--count`: papers to sample for each resolved parent paper
- `--sample-seed`: optional OpenAlex sample seed
- `--per-page`: OpenAlex API page size, up to 100

### Output

Outputs are written under the output root, with a `manifest.json` plus one
folder per resolved parent paper containing:

- `<slug>.json`
- `papers_before_<slug>.json`
- `references_<slug>.json`

For `papers_before_<slug>.json`, the sample starts on January 1 of the year
five years before the parent paper's publication date and ends on the parent
paper's publication date. For example, a parent published on `2020-02-28`
samples from `2015-01-01` through `2020-02-28`.

## Local Parquet Sample

`fetch_openalex_papers_parquet.py` samples from a downloaded OpenAlex works
snapshot without calling the API. It supports both the original, update-date
layout and the Topic-partitioned layout produced by the supporting
`build_topic_partitions.py` utility.

### Usage

Install DuckDB:

```bash
python3 -m pip install -r requirements.txt
```

The downloaded snapshot can be sampled directly, but its update-date layout
requires every source file to be scanned for a Topic query. For faster or
repeated sampling, build a Topic-partitioned copy once:

```bash
python build_topic_partitions.py \
  --input /path/to/openalex-snapshot/works-parquet \
  --output /path/to/openalex-snapshot/works-by-topic-parquet \
  --threads 2 \
  --memory-limit 8GB \
  --max-open-files 4 \
  --log-file output/build_topic_partitions.log
```

The builder leaves the original snapshot unchanged. The build can take several
hours, so consider using `tmux`, `screen`, a job scheduler, or an equivalent
tool. Re-running the same command resumes completed checkpoints.

After the build, sample from the optimized dataset:

```bash
python fetch_openalex_papers_parquet.py \
  --config config/openalex_config_cs_parquet.json \
  --snapshot /path/to/openalex-snapshot/works-by-topic-parquet \
  --output output/papers_cs_optimized.json
```

### Configuration and Options

The example Parquet config accepts `snapshot_path`, so `--snapshot` can be
omitted when the config already points to the optimized directory. Sampling
filters use the same config fields as the OpenAlex API sampler.

Builder options:

- `--batch-size`: source Parquet files per resumable checkpoint
- `--threads`: DuckDB worker threads
- `--memory-limit`: maximum memory available to DuckDB
- `--max-open-files`: maximum simultaneous partition writers
- `--compression`: `ZSTD` or `SNAPPY` output compression
- `--log-file`: persistent progress log path

Sampler options:

- `--config`: JSON sampling configuration
- `--snapshot`: override the config's local snapshot path
- `--output`: override the config's output JSON path

### Output

The builder checkpoints source-file batches into bounded-memory buckets, then
writes compact Parquet files grouped by primary Topic:

```text
openalex-snapshot/works-by-topic-parquet/
  data/
    topic_id=T10036/
      part_....parquet
  work-id-index/
    id_bucket=0/
      part_....parquet
  manifest.json
  topics.json
```

Topic records retain the fields needed by the sampler plus `doi`,
`referenced_works`, and `cited_by_count`. `work-id-index/` maps every work ID to
its primary-Topic partition. A successful build removes the temporary `.build/`
directory and logs `Topic-partitioned dataset complete`.

The sampler writes the same JSON structure as the API sampler. It uses seeded
hash-based random sampling, records the effective seed and snapshot date in the
metadata, and does not have the API's 10,000-paper limit.

## Tests

Tests live in `tests/` and use Python's standard `unittest` framework. Run the
complete suite from the repository root:

```bash
python -m unittest -v
```

To run one test module:

```bash
python -m unittest -v tests.test_fetch_openalex_papers
```
