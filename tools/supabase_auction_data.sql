-- supabase_auction_data.sql — таблица auction_data (push из admin EXE, чтение вебом).
--
-- ⚠️ Эта миграция долгое время существовала только в живом проекте Supabase
-- и отсутствовала в репо (находка аудита 2026-08-23). Файл фиксирует актуальную схему.
--
-- Применение: SQL Editor в Supabase (project example-project).
-- Таблица уже существует в проде — файл идемпотентен (IF NOT EXISTS / DROP POLICY IF EXISTS).

-- 1) Таблица: одна строка на регион, items_json перезаписывается каждым пушем.
CREATE TABLE IF NOT EXISTS public.auction_data (
    region     text PRIMARY KEY,
    items_json text,
    updated_at timestamptz DEFAULT now()
);

-- 2) RLS: SELECT для anon — веб-клиенты читают напрямую с anon-ключом.
ALTER TABLE public.auction_data ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon read auction_data" ON public.auction_data;
CREATE POLICY "anon read auction_data"
    ON public.auction_data FOR SELECT
    TO anon
    USING (true);

-- Запись только через service_role (ключ НЕ должен быть ни в EXE, ни в браузере):
-- отдельной политики INSERT/UPDATE для anon НЕТ — это осознанно.
