/* Item selection and shared, account-local presentation preferences. */
function uiText(ru, en) { return _lang === 'en' ? en : ru; }
Object.assign(T.ru, {tab_sniping: 'Снайпинг', tab_browser: 'Браузер', tab_realms: 'Реалмы', tab_logs: 'Логи', tab_settings: 'Настройки', search_ph: 'Поиск в таблице…', sale_realms: 'Реалмы продажи', cat_search_ph: 'Найти категорию…', api_settings: 'Подключение', sound: 'Уведомления', license: 'Лицензия', presets: 'Пресеты', save_settings: 'Сохранить настройки', new_preset: '+ Из текущих фильтров', min_discount: 'Скидка от, %', min_avg: 'Средняя от, g', grid_qty: 'Кол-во', grid_sale_price: 'Sale цена'});
Object.assign(T.en, {tab_sniping: 'Sniping', tab_browser: 'Browser', tab_realms: 'Realms', tab_logs: 'Logs', tab_settings: 'Settings', search_ph: 'Search this table…', cat_search_ph: 'Find a category…', api_settings: 'Connection', sound: 'Notifications', license: 'License', presets: 'Presets', save_settings: 'Save settings', new_preset: '+ From current filters'});
var workspaceLabels = {
    choose_items: ['Выбрать предметы', 'Choose items'], chosen: ['Выбрано', 'Selected'], paste_ids: ['Вставить ID вручную', 'Paste IDs manually'],
    add_group: ['Добавить группу', 'Add group'], choose_realms: ['Выбрать реалмы →', 'Choose realms →'], market_table: ['Рынок', 'Market'], offers: ['предложений', 'offers'],
    click_item_hint: ['Нажмите на предмет или цену · аналитика по реалмам', 'Click an item or price · realm analytics'], full_table_hint: ['Все столбцы · горизонтальная прокрутка', 'All columns · horizontal scroll'],
    catalog_hint: ['Каталог независим от фильтров снайпера.', 'The catalog is independent of sniper filters.'], catalog_star_hint: ['☆ — в снайп-лист · нажмите предмет для аналитики', '☆ — snipe list · click an item for analytics'],
    presets_hint: ['Фильтры сохраняются автоматически. При сбое облака черновик остаётся на этом устройстве.', 'Filters save automatically. During an outage, drafts stay on this device.'],
    export_local: ['Скачать копию', 'Export a copy'], sync_now: ['Синхронизировать', 'Sync now'], import_presets: ['Добавить пресеты из файла', 'Add presets from a file'],
    appearance: ['Рабочее пространство', 'Workspace'], appearance_hint: ['Настройте таблицу под себя. Полный набор аналитики остаётся доступен.', 'Make the table yours. All analytics remain available.'],
    theme: ['Тема', 'Theme'], theme_dark: ['Графит', 'Graphite'], theme_light: ['Светлая', 'Light'], theme_system: ['Системная', 'System'],
    density: ['Плотность таблицы', 'Table density'], density_compact: ['Компактная', 'Compact'], density_comfortable: ['Свободнее', 'Comfortable'],
    pin_item: ['Закрепить название предмета', 'Pin item names'], pin_sale: ['Закрепить цену продажи', 'Pin sale prices'], pin_above: ['Приоритетные предложения', 'Priority offers'],
    item_catalog: ['КАТАЛОГ ПРЕДМЕТОВ', 'ITEM CATALOG'], picker_search: ['Название или ID предмета…', 'Item name or ID…'], picker_hint: ['Выбор сразу в фильтре', 'Selections apply immediately'], done: ['Готово', 'Done']
};
Object.keys(workspaceLabels).forEach(function (key) { T.ru[key] = workspaceLabels[key][0]; T.en[key] = workspaceLabels[key][1]; });
var _itemNames = new Map(), _nameRequests = new Set();
var _picker = {target: null, request: 0, offset: 0, total: 0, selectedOnly: false, timer: null};
var _appearance = {theme: 'dark', density: 'compact', pinItem: true, pinSale: false, filters: true, browserFilters: true};
var _mobileFiltersOpen = false, _mobileBrowserFiltersOpen = false;
var _gridViewportMode = new WeakMap();
function parseItemIds(value) { return Array.from(new Set(String(value || '').split(/[\s,;]+/).filter(function (v) { return /^\d+$/.test(v) && Number(v) > 0 && Number.isSafeInteger(Number(v)); }).map(Number))); }
function selectionIds(target) { return target === 'items' ? selectedItemIds() : parseItemIds((_boeFilters[Number(target)] || {}).ids); }
function itemName(id) { var item = _itemNames.get(Number(id)); return item ? item.name : '#' + id; }
async function resolveItemNames(ids) {
    var missing = ids.filter(function (id) { return !_itemNames.has(id) && !_nameRequests.has(id); });
    if (!missing.length) return;
    for (var start = 0; start < missing.length; start += 100) {
        var group = missing.slice(start, start + 100); group.forEach(function (id) { _nameRequests.add(id); });
        var result = await api('/browser/api/picker?limit=100&ids=' + group.join(','));
        if (result && Array.isArray(result.items)) result.items.forEach(function (it) { _itemNames.set(it.id, it); });
        group.forEach(function (id) { _nameRequests.delete(id); });
    }
    renderItemSelection(false);
}
function chosenHtml(ids, target) {
    return ids.length ? ids.map(function (id) {
        return '<div class="chosen-item"><img loading="lazy" src="/browser/icon/' + id + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
            '<span title="' + escapeHtml(itemName(id)) + '">' + escapeHtml(itemName(id)) + '<small>#' + id + '</small></span>' +
            '<button type="button" class="icon-button" aria-label="' + uiText('Убрать ', 'Remove ') + escapeHtml(itemName(id)) + '" onclick="togglePickerItem(\'' + target + '\',' + id + ',false)">×</button></div>';
    }).join('') : '<div class="muted empty-selection">' + uiText('Выберите предметы для этого фильтра.', 'Choose items for this filter.') + '</div>';
}
function renderItemSelection(resolve) {
    var ids = selectedItemIds(), box = document.getElementById('itemsChosenList');
    if (box) box.innerHTML = chosenHtml(ids, 'items');
    var count = document.getElementById('itemsChosenCount'); if (count) count.textContent = ids.length;
    var all = ids.slice();
    _boeFilters.forEach(function (f, i) {
        var groupIds = parseItemIds(f.ids); all = all.concat(groupIds);
        var list = document.getElementById('boeChosen-' + i), count = document.getElementById('boeChosenCount-' + i);
        if (list) list.innerHTML = chosenHtml(groupIds, i);
        if (count) count.textContent = groupIds.length;
        var input = document.getElementById('boeIds-' + i);
        if (input && document.activeElement !== input) input.value = f.ids || '';
    });
    if (resolve !== false) resolveItemNames(Array.from(new Set(all)));
}
function togglePickerItem(target, id, checked) {
    var ids = selectionIds(target), present = ids.includes(id);
    if (checked && !present) {
        if (target === 'items' && _isWeb && _tier === 'none' && ids.length >= 5) { toast(t('free_max_items'), 'err'); renderPicker(); return; }
        ids.push(id);
    } else if (!checked) ids = ids.filter(function (x) { return x !== id; });
    if (target === 'items') document.getElementById('itemIds').value = ids.join(',');
    else { if (!_boeFilters[Number(target)]) return; _boeFilters[Number(target)].ids = ids.join(','); }
    renderItemSelection(); applyFilters(); autoSavePreset();
    if (_picker.target !== null) {
        document.getElementById('pickerSelectedCount').textContent = selectionIds(_picker.target).length;
        document.querySelectorAll('#pickerResults input[data-id]').forEach(function (input) { input.checked = selectionIds(_picker.target).includes(Number(input.dataset.id)); });
        if (_picker.selectedOnly) renderPicker();
    }
}
function openItemPicker(target) {
    if (target !== 'items' && !_boeFilters[Number(target)]) return;
    _picker.target = target; _picker.offset = 0; _picker.selectedOnly = false;
    document.getElementById('pickerTitle').textContent = target === 'items' ? 'Snipe items' : uiText('BoE · группа ', 'BoE · group ') + (Number(target) + 1);
    document.getElementById('pickerSearch').value = '';
    var dialog = document.getElementById('itemPicker');
    if (!dialog.open) dialog.showModal();
    renderPicker(); document.getElementById('pickerSearch').focus();
}
function closeItemPicker() {
    ++_picker.request; _picker.target = null; clearTimeout(_picker.timer);
    var dialog = document.getElementById('itemPicker'); if (dialog && dialog.open) dialog.close();
}
function searchPicker() { clearTimeout(_picker.timer); _picker.offset = 0; _picker.timer = setTimeout(renderPicker, 180); }
function pickerMode(selected) { _picker.selectedOnly = selected; _picker.offset = 0; renderPicker(); }
function pickerPage(direction) { _picker.offset = Math.max(0, _picker.offset + direction * 40); renderPicker(); }
async function renderPicker() {
    if (_picker.target === null) return;
    var request = ++_picker.request, target = _picker.target, ids = selectionIds(target);
    var query = document.getElementById('pickerSearch').value.trim();
    document.getElementById('pickerSelectedCount').textContent = ids.length;
    document.getElementById('pickerAll').setAttribute('aria-pressed', !_picker.selectedOnly);
    document.getElementById('pickerSelected').setAttribute('aria-pressed', _picker.selectedOnly);
    var box = document.getElementById('pickerResults'); box.setAttribute('aria-busy', 'true');
    var result;
    if (_picker.selectedOnly) {
        await resolveItemNames(ids);
        var selected = ids.map(function (id) { return _itemNames.get(id) || {id: id, name: '#' + id}; }).filter(function (it) {
            return !query || (it.name + ' ' + (it.name_ru || '') + ' ' + it.id).toLowerCase().includes(query.toLowerCase());
        });
        result = {items: selected.slice(_picker.offset, _picker.offset + 40), total: selected.length};
    } else result = await api('/browser/api/picker?limit=40&offset=' + _picker.offset + '&search=' + encodeURIComponent(query) + (target === 'items' ? '' : '&boe=true'));
    if (request !== _picker.request) return;
    box.removeAttribute('aria-busy');
    if (!result || !Array.isArray(result.items)) {
        box.innerHTML = '<div class="empty-selection">' + uiText('Каталог недоступен. Выбранные предметы сохранены; ID можно добавить вручную.', 'Catalog unavailable. Your selection is kept; you can still enter IDs manually.') + '</div>';
        return;
    }
    result.items.forEach(function (it) { _itemNames.set(it.id, it); });
    _picker.total = result.total;
    box.innerHTML = result.items.map(function (it) {
        return '<label class="picker-item"><input type="checkbox" data-id="' + it.id + '" ' + (ids.includes(it.id) ? 'checked' : '') +
            ' onchange="togglePickerItem(\'' + target + '\',' + it.id + ',this.checked)"><img loading="lazy" src="/browser/icon/' + it.id + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
            '<span><b>' + escapeHtml(it.name) + '</b><small>#' + it.id + (it.slot ? ' · ' + escapeHtml(it.slot) : '') + (_lang === 'ru' && it.name_ru ? ' · ' + escapeHtml(it.name_ru) : '') + '</small></span></label>';
    }).join('') || '<div class="empty-selection">' + uiText('Ничего не найдено. Можно вставить ID в фильтр вручную.', 'No matches. You can paste item IDs into the filter manually.') + '</div>';
    document.getElementById('pickerPageInfo').textContent = result.total ? (_picker.offset + 1) + '–' + Math.min(_picker.offset + 40, result.total) + ' / ' + result.total : '0';
    document.getElementById('pickerPrev').disabled = _picker.offset === 0;
    document.getElementById('pickerNext').disabled = _picker.offset + 40 >= result.total;
    renderItemSelection(false);
}
function renderBoeFilters() {
    var el = document.getElementById('boeFilters'); if (!el) return;
    var open = {}; el.querySelectorAll('.boe-filter').forEach(function (d) { open[d.dataset.index] = d.open; });
    el.innerHTML = _boeFilters.map(function (f, i) {
        function number(field, label, fallback, min, max) {
            return '<label>' + label + '<input type="number" min="' + min + '"' + (max == null ? '' : ' max="' + max + '"') + ' value="' + escapeHtml(f[field] == null ? fallback : f[field]) +
                '" oninput="updateBoeFilter(' + i + ',\'' + field + '\',this.value===\'\'?' + fallback + ':Number(this.value))"></label>';
        }
        function select(field, label, options) {
            return '<label>' + label + '<select onchange="updateBoeFilter(' + i + ',\'' + field + '\',this.value)">' + detailOptions(options, f[field]) + '</select></label>';
        }
        return '<details class="boe-filter" data-index="' + i + '"' + (open[i] !== false ? ' open' : '') + '><summary><span>BoE ' + String(i + 1).padStart(2, '0') + '</span><small>' + uiText('отдельный фильтр', 'independent filter') + '</small></summary><div class="boe-filter-body">' +
            '<button type="button" class="pick-button" onclick="openItemPicker(\'' + i + '\')">+ ' + uiText('Выбрать предметы', 'Choose items') + '</button>' +
            '<details class="chosen"><summary>' + uiText('Выбрано', 'Selected') + ' <b id="boeChosenCount-' + i + '">0</b></summary><div class="chosen-list" id="boeChosen-' + i + '"></div></details>' +
            '<div class="field-pair">' + number('ilvl_min', 'ilvl ' + uiText('от', 'from'), 0, 0, 999) + number('ilvl_max', 'ilvl ' + uiText('до', 'to'), 999, 0, 999) + '</div>' +
            '<div class="field-pair">' + number('discount', uiText('Скидка от, %', 'Discount from, %'), 0, 0, 100) + number('topx', uiText('Реалмов в топе', 'Top realms'), 4, 1, 100) + '</div>' +
            select('stats', uiText('Вторичные статы', 'Secondary stats'), Boe.statOptions) +
            '<div class="field-pair">' + select('socket', uiText('Сокет', 'Socket'), Boe.socketOptions) + select('effect', uiText('Доп. стат', 'Tertiary stat'), Boe.effectOptions) + '</div>' +
            '<details class="manual-ids"><summary>' + uiText('Вставить ID вручную', 'Paste IDs manually') + '</summary><input id="boeIds-' + i + '" aria-label="BoE ' + (i + 1) + ' IDs" value="' + escapeHtml(f.ids || '') + '" oninput="updateBoeFilter(' + i + ',\'ids\',this.value)"></details>' +
            '<button type="button" class="text-button danger" onclick="removeBoeFilter(' + i + ')">' + uiText('Удалить группу', 'Remove group') + '</button></div></details>';
    }).join('') || '<p class="muted empty-selection">' + uiText('Добавьте группу и выберите предметы. У каждой группы свои условия.', 'Add a group and choose its items. Each group has independent rules.') + '</p>';
    renderItemSelection();
}
function toggleFilters() { if (innerWidth < 760) _mobileFiltersOpen = !_mobileFiltersOpen; else _appearance.filters = !_appearance.filters; applyAppearance(); saveAppearance(); }
function toggleBrowserFilters() { if (innerWidth < 760) _mobileBrowserFiltersOpen = !_mobileBrowserFiltersOpen; else _appearance.browserFilters = !_appearance.browserFilters; applyAppearance(); saveAppearance(); }
function saveAppearance() { presetLocalSet('appearance', JSON.stringify(_appearance)); }
function updateAppearance() {
    _appearance.theme = document.getElementById('appearanceTheme').value;
    _appearance.density = document.getElementById('appearanceDensity').value;
    _appearance.pinItem = document.getElementById('appearancePinItem').checked;
    _appearance.pinSale = document.getElementById('appearancePinSale').checked;
    _appearance.pinAvg = Number(document.getElementById('setPinAvg').value) || 0;
    _appearance.pinDisc = Number(document.getElementById('setPinDisc').value) || 0;
    saveAppearance(); applyAppearance(); applyFilters();
}
function applyAppearance() {
    var theme = _appearance.theme === 'system' ? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark') : _appearance.theme;
    document.documentElement.dataset.theme = theme;
    var narrow = window.innerWidth < 760;
    var filters = narrow ? _mobileFiltersOpen : _appearance.filters;
    var browserFilters = narrow ? _mobileBrowserFiltersOpen : _appearance.browserFilters;
    document.getElementById('sniperMain').classList.toggle('filters-hidden', !filters);
    document.getElementById('filterToggle').setAttribute('aria-expanded', filters);
    document.getElementById('browserMain').classList.toggle('filters-hidden', !browserFilters);
    document.getElementById('browserFilterToggle').setAttribute('aria-expanded', browserFilters);
    [_grid, _bGrid].forEach(function (grid) {
        if (!grid) return;
        grid.setGridOption('rowHeight', _appearance.density === 'compact' ? 32 : 40);
        grid.resetRowHeights();
        var pinState = [{colId: 'item', pinned: !narrow && _appearance.pinItem ? 'left' : null}];
        if (grid === _grid) pinState.push({colId: 'sale_price', pinned: !narrow && _appearance.pinSale ? 'right' : null});
        grid.applyColumnState({state: pinState});
        if (_gridViewportMode.get(grid) !== narrow) {
            grid.applyColumnState({state: [{colId: 'item', width: narrow ? 165 : grid === _grid ? 270 : 290}, {colId: 'discount', width: narrow ? 60 : 76}]});
            if (grid === _grid) grid.applyColumnState({state: [{colId: 'price', width: narrow ? 110 : 115}]});
            _gridViewportMode.set(grid, narrow);
        }
    });
}
function initWorkspace() {
    try { _appearance = Object.assign(_appearance, JSON.parse(presetLocalGet('appearance') || '{}')); } catch (e) {}
    document.getElementById('appearanceTheme').value = _appearance.theme;
    document.getElementById('appearanceDensity').value = _appearance.density;
    document.getElementById('appearancePinItem').checked = _appearance.pinItem;
    document.getElementById('appearancePinSale').checked = _appearance.pinSale;
    if (_appearance.pinAvg != null) document.getElementById('setPinAvg').value = _appearance.pinAvg;
    if (_appearance.pinDisc != null) document.getElementById('setPinDisc').value = _appearance.pinDisc;
    if (_isWeb) {
        if (_tier === 'none') document.getElementById('boeCard').hidden = true;
    }
    document.body.dataset.mode = _isWeb ? 'web' : 'desktop';
    document.getElementById('presetIOSettings').style.display = 'block';
    document.getElementById('modeLabel').textContent = _isWeb ? 'WEB' : 'DESKTOP';
    applyAppearance(); renderItemSelection();
    window.addEventListener('resize', applyAppearance);
    matchMedia('(prefers-color-scheme: light)').addEventListener('change', applyAppearance);
    document.getElementById('itemPicker').addEventListener('cancel', function () { ++_picker.request; _picker.target = null; });
}
function showStartupError() {
    document.querySelectorAll('#sniperSidebar input,#sniperSidebar button,#sniperSidebar select').forEach(function (el) { el.disabled = true; });
    var message = uiText('Не удалось загрузить аккаунт и пресеты. Перезагрузите страницу, чтобы продолжить.', 'Could not load your account and presets. Reload the page to continue.');
    document.getElementById('presetSaveStatus').textContent = message;
    document.getElementById('presetSaveStatus').dataset.state = 'blocked';
    document.getElementById('gridContainer').innerHTML = '<div class="detail-message">' + message + '<br><button onclick="location.reload()">' + uiText('Перезагрузить', 'Reload') + '</button></div>';
}
function gridItemCell(p) {
    return '<span class="grid-item-name"><img loading="lazy" src="/browser/icon/' + p.data._id + '" alt="" onerror="this.style.visibility=\'hidden\'"><button class="item-open" type="button" title="' + escapeHtml(p.value) + '">' + escapeHtml(p.value) + '</button></span>';
}
function goldNumber(value) { return Number(String(value || '').replace(/[^\d.-]/g, '')) || 0; }
function formattedGold(value) {
    if (value == null || value === '' || value === '—') return '—';
    var parts = Number(value).toFixed(2).split('.');
    return parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, '\u202f') + '.' + parts[1];
}
function copyItemName(name) {
    var text = String(name).replace(/\s*\[\d+\]$/, '');
    function fallback() { var input = document.createElement('textarea'); input.value = text; document.body.append(input); input.select(); var ok = document.execCommand('copy'); input.remove(); toast(ok ? uiText('Название скопировано', 'Name copied') : t('copy_fail'), ok ? 'ok' : 'err'); }
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { toast(uiText('Название скопировано', 'Name copied'), 'ok'); }, fallback); else fallback();
}
