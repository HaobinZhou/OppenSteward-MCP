"""Narrow MCP adapter for the Stepwise R project-owned Freeze workspace."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from .catalog import AccessDenied


class Freezes:
    def __init__(self, catalog):
        self.catalog = catalog
        self.settings = catalog.settings
        self.module = None

    def storage_module(self):
        if self.module is None:
            guide = self.settings.skill_guide("stepwise-r-project")
            source = guide.parent / "scripts" / "freeze_store.py"
            if not source.is_file():
                raise ValueError("Installed Stepwise R skill lacks the Freeze storage helper")
            spec = importlib.util.spec_from_file_location("stepwise_freeze_store", source)
            if spec is None or spec.loader is None:
                raise ValueError("Unable to load the Stepwise R Freeze storage helper")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.module = module
        return self.module

    def store(self, project_id: str):
        project = self.catalog.project(project_id)
        if project.skill != "stepwise-r-project" or project.version != "v3":
            raise AccessDenied("Freeze is available only for registered Stepwise R v3 projects")
        if not self.settings.freeze_allowed(project.root):
            raise AccessDenied("Freeze is not enabled for this project")
        root = Path(project.root)
        if self.catalog.excluded(root / "Freeze"):
            raise AccessDenied("Freeze workspace is excluded")
        stat = os.stat(root, follow_symlinks=False)
        if (stat.st_dev, stat.st_ino) != (project.device, project.inode):
            raise AccessDenied("Project root changed during access")
        return self.storage_module().FreezeStore(root)

    def snapshot(self, project_id: str):
        return self.store(project_id).snapshot()

    def read(self, project_id: str, question_id: str, include_example: bool = False):
        return self.store(project_id).read_question(question_id, include_example)

    @staticmethod
    def actor(value: str):
        if value not in {"chatgpt", "codex"}:
            raise AccessDenied("MCP actor must be chatgpt or codex")
        return value

    def add(self, project_id: str, questions: list[dict], request_id: str, actor: str = "chatgpt"):
        actor = self.actor(actor)
        return self.store(project_id).add_questions(questions, request_id=request_id, actor=actor)

    def change(self, project_id: str, question_id: str, operation: str, value: object,
               expected_revision: int, request_id: str, actor: str = "chatgpt"):
        actor = self.actor(actor)
        if operation not in {"comment", "ai_position", "example", "reopen"}:
            raise AccessDenied("MCP may only discuss, revise an AI opinion or example, or reopen a question")
        return self.store(project_id).change(question_id, operation, value,
            expected_revision=expected_revision, request_id=request_id, actor=actor)
