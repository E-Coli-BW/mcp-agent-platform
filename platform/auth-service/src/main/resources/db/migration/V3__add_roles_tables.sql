CREATE TABLE roles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL UNIQUE,
    description VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE user_roles (
    user_id BIGINT REFERENCES auth_users(id) ON DELETE CASCADE,
    role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

CREATE TABLE role_permissions (
    role_id INTEGER REFERENCES roles(id) ON DELETE CASCADE,
    permission VARCHAR(50) NOT NULL,
    PRIMARY KEY (role_id, permission)
);

-- Seed default roles
INSERT INTO roles (name, description) VALUES
  ('SUPER_ADMIN', 'Full system access'),
  ('TENANT_ADMIN', 'Tenant-level administration'),
  ('USER', 'Standard user access'),
  ('VIEWER', 'Read-only access');

-- Seed permissions
INSERT INTO role_permissions (role_id, permission) VALUES
  (1, '*'),
  (2, 'USER_MANAGE'), (2, 'MEMORY_READ'), (2, 'MEMORY_WRITE'), (2, 'CHAT'), (2, 'SETTINGS'), (2, 'AUDIT_READ'),
  (3, 'MEMORY_READ'), (3, 'MEMORY_WRITE'), (3, 'CHAT'), (3, 'SETTINGS_SELF'),
  (4, 'MEMORY_READ'), (4, 'CHAT_READ');
