"""Deploy the graph extracted from the complete user-provided novel."""
from pathlib import Path
from graph_engine.bundled_presets import install_bundled_preset

RESOURCES = Path(__file__).resolve().parent / "resources" / "tianlongbabu"
DATASET_ID = "ds_tianlongbabu_v1"
FILENAME = "tianlongbabu-characters.npz"


def install_novel_preset(data_root: Path, *, resources: Path = RESOURCES, server_root: Path | None = None):
    return install_bundled_preset(data_root, resources=resources, server_root=server_root)
