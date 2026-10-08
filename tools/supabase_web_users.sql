-- Таблица веб-пользователей (email + пароль + ключ)
CREATE TABLE IF NOT EXISTS public.web_users (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    license_key TEXT,
    tier TEXT DEFAULT 'none',
    presets TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE public.web_users ENABLE ROW LEVEL SECURITY;

-- Сервисный доступ (для Flask backend с service_key)
-- RLS не блокирует service_role, так что доп. политики не нужны
