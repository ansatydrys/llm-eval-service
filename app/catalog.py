"""Datasets, prompts and pricing: versioned, append-only entities."""

import hashlib
import json
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    Dataset,
    DatasetCase,
    DatasetVersion,
    ModelPricing,
    Prompt,
    PromptVersion,
)
from app.schemas import CaseIn, PricingIn


class DatasetError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"{len(errors)} invalid line(s)")
        self.errors = errors


MAX_REPORTED_ERRORS = 50


def parse_jsonl(raw: bytes, max_cases: int) -> list[CaseIn]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DatasetError([{"line": None, "error": "file is not valid UTF-8"}]) from None

    cases: list[CaseIn] = []
    errors: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(cases) + len(errors) >= max_cases:
            errors.append({"line": lineno, "error": f"dataset exceeds {max_cases} cases"})
            break
        try:
            case = CaseIn.model_validate_json(line)
        except ValidationError as exc:
            details = "; ".join(
                f"{'.'.join(map(str, e['loc'])) or 'line'}: {e['msg']}" for e in exc.errors()
            )
            errors.append({"line": lineno, "error": details})
            continue
        if case.id in seen:
            errors.append({"line": lineno, "error": f"duplicate id {case.id!r} (line {seen[case.id]})"})
            continue
        seen[case.id] = lineno
        cases.append(case)

    if not cases and not errors:
        errors.append({"line": None, "error": "dataset is empty"})
    if errors:
        raise DatasetError(errors[:MAX_REPORTED_ERRORS])
    return cases


def _hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _get_or_create_locked(session: Session, model: type[Dataset] | type[Prompt], name: str):
    session.execute(insert(model).values(name=name).on_conflict_do_nothing(index_elements=["name"]))
    # Row lock serializes version numbering for this name.
    return session.execute(select(model).where(model.name == name).with_for_update()).scalar_one()


def create_dataset_version(
    session: Session, name: str, cases: list[CaseIn]
) -> tuple[Dataset, DatasetVersion, bool]:
    """Append a new immutable version; identical content returns the latest version."""
    content_hash = _hash([c.model_dump() for c in cases])
    dataset = _get_or_create_locked(session, Dataset, name)
    latest = session.execute(
        select(DatasetVersion)
        .where(DatasetVersion.dataset_id == dataset.id)
        .order_by(DatasetVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None and latest.content_hash == content_hash:
        session.commit()
        return dataset, latest, False

    version = DatasetVersion(
        dataset_id=dataset.id,
        version=(latest.version + 1) if latest else 1,
        content_hash=content_hash,
        case_count=len(cases),
    )
    session.add(version)
    session.flush()
    session.add_all(
        DatasetCase(
            dataset_version_id=version.id,
            position=i,
            case_id=c.id,
            question=c.question,
            expected_answer=c.expected_answer,
            required_facts=c.required_facts,
            json_schema=c.json_schema,
        )
        for i, c in enumerate(cases)
    )
    session.commit()
    return dataset, version, True


def create_prompt_version(
    session: Session, name: str, template: str
) -> tuple[Prompt, PromptVersion, bool]:
    content_hash = _hash(template)
    prompt = _get_or_create_locked(session, Prompt, name)
    latest = session.execute(
        select(PromptVersion)
        .where(PromptVersion.prompt_id == prompt.id)
        .order_by(PromptVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is not None and latest.content_hash == content_hash:
        session.commit()
        return prompt, latest, False
    version = PromptVersion(
        prompt_id=prompt.id,
        version=(latest.version + 1) if latest else 1,
        template=template,
        content_hash=content_hash,
    )
    session.add(version)
    session.commit()
    return prompt, version, True


def add_pricing(session: Session, data: PricingIn) -> ModelPricing:
    row = ModelPricing(**data.model_dump())
    session.add(row)
    session.commit()
    return row


def find_pricing(session: Session, provider: str, model: str, on_date) -> ModelPricing | None:
    return session.execute(
        select(ModelPricing)
        .where(
            ModelPricing.provider == provider,
            ModelPricing.model == model,
            ModelPricing.effective_from <= on_date,
        )
        .order_by(ModelPricing.effective_from.desc())
        .limit(1)
    ).scalar_one_or_none()

