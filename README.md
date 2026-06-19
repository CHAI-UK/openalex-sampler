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

## Google Scholar Health Samples

`fetch_scholar_health_samples.py` builds the larger workflow for Google
Scholar's Health & Medical Sciences h5-core papers:

```bash
python fetch_scholar_health_samples.py \
  --scholar-fixture config/scholar_health_top_papers.json \
  --output-root output/scholar_health_samples/run_001
```

The script first tries Google Scholar, but Scholar may block automated access.
When that happens, pass a JSON or CSV fixture with the top five journals and
top 20 article titles for each journal. A JSON fixture should look like:

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
