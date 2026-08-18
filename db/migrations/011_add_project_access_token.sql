-- 项目访问令牌
ALTER TABLE projects ADD COLUMN access_token TEXT DEFAULT '';
