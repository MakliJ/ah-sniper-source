-- ============================================================
-- Supabase Schema for AH Sniper License System
-- Выполни этот SQL в Supabase SQL Editor (https://supabase.com)
-- 1. Зайди в проект → SQL Editor
-- 2. Вставь и выполни
-- 3. Скопируй URL проекта и anon key → в settings.json
-- ============================================================

-- Таблица лицензий
CREATE TABLE IF NOT EXISTS public.licenses (
    id SERIAL PRIMARY KEY,
    key_hash TEXT UNIQUE NOT NULL,              -- SHA256(plain_key)
    key_type TEXT NOT NULL DEFAULT 'single',    -- 'single' | 'promo'
    max_activations INTEGER NOT NULL DEFAULT 1, -- для single=1, для promo=N
    activations_count INTEGER NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ,                    -- NULL = бессрочно
    created_at TIMESTAMPTZ DEFAULT NOW(),
    created_by TEXT DEFAULT 'cli'
);

-- Таблица активаций (HWID)
CREATE TABLE IF NOT EXISTS public.activations (
    id SERIAL PRIMARY KEY,
    license_id INTEGER REFERENCES public.licenses(id) ON DELETE CASCADE,
    hwid TEXT NOT NULL,                         -- SHA256(volume|MAC|processor)
    activated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(license_id, hwid)                    -- один ключ = один HWID
);

-- Функция активации (вызывается из EXE анонимно)
CREATE OR REPLACE FUNCTION public.activate_key(p_key_hash TEXT, p_hwid TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_license public.licenses;
    v_activation public.activations;
    v_result JSONB;
BEGIN
    -- Ищем ключ
    SELECT * INTO v_license FROM public.licenses WHERE key_hash = p_key_hash;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('valid', false, 'reason', 'Key not found');
    END IF;

    -- Проверяем срок
    IF v_license.expires_at IS NOT NULL AND v_license.expires_at < NOW() THEN
        RETURN jsonb_build_object('valid', false, 'reason', 'Key expired');
    END IF;

    -- Проверяем лимит активаций
    IF v_license.activations_count >= v_license.max_activations THEN
        RETURN jsonb_build_object('valid', false, 'reason', 'Max activations reached');
    END IF;

    -- Проверяем, не активирован ли уже этот HWID
    SELECT * INTO v_activation FROM public.activations
    WHERE license_id = v_license.id AND hwid = p_hwid;
    IF FOUND THEN
        -- Уже активирован на этом HWID → OK (повторная активация)
        RETURN jsonb_build_object(
            'valid', true,
            'expires_at', COALESCE(v_license.expires_at::TEXT, 'forever'),
            'key_type', v_license.key_type
        );
    END IF;

    -- Активируем
    INSERT INTO public.activations (license_id, hwid) VALUES (v_license.id, p_hwid);
    UPDATE public.licenses SET activations_count = activations_count + 1 WHERE id = v_license.id;

    RETURN jsonb_build_object(
        'valid', true,
        'expires_at', COALESCE(v_license.expires_at::TEXT, 'forever'),
        'key_type', v_license.key_type
    );
END;
$$;

-- Функция проверки (вызывается периодически из EXE)
CREATE OR REPLACE FUNCTION public.check_key(p_key_hash TEXT, p_hwid TEXT)
RETURNS JSONB
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
DECLARE
    v_license public.licenses;
    v_activation public.activations;
BEGIN
    SELECT * INTO v_license FROM public.licenses WHERE key_hash = p_key_hash;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('valid', false, 'reason', 'Key not found');
    END IF;

    IF v_license.expires_at IS NOT NULL AND v_license.expires_at < NOW() THEN
        RETURN jsonb_build_object('valid', false, 'reason', 'Key expired');
    END IF;

    SELECT * INTO v_activation FROM public.activations
    WHERE license_id = v_license.id AND hwid = p_hwid;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('valid', false, 'reason', 'HWID not registered');
    END IF;

    RETURN jsonb_build_object(
        'valid', true,
        'expires_at', COALESCE(v_license.expires_at::TEXT, 'forever'),
        'key_type', v_license.key_type
    );
END;
$$;

-- Индексы
CREATE INDEX IF NOT EXISTS idx_licenses_hash ON public.licenses(key_hash);
CREATE INDEX IF NOT EXISTS idx_activations_hwid ON public.activations(hwid);
CREATE INDEX IF NOT EXISTS idx_activations_license ON public.activations(license_id);

-- Row Level Security (RLS)
ALTER TABLE public.licenses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.activations ENABLE ROW LEVEL SECURITY;

-- Анонимный доступ только к RPC функциям (безопасность)
REVOKE ALL ON public.licenses FROM anon;
REVOKE ALL ON public.activations FROM anon;
GRANT EXECUTE ON FUNCTION public.activate_key TO anon;
GRANT EXECUTE ON FUNCTION public.check_key TO anon;
