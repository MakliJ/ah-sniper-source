/* Shared web / EXE preset UI. All saves pass through the durable outbox. */
var _autoSaveTimer = null, _presetStore = null, _presetScope = '';
var _activePresetId = null, _presetEditBase = null, _presetLoadRequest = 0;
var _presetUiBaseline = {};
function presetLocalGet(suffix) { try { return localStorage.getItem(_presetScope + ':' + suffix); } catch (e) { return null; } }
function presetLocalSet(suffix, value) { try { localStorage.setItem(_presetScope + ':' + suffix, value); } catch (e) {} }
function presetStatus(state, result) {
    var el = document.getElementById('presetSaveStatus');
    var ru = {pending: 'Сохраняю…', saved: 'Сохранено', offline: 'На устройстве · ждёт синхронизации',
        storage: 'Память браузера недоступна · скачайте пресеты', blocked: 'Черновик сохранён · нужна проверка', auth: 'Войдите снова · черновик сохранён'};
    var en = {pending: 'Saving…', saved: 'Saved', offline: 'On this device · sync pending',
        storage: 'Browser storage unavailable · export presets', blocked: 'Draft saved · needs attention', auth: 'Sign in again · draft saved'};
    el.textContent = (_lang === 'en' ? en : ru)[state] || '';
    el.dataset.state = state;
    el.title = result && result.reason || el.textContent;
    var details = document.getElementById('presetSyncDetails');
    if (details) details.textContent = el.title;
    if (state === 'auth' && details) {
        var link = document.createElement('a'); link.href = '/login'; link.textContent = uiText(' Войти →', ' Sign in →'); details.append(link);
    }
}
function initPresetStore(env) {
    if (!env || !env.preset_scope) return;
    _presetScope = 'ah-presets:' + env.preset_scope;
    // Older EXE versions used one localhost scope. Copy once, retaining the old
    // record as a recovery source; future EXE copies have separate path scopes.
    var legacyOwner;
    try { legacyOwner = localStorage.getItem('ah-presets:legacy-local-owner'); } catch (e) {}
    if (env.preset_scope.startsWith('local:') && !presetLocalGet('pending') && (!legacyOwner || legacyOwner === _presetScope)) {
        ['pending', 'list', 'last'].forEach(function (suffix) {
            try { var old = localStorage.getItem('ah-presets:local:' + suffix); if (old) presetLocalSet(suffix, old); } catch (e) {}
        });
        if (!presetLocalGet('last')) { try { presetLocalSet('last', localStorage.getItem('lastPreset') || ''); } catch (e) {} }
        try { localStorage.setItem('ah-presets:legacy-local-owner', _presetScope); } catch (e) {}
    }
    _presetStore = PresetStore.create(localStorage, _presetScope + ':pending', function (data, keepalive) {
        return api('/api/presets', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data), keepalive: keepalive, cache: 'no-store'});
    }, presetStatus, function (list, mapping) {
        if (mapping && String(_activePresetId) === String(mapping.from)) {
            _activePresetId = mapping.to;
            presetLocalSet('last', String(mapping.to));
        }
        renderPresetMenus(list);
    });
    setInterval(function () { if (_presetStore.hasPending()) _presetStore.flush(); }, 15000);
    window.addEventListener('online', function () { _presetStore.flush(); });
    window.addEventListener('storage', function (e) {
        if (e.key === _presetScope + ':pending') _presetStore.sync();
    });
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') _presetStore.flush(true); });
    window.addEventListener('pagehide', function () { _presetStore.flush(true); });
}
async function fetchPresets() {
    if (!_presetStore) return null;
    return _presetStore.refresh(function () { return api('/api/presets', {cache: 'no-store'}); });
}
function renderPresetMenus(pl) {
    var select = document.getElementById('presetSel'), list = document.getElementById('presetList');
    var current = _activePresetId == null ? select.value : String(_activePresetId);
    select.replaceChildren(new Option(t('presets_select'), ''));
    list.replaceChildren();
    pl.forEach(function (p) {
        var pending = _presetStore.hasPending(p.id);
        select.add(new Option((p.is_default ? '★ ' : '') + (p.name || 'Preset ' + p.id) + (pending ? ' ·' : ''), String(p.id)));
        var row = document.createElement('div'); row.className = 'preset-row';
        var edit = document.createElement('button'); edit.className = 'preset-edit';
        edit.textContent = (p.is_default ? '★ ' : '') + (p.name || 'Preset ' + p.id);
        edit.onclick = function () { editPreset(p.id); };
        var state = document.createElement('small'); state.textContent = pending ? uiText('На устройстве', 'On this device') : '';
        var remove = document.createElement('button'); remove.className = 'icon-button danger';
        remove.textContent = '×'; remove.setAttribute('aria-label', uiText('Удалить ', 'Delete ') + p.name);
        remove.onclick = function () { delPreset(p.id); };
        row.append(edit, state, remove); list.append(row);
    });
    select.value = current;
    if (!pl.length) list.textContent = uiText('Создайте пресет из текущих фильтров.', 'Create a preset from your current filters.');
}
async function loadPresets() {
    if (!_presetStore) return;
    var request = ++_presetLoadRequest;
    function accept(pl) {
        renderPresetMenus(pl);
        if (_activePresetId != null) return;
        var last = presetLocalGet('last');
        var p = pl.find(function (p) { return String(p.id) === String(_presetStore.resolve(last)); }) ||
            pl.find(function (p) { return p.is_default; }) || pl[0];
        if (p) { document.getElementById('presetSel').value = String(p.id); applyPresetData(p); }
    }
    accept(_presetStore.list());
    var pl = await fetchPresets();
    if (request === _presetLoadRequest && pl) accept(pl);
}
function parsePresetBoe(value) {
    if (Array.isArray(value)) return JSON.parse(JSON.stringify(value));
    try { var parsed = JSON.parse(value || '[]'); return Array.isArray(parsed) ? parsed : []; } catch (e) { return []; }
}
function applyPresetData(p) {
    if (!p) return;
    p = _presetStore ? _presetStore.overlay(p) : p;
    _activePresetId = p.id;
    document.getElementById('presetSel').value = String(p.id);
    presetLocalSet('last', String(p.id));
    _itemsSnipe = p.items_enabled !== false;
    document.getElementById('itemIds').value = Array.isArray(p.item_ids) ? p.item_ids.join(',') : p.item_ids || '';
    document.getElementById('saleRealms').value = Array.isArray(p.sale_realm_ids) ? p.sale_realm_ids.join(',') : p.sale_realm_ids || '';
    document.getElementById('minDiscount').value = p.discount || 0;
    document.getElementById('minAvg').value = p.min_top10avg || 0;
    _boeFilters = parsePresetBoe(p.boe_filters);
    _boeSnipe = p.boe_enabled !== false && (p.boe_enabled === true || _boeFilters.length > 0);
    document.getElementById('boeSnipe').checked = _boeSnipe;
    syncItemsSnipe(); renderBoeFilters(); renderRealmPicker(); renderItemSelection();
    _presetUiBaseline = currentPresetFields();
    closeItemPicker();
    applyFilters();
    if (!_isWeb) apiPost('/browser/api/snipe/reset', {item_ids: selectedItemIds()});
}
function applyPreset() {
    clearTimeout(_autoSaveTimer);
    if (!_presetStore) return;
    _presetStore.flush();
    var id = document.getElementById('presetSel').value;
    if (id === '') { _activePresetId = null; return; }
    var p = _presetStore.list().find(function (p) { return String(p.id) === id; });
    if (p) applyPresetData(p);
}
function currentPresetFields() {
    return {item_ids: selectedItemIds(), sale_realm_ids: selectedRealmIds(),
        discount: parseInt(document.getElementById('minDiscount').value) || 0,
        min_top10avg: parseInt(document.getElementById('minAvg').value) || 0,
        boe_filters: JSON.stringify(_boeFilters), boe_enabled: _boeSnipe, items_enabled: _itemsSnipe};
}
function autoSavePreset() {
    renderItemSelection();
    if (!_presetStore || _activePresetId == null) return;
    var current = currentPresetFields(), patch = {id: _activePresetId};
    Object.keys(current).forEach(function (key) {
        if (JSON.stringify(current[key]) !== JSON.stringify(_presetUiBaseline[key])) patch[key] = current[key];
    });
    _presetUiBaseline = current;
    if (Object.keys(patch).length === 1) return;
    _presetStore.put(patch);
    clearTimeout(_autoSaveTimer);
    _autoSaveTimer = setTimeout(function () { _presetStore.flush(); }, 700);
}
function fillPresetForm(p) {
    _presetEditBase = JSON.parse(JSON.stringify(p));
    document.getElementById('presetForm').style.display = 'block';
    document.getElementById('pfId').value = p.id == null ? '' : p.id;
    document.getElementById('pfName').value = p.name || '';
    document.getElementById('pfItems').value = Array.isArray(p.item_ids) ? p.item_ids.join(',') : p.item_ids || '';
    document.getElementById('pfRealms').value = Array.isArray(p.sale_realm_ids) ? p.sale_realm_ids.join(',') : p.sale_realm_ids || '';
    document.getElementById('pfDiscount').value = p.discount || 0;
    document.getElementById('pfAvg').value = p.min_top10avg || 0;
    document.getElementById('pfItemsEnabled').checked = p.items_enabled !== false;
    document.getElementById('pfDefault').checked = !!p.is_default;
    document.getElementById('pfName').focus();
}
function newPreset() { switchTab('settings'); fillPresetForm(Object.assign({name: '', region: _sbRegion || 'eu'}, currentPresetFields())); }
function editPreset(id) {
    var p = _presetStore && _presetStore.list().find(function (p) { return String(p.id) === String(id); });
    if (p) fillPresetForm(p);
}
function cancelPreset() { document.getElementById('presetForm').style.display = 'none'; _presetEditBase = null; }
async function savePreset() {
    if (!_presetStore || !_presetEditBase) return;
    var name = document.getElementById('pfName').value.trim();
    if (!name) { document.getElementById('pfName').focus(); return; }
    var data = Object.assign({}, _presetEditBase, {name: name,
        item_ids: parseItemIds(document.getElementById('pfItems').value),
        sale_realm_ids: parseItemIds(document.getElementById('pfRealms').value),
        discount: Number(document.getElementById('pfDiscount').value) || 0,
        min_top10avg: Number(document.getElementById('pfAvg').value) || 0,
        items_enabled: document.getElementById('pfItemsEnabled').checked,
        is_default: document.getElementById('pfDefault').checked});
    var isNew = data.id == null;
    if (isNew) {
        data.client_id = window.crypto && crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
        data.id = 'draft:' + data.client_id;
    }
    // Existing presets only receive fields actually edited in this form.
    if (!isNew) {
        var edited = {id: data.id};
        ['name', 'item_ids', 'sale_realm_ids', 'discount', 'min_top10avg', 'items_enabled', 'is_default'].forEach(function (key) {
            var previous = _presetEditBase[key];
            if (key === 'item_ids' || key === 'sale_realm_ids') previous = parseItemIds(Array.isArray(previous) ? previous.join(',') : previous);
            if (JSON.stringify(data[key]) !== JSON.stringify(previous)) edited[key] = data[key];
        });
        data = edited;
    }
    var saved = _presetStore.put(data);
    if (isNew || String(data.id) === String(_activePresetId)) applyPresetData(saved);
    cancelPreset();
    var ok = await _presetStore.flush();
    toast(ok ? t('preset_saved') : uiText('Пресет на устройстве. Повторим синхронизацию автоматически.', 'Preset saved on this device. Sync will retry automatically.'), ok ? 'ok' : 'info');
}
async function quickSavePreset() {
    if (_activePresetId == null) { newPreset(); return; }
    autoSavePreset(); if (_presetStore) await _presetStore.flush();
}
async function delPreset(id) {
    if (!_presetStore || !confirm(t('delete_preset'))) return;
    id = _presetStore.resolve(id);
    if (!String(id).startsWith('draft:')) {
        await _presetStore.flush();
        var result = await api('/api/presets?id=' + encodeURIComponent(id), {method: 'DELETE'});
        if (!result || !result.ok) { toast(t('error_save'), 'err'); return; }
    }
    if (String(_activePresetId) === String(id)) _activePresetId = null;
    _presetStore.discard(id); loadPresets();
}
function exportLocalPresets() {
    if (!_presetStore) return;
    var blob = new Blob([JSON.stringify(_presetStore.list(), null, 2)], {type: 'application/json'});
    var url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = 'presets.json'; link.click();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
}
async function importPresetsFile(input) {
    var file = input.files[0]; if (!file || !_presetStore) return;
    try {
        var imported = JSON.parse(await file.text());
        if (!Array.isArray(imported) || !imported.every(function (p) { return p && typeof p === 'object' && !Array.isArray(p); })) throw Error(uiText('Ожидается список пресетов.', 'Expected a list of presets.'));
        if (_isWeb && _tier === 'none' && imported.length + _presetStore.list().length > 1) throw Error(uiText('Free: доступен один пресет.', 'Free allows one preset.'));
        // Validate the entire file before adding any drafts. Imports add copies;
        // existing presets and their unsent edits remain intact.
        imported.forEach(function (p) {
            var filters = typeof p.boe_filters === 'string' ? JSON.parse(p.boe_filters || '[]') : p.boe_filters || [];
            if (!Array.isArray(filters) || !filters.every(function (f) { return f && typeof f === 'object' && !Array.isArray(f); })) throw Error(uiText('Неверный формат BoE-фильтров.', 'Invalid BoE filters.'));
            p.boe_filters = JSON.stringify(filters);
        });
        imported.forEach(function (p) {
            var token = crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2);
            _presetStore.put(Object.assign({}, p, {id: 'draft:' + token, client_id: token, is_default: false,
                name: p.name || 'Imported preset', items_enabled: p.items_enabled !== false}));
        });
        loadPresets();
        var ok = await _presetStore.flush();
        toast(t('presets_loaded') + imported.length + (ok ? '' : uiText(' · на устройстве', ' · on this device')), ok ? 'ok' : 'info');
    } catch (e) { toast(e.message || t('error'), 'err'); }
    input.value = '';
}
