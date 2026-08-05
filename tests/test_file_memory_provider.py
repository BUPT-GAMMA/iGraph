from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from backend.agent.memory.file_provider import FileMemoryProvider


class FileMemoryProviderTest(unittest.TestCase):
    def test_write_read_update_and_delete_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = FileMemoryProvider(user_id="alice", root=Path(tmp))

            snapshot = provider.load_and_freeze(None)
            self.assertIn("Persistent File Memory", snapshot)
            self.assertTrue((Path(tmp) / "MEMORY.md").exists())

            written = provider.write(
                {
                    "type": "project",
                    "name": "Dataset preference",
                    "description": "Preferred graph dataset",
                    "content": "Use Cora for quick smoke tests.",
                }
            )
            self.assertTrue(written["ok"])

            by_path = provider.read({"path": written["path"]})
            self.assertEqual(by_path["meta"]["name"], "Dataset preference")
            self.assertIn("Cora", by_path["content"])

            updated = provider.update(
                {
                    "path": written["path"],
                    "content": "Use PubMed for heavier smoke tests.",
                }
            )
            self.assertTrue(updated["ok"])

            result = json.loads(
                provider.handle_tool_call(
                    "file_memory_read",
                    {"query": "PubMed", "limit": 1},
                )
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["items"][0]["path"], written["path"])

            deleted = provider.delete({"path": written["path"], "archive": True})
            self.assertTrue(deleted["ok"])
            self.assertTrue(deleted["archived_to"].startswith("_archive/"))

    def test_rejects_paths_outside_memory_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = FileMemoryProvider(root=Path(tmp))
            provider.initialize(None)

            for bad_path in ("../outside.md", "project/../../outside.md"):
                with self.subTest(path=bad_path):
                    with self.assertRaises(ValueError):
                        provider.read({"path": bad_path})


if __name__ == "__main__":
    unittest.main()
