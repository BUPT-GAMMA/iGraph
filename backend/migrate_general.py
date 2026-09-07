"""Inspect first: python -m backend.migrate_general [--apply]."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from backend.config import BackendConfig
from backend.general_migration import GeneralMigration
from backend.projects import ProjectDirectory
from backend.project_datasets import ProjectDatasetDirectory
from backend.service_client import ServiceClient
from execution_protocol import service_headers
from execution_lifecycle import atomic_json


async def migrate(apply=False):
    settings = BackendConfig.from_env()
    projects = ProjectDirectory(settings.projects_file)
    data_root = settings.projects_file.parent.parent if settings.projects_file.parent.name == 'backend' else settings.projects_file.parent
    datasets = ProjectDatasetDirectory(
        Path(os.getenv('BACKEND_PROJECT_DATASETS_FILE', str(settings.projects_file.with_name('project_datasets.json')))).resolve(),
        Path(os.getenv('BACKEND_PROJECT_DATA_ROOT', str(data_root / 'projects'))).resolve())
    for path in (projects.path, datasets.metadata_path):
        if path.exists():
            json.loads(path.read_text())  # Fail explicitly for damaged metadata.
    runtime = ServiceClient(settings.agent_runtime_url, settings.request_timeout, settings.stream_timeout)
    graph = ServiceClient(settings.graph_engine_url, settings.request_timeout, settings.stream_timeout)
    migration = GeneralMigration(projects, datasets, runtime, settings.agent_id, graph)
    owners = {project.user_id for project in projects._load().values()}
    if settings.users_file.exists():
        owners.update(row['user_id'] for row in json.loads(settings.users_file.read_text()).get('users', []))
    for manifest in (projects.path.parent / 'migrations' / 'general-v1').glob('*/manifest.json'):
        owners.add(json.loads(manifest.read_text())['user_id'])
    reports = []
    for uid in sorted(owners):
        report = await migration.run(uid, apply=apply)
        if report:
            reports.append(report)
        if apply:
            sessions = await migration.sessions(uid)
            for offset in range(0, len(sessions), 100):
                await graph.request('POST', '/internal/artifacts/ownership', headers=service_headers(uid),
                    json_body={'sessions': [{'session_id': row['session_id'],
                        'project_id': (row.get('session_state') or {}).get('project_id')}
                        for row in sessions[offset:offset + 100]]})
    artifacts = await graph.request('GET', '/internal/artifacts/ownership-report',
                                    headers=service_headers(settings.admin_user_id))
    output = {'applied': apply, 'migrations': reports, 'artifacts': artifacts}
    if apply:
        atomic_json(projects.path.parent / 'migrations' / 'general-v1' / 'report.json', output)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Back up and apply; default is read-only inspection')
    args = parser.parse_args()
    asyncio.run(migrate(args.apply))
