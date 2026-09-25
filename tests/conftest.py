from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_db
from app import job_worker
from app.main import app
from app.science.service import MockScienceService
from app.storage.fake_drive_service import FakeDriveService


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True)
def reset_database(tmp_path):
    old_executor = job_worker.executor
    old_executor.shutdown(wait=True)

    job_worker.executor = ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="sarguardian-test-job",
    )

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

    job_worker.science_service_override = MockScienceService(
        tmp_path / "science-results"
    )
    job_worker.drive_service_override = FakeDriveService()
    job_worker.session_factory_override = TestingSessionLocal

    yield

    job_worker.executor.shutdown(wait=True)
    job_worker.science_service_override = None
    job_worker.drive_service_override = None
    job_worker.session_factory_override = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)
