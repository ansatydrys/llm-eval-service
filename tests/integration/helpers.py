import json
from typing import Any

from fastapi.testclient import TestClient

TEMPLATE_V1 = "Answer the question.\n\nQuestion: {{question}}\nAnswer:"
TEMPLATE_V2 = "You are a precise assistant. Reply briefly.\n\n{{question}}"


def jsonl(cases: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(c) + "\n" for c in cases).encode()


def upload(client: TestClient, name: str, payload: bytes, expect: int = 201) -> dict[str, Any]:
    r = client.post(f"/datasets/{name}/versions", files={"file": ("d.jsonl", payload)})
    assert r.status_code == expect, r.text
    return r.json()


def prompt(client: TestClient, name: str = "qa", template: str = TEMPLATE_V1) -> dict[str, Any]:
    r = client.post(f"/prompts/{name}/versions", json={"template": template})
    assert r.status_code == 201, r.text
    return r.json()


def start_run(client: TestClient, dataset_version_id: int, prompt_version_id: int,
              expect: int = 202, **extra: Any) -> dict[str, Any]:
    body = {"dataset_version_id": dataset_version_id, "prompt_version_id": prompt_version_id, **extra}
    r = client.post("/runs", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def simple_cases(n: int) -> list[dict[str, Any]]:
    return [
        {"id": f"c{i}", "question": f"q{i}?", "expected_answer": f"answer {i}"} for i in range(n)
    ]
