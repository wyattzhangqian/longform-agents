-- 补 users 表缺失的 last_login_at 列。
-- 015 建表时漏了此列，但 core/security/users.py 读(_row_to_user:160)写(登录刷新:218)它，
-- 且 PG schema(db/schema.pg.extras.sql)与 database.py 内联建表都含此列。此处对 SQLite 补齐。
ALTER TABLE users ADD COLUMN last_login_at TEXT DEFAULT '';
