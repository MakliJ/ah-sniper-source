/* One analytical card for the sniper table and the catalog. */
var _detailRequest = 0, _detailState = null;
function detailOptions(options, value) {
    var available = options.slice();
    if (value && !available.some(function (o) { return o[0] === value; })) available.push([value, value]);
    var en = {'Все статы':'All stats','Сокет: любой':'Any socket','С сокетом':'With socket','Без сокета':'No socket','Любой':'Any','Без доп. стата':'No tertiary stat'};
    return available.map(function (o) { return '<option value="' + escapeHtml(o[0]) + '"' + (o[0] === (value || '') ? ' selected' : '') + '>' + escapeHtml(_lang === 'en' ? en[o[1]] || o[1] : o[1]) + '</option>'; }).join('');
}
function openSniperItem(row) {
    var selection = row._detail; if (!selection) return;
    openItemDetail(selection.itemId, selection.ilvl, selection.stats, selection.socket, selection.effect, selection);
}
async function openItemDetail(itemId, ilvl, stats, socket, effect, origin) {
    var request = ++_detailRequest, hostId = origin ? 'sniperDetailHost' : 'browserDetailHost';
    if (!_detailState || _detailState.itemId !== itemId || _detailState.ilvl !== ilvl || _detailState.hostId !== hostId) {
        closeDetailHosts();
        _detailState = {itemId: itemId, ilvl: ilvl || 0, hostId: hostId, query: '', sort: 'price', saleOnly: false, allVariants: false, limit: 100};
    }
    Object.assign(_detailState, {stats: stats || '', socket: socket || '', effect: effect || '', origin: origin || null});
    var host = document.getElementById(hostId); host.hidden = false;
    host.innerHTML = '<div class="item-detail" aria-busy="true"><div class="detail-head"><span class="workspace-title">' + uiText('Аналитика предмета', 'Item analytics') + ' #' + itemId + '</span><button class="icon-button" style="margin-left:auto" onclick="closeItemDetail()" aria-label="' + uiText('Закрыть карточку', 'Close item details') + '">×</button></div><div class="detail-message">' + t('loading_data') + '</div></div>';
    var url = '/browser/api/item?id=' + itemId + '&ilvl=' + (ilvl || 0) + '&boe_stats=' + encodeURIComponent(stats || '') + '&boe_socket=' + encodeURIComponent(socket || '') + '&boe_effect=' + encodeURIComponent(effect || '');
    var d = await api(url);
    if (request !== _detailRequest || !_detailState) return;
    if (!d || !Array.isArray(d.realm_data)) {
        host.querySelector('.detail-message').innerHTML = uiText('Не удалось получить аналитику. Фильтры и таблица сохранены.', 'Could not load analytics. Your filters and table are kept.') + '<br><button onclick="retryItemDetail()">' + uiText('Повторить', 'Retry') + '</button>';
        host.querySelector('.item-detail').removeAttribute('aria-busy'); return;
    }
    _detailState.data = d;
    renderItemDetail();
}
function retryItemDetail() {
    if (!_detailState) return;
    var s = _detailState; openItemDetail(s.itemId, s.ilvl, s.stats, s.socket, s.effect, s.origin);
}
function closeDetailHosts() {
    ['sniperDetailHost', 'browserDetailHost'].forEach(function (id) { var host = document.getElementById(id); host.hidden = true; host.replaceChildren(); });
}
function closeItemDetail() { ++_detailRequest; _detailState = null; closeDetailHosts(); }
function expandItemDetail() {
    if (!_detailState) return;
    var host = document.getElementById(_detailState.hostId); host.classList.toggle('expanded');
    var button = document.getElementById('detailExpand'); button.setAttribute('aria-pressed', host.classList.contains('expanded'));
    button.title = host.classList.contains('expanded') ? uiText('Уменьшить карточку', 'Shrink card') : uiText('Развернуть карточку', 'Expand card');
}
function reloadDetail() {
    if (!_detailState) return;
    var s = _detailState;
    openItemDetail(s.itemId, s.ilvl, document.getElementById('detailBoeStats').value,
        document.getElementById('detailBoeSocket').value, document.getElementById('detailBoeEffect').value, s.origin);
}
function detailRealmUrl(row) {
    var region = _detailState.data.region || 'eu';
    return 'https://undermine.exchange/#' + (row.slug ? region + '-' + encodeURIComponent(row.slug) : region + '/' + row.realm_id) + '/' + _detailState.itemId;
}
function detailEffectText(row) {
    if (row.effects == null) return uiText('Неизвестно', 'Unknown');
    return row.effects.length ? row.effects.map(function (effect) { var option = Boe.effectOptions.find(function (o) { return o[0] === effect; }); return option ? option[1] : effect; }).join(', ') : '—';
}
function renderItemDetail() {
    var s = _detailState, d = s.data;
    var color = {EPIC: 'var(--blue)', RARE: 'var(--blue)', LEGENDARY: 'var(--gold)', UNCOMMON: 'var(--green)'}[d.quality] || 'var(--text)';
    var rows = d.realm_data, saleIds = selectedRealmIds();
    var saleRows = saleIds.length ? rows.filter(function (r) { return saleIds.includes(r.realm_id); }) : rows;
    var highest = saleRows.reduce(function (best, r) { return !best || r.min_buyout > best.min_buyout ? r : best; }, null);
    var qty = rows.reduce(function (n, r) { return n + (r.quantity || 0); }, 0);
    function metric(label, value, title) { return '<div class="detail-metric" title="' + escapeHtml(title || label) + '"><small>' + label + '</small><b>' + value + '</b></div>'; }
    var h = '<section id="itemDetail" class="item-detail" aria-label="' + uiText('Аналитика предмета', 'Item analytics') + '"><div class="detail-head"><img class="detail-icon" src="/browser/icon/' + d.item_id + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
        '<div class="detail-heading"><h2 style="color:' + color + '">' + escapeHtml(d.name) + '</h2><small>#' + d.item_id + ' · ' + escapeHtml([d.quality, d.class_name, d.subclass_name, d.slot_name].filter(Boolean).join(' · ')) + '</small></div>' +
        '<div class="detail-actions"><button id="detailStarBtn" onclick="detailToggleStar(' + d.item_id + ')" title="' + t('add_snipe') + '">' + (selectedItemIds().includes(d.item_id) ? '★' : '☆') + '<span class="detail-action-text"> ' + uiText('В снайп-лист', 'Snipe list') + '</span></button>' +
        '<button class="detail-copy" id="detailCopy" title="' + uiText('Скопировать название', 'Copy item name') + '">⧉</button>' +
        '<a href="' + escapeHtml(d.url) + '" target="_blank" rel="noopener noreferrer" title="Undermine Exchange">↗</a>' +
        '<button id="detailExpand" onclick="expandItemDetail()" title="' + uiText('Развернуть карточку', 'Expand card') + '" aria-pressed="' + document.getElementById(s.hostId).classList.contains('expanded') + '">↕</button>' +
        '<button onclick="closeItemDetail()" aria-label="' + uiText('Закрыть карточку', 'Close item details') + '">×</button></div></div>' +
        '<div class="detail-body"><aside class="detail-summary"><div class="detail-metrics">' +
        metric(uiText('Минимум, g', 'Minimum, g'), fmtGold(d.min_price)) + metric('Avg, g', fmtGold(d.avg_price), uiText('Историческая средняя для предмета и ilvl; не только выбранных статов.', 'Historical average for the item and ilvl, across stat combinations.')) +
        metric(uiText('Скидка к Avg', 'Discount vs Avg'), d.min_price && d.avg_price ? d.discount + '%' : '—') + metric(uiText('Реалмов', 'Realms'), d.realm_count) +
        metric(d.boe ? uiText('Кол-во в мин. лотах', 'Qty at realm minima') : uiText('Количество', 'Quantity'), qty) + metric(uiText('Макс. цена продажи, g', 'Highest sale price, g'), highest ? fmtGold(highest.min_buyout) : '—', highest ? highest.realm_name : '') + '</div>';
    if (highest) h += '<p class="detail-note">' + uiText('Максимум ', 'Maximum ') + escapeHtml(highest.realm_name) + (saleIds.length ? uiText(' · среди выбранных реалмов продажи', ' · selected sale realms') : uiText(' · среди всех реалмов', ' · all realms')) + '</p>';
    if (s.origin) h += '<div class="detail-origin">' + uiText('Выбрано в таблице', 'Selected in table') + '<br><b>' + escapeHtml(s.origin.realm || '') + ' · ' + fmtGold(s.origin.price) + ' g</b></div>';
    h += '<p class="detail-note">' + uiText('Avg — историческая средняя предмета / ilvl. Сравнение цен не учитывает комиссию и скорость продажи.', 'Avg is the historical item / ilvl average. Price comparisons exclude fees and sale speed.') + '</p></aside><div class="detail-realm-area">';
    if (d.boe) h += '<div class="detail-controls"><label>' + uiText('Статы', 'Stats') + '<select id="detailBoeStats" onchange="reloadDetail()">' + detailOptions(Boe.statOptions, s.stats) + '</select></label><label>' + uiText('Сокет', 'Socket') + '<select id="detailBoeSocket" onchange="reloadDetail()">' + detailOptions(Boe.socketOptions, s.socket) + '</select></label><label>' + uiText('Доп. стат', 'Tertiary stat') + '<select id="detailBoeEffect" onchange="reloadDetail()">' + detailOptions(Boe.effectOptions, s.effect) + '</select></label></div>';
    h += '<div class="detail-realm-tools"><input type="search" id="detailRealmSearch" placeholder="' + uiText('Найти реалм…', 'Find realm…') + '" value="' + escapeHtml(s.query) + '" aria-label="' + uiText('Поиск по реалмам', 'Search realms') + '" oninput="detailTableChanged()">' +
        '<select id="detailRealmSort" aria-label="' + uiText('Порядок реалмов', 'Realm order') + '" onchange="detailTableChanged()">' + detailOptions([['price', uiText('Цена ↑', 'Price ↑')], ['price_desc', uiText('Цена ↓', 'Price ↓')], ['name', uiText('Реалм A–Z', 'Realm A–Z')], ['quantity', uiText('Количество ↓', 'Quantity ↓')]], s.sort) + '</select>' +
        '<label><input id="detailSaleOnly" type="checkbox"' + (s.saleOnly ? ' checked' : '') + ' onchange="detailTableChanged()">' + uiText('Мои реалмы', 'My realms') + '</label>' +
        (d.boe ? '<label><input id="detailAllVariants" type="checkbox"' + (s.allVariants ? ' checked' : '') + ' onchange="detailTableChanged()">' + uiText('Все варианты лотов', 'All lot variants') + '</label>' : '') + '<span id="detailRealmCount"></span></div><div class="detail-table-scroll" id="detailRealmTable"></div></div></div></section>';
    document.getElementById(s.hostId).innerHTML = h;
    document.getElementById('detailCopy').onclick = function () { copyItemName(d.name); };
    renderDetailRealms();
}
function detailTableChanged() {
    var s = _detailState; if (!s) return;
    s.query = document.getElementById('detailRealmSearch').value;
    s.sort = document.getElementById('detailRealmSort').value;
    s.saleOnly = document.getElementById('detailSaleOnly').checked;
    s.allVariants = !!document.getElementById('detailAllVariants') && document.getElementById('detailAllVariants').checked;
    s.limit = 100; renderDetailRealms();
}
function moreDetailRealms() { _detailState.limit += 100; renderDetailRealms(); }
function renderDetailRealms() {
    var s = _detailState, d = s.data, saleIds = selectedRealmIds(), query = s.query.trim().toLowerCase();
    var source = s.allVariants ? (d.variants || []).filter(function (r) { return Boe.matches(r, s.stats, s.socket, s.effect); }) : d.realm_data;
    var rows = source.filter(function (r) {
        return (!s.saleOnly || saleIds.includes(r.realm_id)) && (!query || ((r.realm_name || '') + ' ' + r.realm_id).toLowerCase().includes(query));
    }).slice();
    function price(r) { return r.min_buyout == null ? r.price : r.min_buyout; }
    rows.sort(function (a, b) { return s.sort === 'name' ? (a.realm_name || '').localeCompare(b.realm_name || '') : s.sort === 'quantity' ? b.quantity - a.quantity : s.sort === 'price_desc' ? price(b) - price(a) : price(a) - price(b); });
    document.getElementById('detailRealmCount').textContent = rows.length + ' / ' + source.length;
    var widths = d.boe ? ['25%', '16%', '7%', '10%', '23%', '8%', '11%'] : ['43%', '25%', '13%', '19%'];
    var h = '<table class="realm-analytics' + (d.boe ? ' boe-realms' : '') + '"><colgroup>' + widths.map(function (width) { return '<col style="width:' + width + '">'; }).join('') + '</colgroup><thead><tr><th>' + uiText('Реалм', 'Realm') + '</th><th class="num">' + uiText('Цена, g', 'Price, g') + '</th><th class="num">' + uiText('Кол-во', 'Qty') + '</th><th class="num">vs Avg</th>' +
        (d.boe ? '<th>' + uiText('Статы', 'Stats') + '</th><th>' + uiText('Сокет', 'Socket') + '</th><th>' + uiText('Доп. стат', 'Tertiary stat') + '</th>' : '') + '</tr></thead><tbody>';
    h += rows.slice(0, s.limit).map(function (r) {
        var discount = d.avg_price ? Math.round((1 - price(r) / d.avg_price) * 1000) / 10 : null;
        var selected = s.origin && r.realm_id === s.origin.realmId && price(r) === s.origin.price;
        return '<tr' + (selected ? ' class="origin-realm"' : '') + '><td title="' + escapeHtml(r.realm_name) + '"><a href="' + detailRealmUrl(r) + '" target="_blank" rel="noopener noreferrer">' + (saleIds.includes(r.realm_id) ? '<span class="sale-marker">★</span>' : '') + escapeHtml(r.realm_name || 'Realm ' + r.realm_id) + '</a></td><td class="num price">' + fmtGold(price(r)) + '</td><td class="num">' + (r.quantity || 0) + '</td><td class="num">' + (discount == null ? '—' : (discount > 0 ? '−' : '+') + Math.abs(discount) + '%') + '</td>' +
            (d.boe ? '<td title="' + escapeHtml(r.stats_label || uiText('Неизвестно', 'Unknown')) + '">' + escapeHtml(r.stats_label || uiText('Неизвестно', 'Unknown')) + '</td><td title="' + (r.sockets == null ? uiText('Неизвестно', 'Unknown') : r.sockets) + '">' + (r.sockets == null ? '?' : r.sockets > 0 ? '◇ ' + r.sockets : '—') + '</td><td title="' + escapeHtml(detailEffectText(r)) + '">' + escapeHtml(detailEffectText(r)) + '</td>' : '') + '</tr>';
    }).join('');
    h += '</tbody></table>';
    if (!rows.length) h += '<div class="detail-message">' + (s.saleOnly && !saleIds.length ? uiText('Выберите реалмы продажи на вкладке «Реалмы».', 'Choose sale realms on the Realms tab.') : uiText('Нет лотов с выбранными условиями.', 'No lots match these filters.')) + '</div>';
    if (rows.length > s.limit) h += '<button class="text-button" onclick="moreDetailRealms()">' + uiText('Показать ещё', 'Show more') + ' · ' + (rows.length - s.limit) + '</button>';
    document.getElementById('detailRealmTable').innerHTML = h;
}
