# OpenAlex Paper Fetcher

Small Python script for fetching a random sample of OpenAlex papers into JSON.

It currently filters by:

- publication date range
- work type
- language
- Domain > Field > Subfield > Topic
- required fields, currently title and abstract

It also cleans common noisy metadata, including HTML wrappers, LaTeX document dumps, and missing abstracts.

## Usage

Run with a config file:

```bash
python fetch_openalex_papers.py --config config/openalex_config_cs.json
```

The config controls the output path. Generated JSON files should go in `output/`,
which the script creates automatically.

You can also override the output path from the command line:

```bash
python fetch_openalex_papers.py --config config/openalex_config_cs.json --output output/papers_computer_vision.json
```

## Config

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

## Output

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

## Tests

```bash
python -m unittest -v
```

## Medicine Topic Batch

To fetch a batch of Medicine papers for every OpenAlex Medicine Topic, run:

```bash
python fetch_medicine_topics.py
```

This discovers all Topics under the OpenAlex Medicine Field, then writes up to
2,000 English article papers per Topic from 2005 onward. Output is grouped by
Subfield folder under `output/medicine_2005_present/`, for example:

```text
output/medicine_2005_present/
  Oncology/
    Cancer Treatment.json
  Emergency Medicine/
    Emergency Care.json
  manifest.json
```

The batch script updates `manifest.json` after each Topic. Manifest entries
record the Topic, Subfield, output path, requested paper count, written paper
count, skipped paper count, and status.

Existing Topic files are skipped by default so interrupted runs can be resumed:

```bash
python fetch_medicine_topics.py
```

Use `--overwrite` to refetch files that already exist:

```bash
python fetch_medicine_topics.py --overwrite
```

Useful batch options:

- `--output-root`: output directory for Subfield folders and `manifest.json`
- `--overwrite`: refetch existing Topic JSON files
- `--mailto`: email address to include in OpenAlex API requests
- `--sample-seed`: fixed OpenAlex sample seed to reuse for every Topic

## Google Scholar Health Samples

`fetch_scholar_health_samples.py` builds the larger workflow for Google
Scholar's Health & Medical Sciences h5-core papers:

```bash
python fetch_scholar_health_samples.py --output-root output/scholar_health_samples/run_001
```

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

Outputs are written under the output root, with a `manifest.json` plus one
folder per resolved parent paper containing:

- `<slug>.json`
- `papers_before_<slug>.json`
- `references_<slug>.json`

For `papers_before_<slug>.json`, the sample starts on January 1 of the year
five years before the parent paper's publication date and ends on the parent
paper's publication date. For example, a parent published on `2020-02-28`
samples from `2015-01-01` through `2020-02-28`.

## Local Parquet Workflow

To sample from a downloaded OpenAlex works snapshot instead of calling the API,
install DuckDB:

```bash
python3 -m pip install -r requirements.txt
```

The downloaded snapshot is partitioned by update date, so querying it directly by
Topic still scans every source file. Build a separate optimized copy once:

```bash
python build_topic_partitions.py \
  --input /path/to/openalex-snapshot/works-parquet \
  --output /path/to/openalex-snapshot/works-by-topic-parquet \
  --threads 2 \
  --memory-limit 8GB \
  --max-open-files 4 \
  --log-file output/build_topic_partitions.log
```

The builder leaves the original snapshot unchanged. It checkpoints source-file
batches into bounded-memory buckets, then writes compact Parquet files grouped by
primary Topic. The optimized output looks like:

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
its primary-Topic partition so reference IDs can be located without scanning all
Topics.

The build can take several hours. Consider running it in a persistent terminal
session using `tmux`, `screen`, a job scheduler, or an equivalent tool. Re-running
the same command resumes completed batch and compaction checkpoints. A successful
build creates top-level `manifest.json` and `topics.json`, removes the temporary
`.build/` directory, and logs `Topic-partitioned dataset complete`.

After the build, sample from the optimized dataset:

```bash
python fetch_openalex_papers_parquet.py \
  --config config/openalex_config_cs_parquet.json \
  --snapshot /path/to/openalex-snapshot/works-by-topic-parquet \
  --output output/papers_cs_optimized.json
```

The local sampler applies the same publication-date, work-type, language,
primary-Topic, required-field, and text-cleaning rules as the API sampler. It
uses seeded hash-based random sampling, records the effective seed and snapshot
date in the output metadata, and does not have the API's 10,000-paper limit.

The example Parquet config also accepts `snapshot_path`, so `--snapshot` can be
omitted when the config already points to the optimized directory.

Useful builder options:

- `--batch-size`: source Parquet files per resumable checkpoint
- `--threads`: DuckDB worker threads
- `--memory-limit`: maximum memory available to DuckDB
- `--max-open-files`: maximum simultaneous partition writers
- `--compression`: `ZSTD` or `SNAPPY` output compression
- `--log-file`: persistent progress log path

Useful local sampler options:

- `--config`: JSON sampling configuration
- `--snapshot`: override the config's local snapshot path
- `--output`: override the config's output JSON path
