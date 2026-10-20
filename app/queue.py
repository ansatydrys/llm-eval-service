"""PostgreSQL-backed job queue with leases.

Guarantees:
  - A job is held by at most one worker at a time: claiming uses
    `FOR UPDATE SKIP LOCKED` and sets a lease (`lease_owner`, `lease_expires_at`).
  - A worker may only finish a job while it still owns an unexpired lease. A worker
    whose lease expired (it was too slow or presumed dead) has its result rejected.
  - `case_results` has UNIQUE(run_id, case_id) and inserts use ON CONFLICT DO NOTHING,
    so a case can never get two results.

Delivery is at-least-once: if a worker crashes after the provider answered but before
its commit, the lease expires and another worker calls the provider again.

All timestamps come from the database clock (`now()`), never from worker clocks.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import CaseResult

TERMINAL_JOB_STATUSES = ("succeeded", "failed")


@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    run_id: int
    case_id: str
    dataset_case_pk: int
    attempt: int
    max_attempts: int


_CLAIM_SQL = text(
    """
    WITH candidate AS (
        SELECT j.id
        FROM run_jobs j
        WHERE j.attempts < j.max_attempts
          AND (
                (j.status = 'queued' AND j.available_at <= now())
             OR (j.status = 'running' AND j.lease_expires_at < now())
          )
        ORDER BY j.available_at, j.id
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    UPDATE run_jobs j
    SET status = 'running',
        attempts = j.attempts + 1,
        lease_owner = :owner,
        lease_expires_at = now() + make_interval(secs => CAST(:lease_s AS float8)),
        updated_at = now()
    FROM candidate
    WHERE j.id = candidate.id
    RETURNING j.id, j.run_id, j.case_id, j.dataset_case_pk, j.attempts, j.max_attempts
    """
)


def claim(session: Session, owner: str, lease_s: float) -> ClaimedJob | None:
    row = session.execute(_CLAIM_SQL, {"owner": owner, "lease_s": lease_s}).one_or_none()
    if row is None:
        session.commit()
        return None
    session.execute(
        text(
            "UPDATE runs SET status = 'running', started_at = coalesce(started_at, now()) "
            "WHERE id = :run_id AND status = 'queued'"
        ),
        {"run_id": row.run_id},
    )
    session.commit()
    return ClaimedJob(*row)


def _release(session: Session, job: ClaimedJob, owner: str, status: str, error: str | None,
             retry_delay_s: float | None = None) -> bool:
    """Move a job out of `running` iff this worker still owns a live lease."""
    result = session.execute(
        text(
            """
            UPDATE run_jobs
            SET status = :status,
                last_error = :error,
                lease_owner = NULL,
                lease_expires_at = NULL,
                available_at = CASE WHEN CAST(:delay AS float8) IS NULL THEN available_at
                                    ELSE now() + make_interval(secs => CAST(:delay AS float8)) END,
                updated_at = now()
            WHERE id = :id AND status = 'running' AND lease_owner = :owner
              AND attempts = :attempt AND lease_expires_at >= now()
            """
        ),
        {
            "status": status,
            "error": error,
            "delay": retry_delay_s,
            "id": job.job_id,
            "owner": owner,
            "attempt": job.attempt,
        },
    )
    return result.rowcount == 1


def _insert_result(session: Session, run_id: int, case_id: str, **values: Any) -> None:
    session.execute(
        insert(CaseResult)
        .values(run_id=run_id, case_id=case_id, **values)
        .on_conflict_do_nothing(index_elements=["run_id", "case_id"])
    )


def complete(session: Session, job: ClaimedJob, owner: str, **result: Any) -> bool:
    """Store a terminal result (success or final error). False if the lease was lost."""
    status = "succeeded" if result.get("error") is None else "failed"
    if not _release(session, job, owner, status, result.get("error")):
        session.rollback()
        return False
    _insert_result(session, job.run_id, job.case_id, attempts=job.attempt, **result)
    finalize_run(session, job.run_id)
    session.commit()
    return True


def schedule_retry(session: Session, job: ClaimedJob, owner: str, error: str,
                   delay_s: float) -> bool:
    ok = _release(session, job, owner, "queued", error, retry_delay_s=delay_s)
    if ok:
        session.commit()
    else:
        session.rollback()
    return ok


def reap_exhausted(session: Session) -> int:
    """Fail jobs whose lease expired on their last allowed attempt (worker died every time)."""
    rows = session.execute(
        text(
            """
            UPDATE run_jobs
            SET status = 'failed',
                last_error = coalesce(last_error || '; ', '')
                             || 'lease expired on final attempt (worker lost)',
                lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
            WHERE id IN (
                SELECT id FROM run_jobs
                WHERE status = 'running' AND lease_expires_at < now()
                  AND attempts >= max_attempts
                FOR UPDATE SKIP LOCKED
            )
            RETURNING run_id, case_id, attempts, last_error
            """
        )
    ).all()
    for row in rows:
        _insert_result(
            session,
            row.run_id,
            row.case_id,
            attempts=row.attempts,
            output=None,
            error=row.last_error,
            error_kind="lease_expired",
            passed=False,
            scores={},
        )
    for run_id in sorted({row.run_id for row in rows}):
        finalize_run(session, run_id)
    session.commit()

    # Safety net: finalize any active run that has no open jobs left.
    stale = session.scalars(
        text(
            """
            SELECT r.id FROM runs r
            WHERE r.status IN ('queued', 'running')
              AND NOT EXISTS (SELECT 1 FROM run_jobs j WHERE j.run_id = r.id
                              AND j.status NOT IN ('succeeded', 'failed'))
            """
        )
    ).all()
    for run_id in stale:
        finalize_run(session, run_id)
        session.commit()
    return len(rows)


def finalize_run(session: Session, run_id: int) -> None:
    """Set the terminal run status once no job is pending. Idempotent and race-safe:
    the UPDATE only applies while the run is still active and no job is unfinished.

    The run row is locked first so that two workers finishing the last two jobs
    concurrently are serialized: the second one sees the first one's commit (READ
    COMMITTED takes a fresh snapshot per statement) and finalizes the run.
    NO KEY UPDATE (not UPDATE) because inserting a case_result takes KEY SHARE on the
    run row through the foreign key; FOR UPDATE would conflict with it and deadlock.
    """
    session.execute(
        text("SELECT 1 FROM runs WHERE id = :run_id FOR NO KEY UPDATE"), {"run_id": run_id}
    )
    session.execute(
        text(
            """
            UPDATE runs r
            SET status = CASE
                    WHEN s.failed = 0 THEN 'completed'
                    WHEN s.failed = s.total THEN 'failed'
                    ELSE 'partial' END,
                finished_at = now(),
                started_at = coalesce(r.started_at, now())
            FROM (
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE status = 'failed') AS failed,
                       count(*) FILTER (WHERE status NOT IN ('succeeded', 'failed')) AS open
                FROM run_jobs WHERE run_id = :run_id
            ) s
            WHERE r.id = :run_id AND r.status IN ('queued', 'running') AND s.open = 0
            """
        ),
        {"run_id": run_id},
    )
