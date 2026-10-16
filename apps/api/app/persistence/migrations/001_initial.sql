-- Phase 4E.1 initial schema (schema_migrations is owned by the migrator)

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    instruction TEXT NOT NULL,
    validation_profile TEXT NOT NULL,
    status TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    error TEXT,
    plan_approved INTEGER NOT NULL DEFAULT 0,
    plan_json TEXT,
    approval_digest TEXT,
    quality_json TEXT NOT NULL,
    execution_json TEXT,
    workspace_relpath TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_tasks_lifecycle ON tasks(lifecycle);

CREATE TABLE task_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    from_lifecycle TEXT,
    to_lifecycle TEXT,
    step_id TEXT,
    error_category TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX idx_events_task_seq ON task_events(task_id, seq);
