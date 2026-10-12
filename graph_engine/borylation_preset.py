"""Install the versioned molecular case using the existing NPZ contract."""
from pathlib import Path

RESOURCES = Path(__file__).resolve().parent / "resources" / "borylation"

def install_borylation_preset(data_root: Path, *, resources: Path = RESOURCES,
                              server_root: Path | None = None) -> dict:
    from graph_engine.bundled_presets import install_bundled_preset
    return install_bundled_preset(data_root, resources=resources, server_root=server_root)
