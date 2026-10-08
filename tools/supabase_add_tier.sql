-- Добавить колонку tier в licenses (lite = веб, pro = EXE)
-- Все существующие ключи становятся 'pro' (backward compatible)
ALTER TABLE public.licenses ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'pro';

-- Обновить RPC activate_key чтобы возвращала tier
CREATE OR REPLACE FUNCTION public.activate_key(p_key_hash TEXT, p_hwid TEXT)
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

    IF v_license.activations_count >= v_license.max_activations THEN
        -- Проверяем, не активирован ли уже этот HWID
        SELECT * INTO v_activation FROM public.activations
        WHERE license_id = v_license.id AND hwid = p_hwid;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('valid', false, 'reason', 'Max activations reached');
        END IF;
    END IF;

    SELECT * INTO v_activation FROM public.activations
    WHERE license_id = v_license.id AND hwid = p_hwid;
    IF FOUND THEN
        RETURN jsonb_build_object(
            'valid', true,
            'expires_at', COALESCE(v_license.expires_at::TEXT, 'forever'),
            'key_type', v_license.key_type,
            'tier', COALESCE(v_license.tier, 'pro')
        );
    END IF;

    INSERT INTO public.activations (license_id, hwid) VALUES (v_license.id, p_hwid);
    UPDATE public.licenses SET activations_count = activations_count + 1 WHERE id = v_license.id;

    RETURN jsonb_build_object(
        'valid', true,
        'expires_at', COALESCE(v_license.expires_at::TEXT, 'forever'),
        'key_type', v_license.key_type,
        'tier', COALESCE(v_license.tier, 'pro')
    );
END;
$$;
