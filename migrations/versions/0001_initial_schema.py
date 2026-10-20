"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-20 14:40:08.069372
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

IMMUTABLE_TABLES = ("dataset_versions", "dataset_cases", "prompt_versions")

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('datasets',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('model_pricing',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('input_usd_per_1m', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('output_usd_per_1m', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('effective_from', sa.Date(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('provider', 'model', 'effective_from')
    )
    op.create_table('prompts',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('dataset_versions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('dataset_id', sa.BigInteger(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('case_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['datasets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_id', 'version')
    )
    op.create_table('prompt_versions',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('prompt_id', sa.BigInteger(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('template', sa.Text(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['prompt_id'], ['prompts.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('prompt_id', 'version')
    )
    op.create_table('dataset_cases',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('dataset_version_id', sa.BigInteger(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('case_id', sa.String(length=128), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('expected_answer', sa.Text(), nullable=False),
    sa.Column('required_facts', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('json_schema', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('dataset_version_id', 'case_id')
    )
    op.create_index(op.f('ix_dataset_cases_dataset_version_id'), 'dataset_cases', ['dataset_version_id'], unique=False)
    op.create_table('runs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('dataset_version_id', sa.BigInteger(), nullable=False),
    sa.Column('prompt_version_id', sa.BigInteger(), nullable=False),
    sa.Column('provider', sa.String(length=32), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('config', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('case_count', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'running', 'completed', 'partial', 'failed')", name='runs_status_check'),
    sa.ForeignKeyConstraint(['dataset_version_id'], ['dataset_versions.id'], ),
    sa.ForeignKeyConstraint(['prompt_version_id'], ['prompt_versions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_runs_dataset_version_id'), 'runs', ['dataset_version_id'], unique=False)
    op.create_table('case_results',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('case_id', sa.String(length=128), nullable=False),
    sa.Column('output', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('error_kind', sa.String(length=32), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('passed', sa.Boolean(), nullable=False),
    sa.Column('scores', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('latency_ms', sa.Float(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=True),
    sa.Column('output_tokens', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'case_id')
    )
    op.create_index(op.f('ix_case_results_run_id'), 'case_results', ['run_id'], unique=False)
    op.create_table('run_jobs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('run_id', sa.BigInteger(), nullable=False),
    sa.Column('case_id', sa.String(length=128), nullable=False),
    sa.Column('dataset_case_pk', sa.BigInteger(), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('max_attempts', sa.Integer(), nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_owner', sa.String(length=128), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("status IN ('queued', 'running', 'succeeded', 'failed')", name='run_jobs_status_check'),
    sa.ForeignKeyConstraint(['dataset_case_pk'], ['dataset_cases.id'], ),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'case_id')
    )
    op.create_index(op.f('ix_run_jobs_run_id'), 'run_jobs', ['run_id'], unique=False)
    # Claim paths: fresh/retry-scheduled jobs and running jobs with an expired lease.
    op.create_index(
        'ix_run_jobs_queued', 'run_jobs', ['available_at'],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index(
        'ix_run_jobs_running_lease', 'run_jobs', ['lease_expires_at'],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.execute(
        """
        CREATE FUNCTION reject_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% is immutable', TG_TABLE_NAME;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_mutation()"
        )


def downgrade() -> None:
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER {table}_immutable ON {table}")
    op.execute("DROP FUNCTION reject_mutation()")
    op.drop_index('ix_run_jobs_running_lease', table_name='run_jobs')
    op.drop_index('ix_run_jobs_queued', table_name='run_jobs')
    op.drop_index(op.f('ix_run_jobs_run_id'), table_name='run_jobs')
    op.drop_table('run_jobs')
    op.drop_index(op.f('ix_case_results_run_id'), table_name='case_results')
    op.drop_table('case_results')
    op.drop_index(op.f('ix_runs_dataset_version_id'), table_name='runs')
    op.drop_table('runs')
    op.drop_index(op.f('ix_dataset_cases_dataset_version_id'), table_name='dataset_cases')
    op.drop_table('dataset_cases')
    op.drop_table('prompt_versions')
    op.drop_table('dataset_versions')
    op.drop_table('prompts')
    op.drop_table('model_pricing')
    op.drop_table('datasets')
