"""Real BFF authorization and metadata; explicit native session records for unit tests."""
from fastapi import FastAPI, HTTPException

from backend.construction_access import install_construction_access
from backend.project_datasets import ProjectDatasetDirectory
from backend.projects import ProjectDirectory


def authority(root, sessions):
    directory = ProjectDatasetDirectory(root / "project_datasets.json", root / "projects")
    projects = ProjectDirectory(root / "projects.json")
    app = FastAPI()

    async def session_for(session_id, _request, user_id):
        session = sessions.get(session_id)
        if session is None or session["user_id"] != user_id:
            raise HTTPException(404, "session not found")
        return session

    install_construction_access(app, directory=directory, projects=projects,
        session_for=session_for, state_for=lambda session: session.get("session_state", {}))
    return app, directory, projects
