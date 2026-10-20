from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, HTTPException, Path, Query, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import catalog, runs
from app.config import Settings, get_settings
from app.db import get_session
from app.models import Dataset, DatasetVersion, Prompt, PromptVersion
from app.schemas import (
    DatasetVersionOut,
    PricingIn,
    PromptVersionIn,
    PromptVersionOut,
    RunCreate,
    RunOut,
)

app = FastAPI(
    title="LLM Evaluation Service",
    description="Run datasets against prompt versions, store per-case results, detect regressions.",
    version="0.1.0",
)

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
Name = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# --- datasets -------------------------------------------------------------------------

@app.post("/datasets/{name}/versions", response_model=DatasetVersionOut, status_code=201)
def upload_dataset(
    name: Name, session: SessionDep, settings: SettingsDep, file: Annotated[UploadFile, File()]
) -> DatasetVersionOut:
    raw = file.file.read(settings.max_upload_bytes + 1)
    if len(raw) > settings.max_upload_bytes:
        raise HTTPException(413, f"dataset exceeds {settings.max_upload_bytes} bytes")
    try:
        cases = catalog.parse_jsonl(raw, settings.max_dataset_cases)
    except catalog.DatasetError as exc:
        raise HTTPException(422, {"message": str(exc), "errors": exc.errors}) from None
    try:
        dataset, version, created = catalog.create_dataset_version(session, name, cases)
    except IntegrityError:
        raise HTTPException(409, "concurrent upload for this dataset, retry") from None
    return DatasetVersionOut(
        dataset=dataset.name,
        dataset_version_id=version.id,
        version=version.version,
        case_count=version.case_count,
        content_hash=version.content_hash,
        created=created,
        created_at=version.created_at,
    )


@app.get("/datasets")
def list_datasets(session: SessionDep) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Dataset.name, DatasetVersion.id, DatasetVersion.version, DatasetVersion.case_count)
        .join(DatasetVersion, DatasetVersion.dataset_id == Dataset.id)
        .order_by(Dataset.name, DatasetVersion.version)
    ).all()
    return [
        {"dataset": n, "dataset_version_id": i, "version": v, "case_count": c}
        for n, i, v, c in rows
    ]


# --- prompts --------------------------------------------------------------------------

@app.post("/prompts/{name}/versions", response_model=PromptVersionOut, status_code=201)
def create_prompt_version(name: Name, body: PromptVersionIn, session: SessionDep) -> PromptVersionOut:
    try:
        prompt, version, created = catalog.create_prompt_version(session, name, body.template)
    except IntegrityError:
        raise HTTPException(409, "concurrent update for this prompt, retry") from None
    return PromptVersionOut(
        prompt=prompt.name,
        prompt_version_id=version.id,
        version=version.version,
        content_hash=version.content_hash,
        created=created,
        created_at=version.created_at,
    )


@app.get("/prompts")
def list_prompts(session: SessionDep) -> list[dict[str, Any]]:
    rows = session.execute(
        select(Prompt.name, PromptVersion.id, PromptVersion.version, PromptVersion.template)
        .join(PromptVersion, PromptVersion.prompt_id == Prompt.id)
        .order_by(Prompt.name, PromptVersion.version)
    ).all()
    return [
        {"prompt": n, "prompt_version_id": i, "version": v, "template": t} for n, i, v, t in rows
    ]


# --- pricing --------------------------------------------------------------------------

@app.post("/pricing", status_code=201)
def add_pricing(body: PricingIn, session: SessionDep) -> dict[str, Any]:
    try:
        row = catalog.add_pricing(session, body)
    except IntegrityError:
        raise HTTPException(409, "pricing for this model and date already exists") from None
    return {"id": row.id, **body.model_dump(mode="json")}


# --- runs -----------------------------------------------------------------------------

@app.post("/runs", response_model=RunOut, status_code=202)
def create_run(body: RunCreate, session: SessionDep, settings: SettingsDep) -> RunOut:
    try:
        run = runs.create_run(session, body, settings)
    except runs.NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except runs.RunError as exc:
        raise HTTPException(422, str(exc)) from None
    return runs.run_out(session, run.id)


@app.get("/runs/compare")
def compare_runs(
    session: SessionDep,
    base: Annotated[int, Query(description="baseline run id")],
    head: Annotated[int, Query(description="candidate run id")],
) -> dict[str, Any]:
    try:
        return runs.compare(session, base, head)
    except runs.NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except runs.RunError as exc:
        raise HTTPException(422, str(exc)) from None


@app.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, session: SessionDep) -> RunOut:
    try:
        return runs.run_out(session, run_id)
    except runs.NotFound as exc:
        raise HTTPException(404, str(exc)) from None


@app.get("/runs/{run_id}/report")
def get_report(
    run_id: int, session: SessionDep, include_cases: bool = True
) -> dict[str, Any]:
    try:
        return runs.report(session, run_id, include_cases)
    except runs.NotFound as exc:
        raise HTTPException(404, str(exc)) from None
