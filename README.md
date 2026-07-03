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
