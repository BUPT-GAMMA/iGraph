"""Optional smoke test against the CSV collections retained on a test host."""
from __future__ import annotations

import os
from contextlib import ExitStack
from pathlib import Path

import pytest

from backend.project_datasets import ProjectDatasetDirectory
from graph_engine.construction.sources import initial_graph, load_source


def test_movielens_multi_csv_bundle_from_external_fixture(tmp_path):
    fixture_root = os.getenv("CSV2GRAPH_DATASET_ROOT")
    if not fixture_root:
        pytest.skip("CSV2GRAPH_DATASET_ROOT is not configured")
    source_dir = Path(fixture_root) / "movielens"
    filenames = ["movies.csv", "links.csv", "ratings.csv", "tags.csv"]
    if not all((source_dir / filename).is_file() for filename in filenames):
        pytest.skip("MovieLens CSV fixture is unavailable")
    schema = {"version": "csv_bundle.v1", "tables": [
        {"file_name": "movies.csv", "name": "movies", "columns": ["movieId", "title", "genres"],
         "primary_key": ["movieId"], "foreign_keys": []},
        {"file_name": "links.csv", "name": "links", "columns": ["movieId", "imdbId", "tmdbId"],
         "primary_key": ["movieId"], "foreign_keys": [
             {"columns": ["movieId"], "ref_table": "movies", "ref_columns": ["movieId"]}]},
        {"file_name": "ratings.csv", "name": "ratings", "columns": ["userId", "movieId", "rating", "timestamp"],
         "primary_key": ["userId", "movieId"], "foreign_keys": [
             {"columns": ["movieId"], "ref_table": "movies", "ref_columns": ["movieId"]}]},
        {"file_name": "tags.csv", "name": "tags", "columns": ["userId", "movieId", "tag", "timestamp"],
         "primary_key": ["userId", "movieId", "tag", "timestamp"], "foreign_keys": [
             {"columns": ["movieId"], "ref_table": "movies", "ref_columns": ["movieId"]}]},
    ]}
    directory = ProjectDatasetDirectory(tmp_path / "datasets.json", tmp_path / "storage")
    with ExitStack() as stack:
        files = [(filename, stack.enter_context((source_dir / filename).open("rb"))) for filename in filenames]
        record = directory.create_csv_bundle(project_id=None, user_id="smoke", name="MovieLens",
                                             original_filename="movielens.csv", files=files, schema=schema)
    source = load_source(Path(record.storage_path))
    try:
        graph = initial_graph(source)
        row_counts = {name: len(table.rows) for name, table in source.tables.items()}
        assert len(graph["nodes"]) == sum(row_counts.values())
        assert len(graph["edges"]) == row_counts["links"] + row_counts["ratings"] + row_counts["tags"]
        assert source.tables["ratings"].primary_key == ["userId", "movieId"]
        assert source.tables["ratings"].foreign_keys[0].ref_table == "movies"
    finally:
        source.close()
