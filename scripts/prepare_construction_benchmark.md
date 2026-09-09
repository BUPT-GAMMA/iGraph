Use `prepare_construction_benchmark.py` to create an explicit, bounded sample for
construction acceptance tests from GalaxyWeaver's `metadata.yaml` and parquet
tables. It requires `pyarrow` and `PyYAML` in the Python environment.

```sh
python scripts/prepare_construction_benchmark.py \
  /path/to/galaxyweaver/data/stackexchange \
  --output /tmp/igraph-construction-benchmark/stackexchange.sql \
  --seed-rows 20 --max-rows 5000
```

Replace `/path/to/galaxyweaver/data/stackexchange` with your dataset directory.
Exported SQL, manifests, and original acceptance data are local artifacts and are
not included in the source release.

The exporter selects the first rows of each physical table deterministically,
then fetches their referenced original rows recursively, including self references.
It preserves original scalar attributes, PKs, FK roles, composite PKs, and duplicate
rows without a PK. Per-column metadata links to composite PKs are rejected because
that format does not unambiguously describe composite foreign keys.

PK discovery scans only key columns in bounded batches. Fetching properties reads
the matching parquet row groups in batches; parquet files with large row groups
can still require substantial decoding. `--max-key-scan-rows` and
`--max-property-rows` bound cumulative work separately for each table. Exceeding a
bound fails explicitly. `--max-rows`, `--max-rounds`, and `--max-sql-bytes` prevent
an unbounded export; the script never labels an incomplete closure successful.

The default missing-reference policy removes entire sampled rows with FK targets
absent from a real source table, cascading this removal until the remaining sample
is closed. It records every removal, without changing attribute values or inventing
source rows. Use `--missing-reference-policy error` to reject such data instead.
For referenced entity tables missing from metadata, key-only virtual entities may
be induced from distinct surviving sampled FK values. They are clearly marked in
the manifest, have no fabricated business properties, and can be disabled using
`--no-virtual-entities`. Missing files for real tables always fail.

The companion `.manifest.json` lists source paths, sizes, modification times,
original cardinalities, exact physical source row indices, sampled cardinalities,
closure additions, removals, virtual entities, scan work, limits, and SQL/metadata
hashes. It explicitly labels the artifact a sample, never the full dataset. The
exporter validates SQLite referential integrity before writing both artifacts
outside the source directory. Task-label parquet files are excluded.

```sh
python -m unittest discover -s tests -p test_prepare_construction_benchmark.py -v
```
