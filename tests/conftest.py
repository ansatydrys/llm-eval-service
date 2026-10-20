import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db import get_session
from app.main import app
from app.worker import Worker

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    if TEST_DATABASE_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL is not set")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def engine():
    engine = create_engine(TEST_DATABASE_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def sessions(engine) -> Iterator[sessionmaker[Session]]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE case_results, run_jobs, runs, dataset_cases, dataset_versions, "
                "datasets, prompt_versions, prompts, model_pricing RESTART IDENTITY CASCADE"
            )
        )
    yield sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=TEST_DATABASE_URL or "postgresql+psycopg://unused/unused",
        lease_seconds=60,
        max_timeout_s=30,
        retry_backoff_base_s=0,
        retry_backoff_max_s=0,
        poll_interval_s=0.05,
        max_dataset_cases=100,
        max_upload_bytes=200_000,
    )


@pytest.fixture
def client(sessions, settings) -> Iterator[TestClient]:
    def _session():
        with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def make_worker(sessions, settings):
    def _make(**kwargs) -> Worker:
        return Worker(sessions, settings, **kwargs)

    return _make


@pytest.fixture
def demo_jsonl() -> bytes:
    return (ROOT / "data" / "demo.jsonl").read_bytes()
