-- 工作流分布式锁（GraphRuntime 执行前获取，防止并发冲突）
CREATE TABLE IF NOT EXISTS run_locks (
    project_id   TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    acquired_at  REAL NOT NULL,
    heartbeat_at REAL NOT NULL
);
