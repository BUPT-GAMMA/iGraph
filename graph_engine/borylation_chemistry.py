"""Read reproducible descriptors only for the exact packaged molecular structure."""
import hashlib
import json
from pathlib import Path

PROFILE = Path(__file__).parent / "resources/borylation/processed/chemistry_profile.json"

def structure_fingerprint(case):
    payload = {key: case[key] for key in ("molecule_id", "smiles", "atoms", "chemical_bonds")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()

def chemistry_profile(case):
    try:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Packaged chemistry profile unavailable") from exc
    if profile.get("structure_sha256") != structure_fingerprint(case):
        raise ValueError("Molecular structure differs from the descriptor source")
    return profile
