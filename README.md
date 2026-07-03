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
