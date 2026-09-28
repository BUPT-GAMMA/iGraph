"""Offline/server acceptance entry point; does not start or contact main agents."""
import argparse
import json
from pathlib import Path

from graph_data_service.construction import _atomic_json
from graph_engine.construction.agent import ConstructionFailed, TabularConstructionSubAgent
from graph_engine.construction.experience import DomainExperienceStore


def main():
    parser = argparse.ArgumentParser(description="Build and validate a tabular or semantic-TXT property graph")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--options", type=Path, help="JSON options, including optional CSV/TXT metadata and fixed queries")
    parser.add_argument("--query-count", type=int)
    parser.add_argument("--max-iterations", type=int)
    parser.add_argument("--mode", choices=("llm", "rules"))
    parser.add_argument("--txt-mode", choices=("auto", "lines", "entities"))
    parser.add_argument("--domain-category", choices=("auto", "finance", "academic", "commerce", "healthcare", "social", "general"))
    parser.add_argument("--experience-dir", type=Path, help="Private reusable strategy directory; defaults to OUTPUT_PARENT/.construction-experience")
    parser.add_argument("--no-experience", action="store_true", help="Do not read or write cross-run experience")
    args = parser.parse_args()
    options = json.loads(args.options.read_text(encoding="utf-8")) if args.options else {}
    for key in ("query_count", "max_iterations", "mode", "txt_mode", "domain_category"):
        value = getattr(args, key)
        if value is not None:
            options[key] = value
    if args.no_experience:
        options["experience_enabled"] = False
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("--output must be a new or empty run directory; old graphs must not be mistaken for this build")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        experience = DomainExperienceStore((args.experience_dir or args.output.parent / ".construction-experience") / "strategies.sqlite3")
        result = TabularConstructionSubAgent(experience_store=experience).run(args.source, options, report_path=args.output / "evolution-report.json")
    except ConstructionFailed as exc:
        print(json.dumps({"status": "failed", "code": exc.code, "report": str(args.output / "evolution-report.json")}))
        return 1
    _atomic_json(args.output / "graph.json", result.graph)
    summary = {"status": "ready", "construction_kind": result.report.get("construction_kind", "relational"),
               "accepted_candidates": result.report["accepted_candidates"], "output": str(args.output)}
    if result.report.get("construction_kind") == "text_entities":
        summary.update(nodes=len(result.graph["nodes"]), edges=len(result.graph["edges"]),
                       chunks=result.report["semantic_extraction"]["chunk_count"])
    elif result.report["config"]["mode"] == "rules":
        summary.update(nodes=len(result.graph["nodes"]), edges=len(result.graph["edges"]),
                       validation=result.report["final"])
    else:
        summary.update(correct=result.report["final"]["correct"], total=result.report["final"]["total"])
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
