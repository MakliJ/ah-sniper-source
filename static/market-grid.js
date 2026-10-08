/* Full analytical tables: no columns or numerical precision removed. */
var _gridRequest = 0;
function marketTheme() {
    return agGrid.themeQuartz.withParams({backgroundColor: 'var(--bg)', foregroundColor: 'var(--text)',
        borderColor: 'var(--border)', headerBackgroundColor: 'var(--bg2)', headerTextColor: 'var(--text2)',
        oddRowBackgroundColor: 'var(--row-alt)', rowHoverColor: 'var(--hover)', accentColor: 'var(--gold)',
        fontFamily: 'Segoe UI Variable Text, Segoe UI, system-ui, sans-serif', fontSize: 12,
        headerFontSize: 10, headerHeight: 36, rowHeight: 32, spacing: 5, wrapperBorderRadius: 0});
}
function priceColumn(field, label, width) {
    return {field: field, headerName: label, width: width || 115, minWidth: 95, cellClass: 'numeric',
        comparator: function (a, b) { return goldNumber(a) - goldNumber(b); },
        cellRenderer: function (p) { return p.value && p.value !== '—' ? formattedGold(goldNumber(p.value)) : '—'; }};
}
async function loadGrid(qs) {
    var request = ++_gridRequest, data;
    if (_isWeb && _webItems && !selectedRealmIds().length) {
        var filtered = webFilterItems(); data = {items: filtered, total: filtered.length, collected_at: _webUpdatedAt};
    } else data = await api('/api/items' + qs);
    if (request !== _gridRequest) return;
    if (!data || !Array.isArray(data.items)) {
        document.getElementById('itemCount').textContent = '—';
        if (_grid) document.getElementById('gridStatusMessage').textContent = uiText('Не удалось обновить · показаны предыдущие данные', 'Refresh failed · showing previous data');
        return;
    }
    var rows = data.items.map(function (it, i) {
        return {discount: it.discount_raw, item: it.name, price: it.price, price_raw: it.price_raw,
            top10avg: it.top10avg, top10avg_raw: it.top10avg_raw, realm: it.realm, quantity: it.quantity,
            top3: it.top3, sale_realm: it.sale_realm || '—', sale_price: it.sale_price || '—',
            stats: it.stats_label || '', sockets: it.sockets, effects: it.effects, _detail: Boe.detailSelection(it),
            url: it.url || '#', _id: it.item_id, _ilvl: it.ilvl || 0, ilvl: it.ilvl || 0, _idx: i,
            _is_first: !!it._is_first, _pin: false, _is121: !!it.is_121};
    });
    _data = data.items;
    document.getElementById('itemCount').textContent = data.total == null ? rows.length : data.total;
    document.getElementById('gridStatusMessage').textContent = '';
    if (data.collected_at) document.getElementById('updateTime').textContent = data.collected_at;
    var pinAvg = Number(document.getElementById('setPinAvg').value) || 0;
    var pinDisc = Number(document.getElementById('setPinDisc').value) || 0;
    if (pinAvg || pinDisc) rows.forEach(function (r) { r._pin = r.top10avg_raw > pinAvg * 10000 && r.discount > pinDisc; });
    rows.sort(function (a, b) { return Number(b._pin) - Number(a._pin) || Number(b._is_first) - Number(a._is_first) || b.discount - a.discount; });
    if (!_grid) {
        var price = priceColumn('price', t('grid_price') + ', g');
        price.cellRenderer = function (p) { return p.value && p.value !== '—' ? '<button type="button" class="sniper-price" title="' + uiText('Открыть аналитику предмета', 'Open item analytics') + '">' + formattedGold(goldNumber(p.value)) + '</button>' : '—'; };
        var columns = [{field: 'discount', headerName: '%', width: 76, cellRenderer: function (p) { return discHtml(p.value); }, sort: 'desc'},
            {field: 'item', headerName: t('grid_item'), width: 270, minWidth: 150, cellRenderer: gridItemCell}, price,
            {field: 'stats', headerName: 'Stats · socket · extra', width: 300, minWidth: 210,
                valueGetter: function (p) { return Boe.label({stats_label: p.data.stats, sockets: p.data.sockets, effects: p.data.effects}, p.data._ilvl > 0); }},
            {field: 'ilvl', headerName: 'ilvl', width: 65, cellClass: 'numeric'}, priceColumn('top10avg', 'Avg, g', 110),
            {field: 'realm', headerName: 'Realm', width: 145}, {field: 'quantity', headerName: t('grid_qty'), width: 75, cellClass: 'numeric'},
            {field: 'top3', headerName: t('grid_top3'), width: 275}, {field: 'sale_realm', headerName: 'Sale R.', width: 145},
            priceColumn('sale_price', t('grid_sale_price') + ', g', 120)];
        _grid = agGrid.createGrid(document.getElementById('gridContainer'), {theme: marketTheme(), rowData: rows, columnDefs: columns,
            defaultColDef: {resizable: true, sortable: true, tooltipValueGetter: function (p) { return p.value == null ? '' : String(p.value); }},
            rowSelection: {mode: 'multiRow', enableClickSelection: false, headerCheckbox: true},
            selectionColumnDef: {width: 34, minWidth: 34, maxWidth: 34, resizable: false},
            onSelectionChanged: onSelChange,
            overlayNoRowsTemplate: '<div class="ag-overlay-no-rows-center">' + uiText('Нет предложений по этим условиям.<br>Выберите предметы или измените фильтры.', 'No offers match these conditions.<br>Choose items or adjust the filters.') + '</div>',
            rowClassRules: {'row-pinned': function (p) { return p.data._pin; }, 'row-rare-gold': function (p) { return p.data._is_first; }, 'row-121': function (p) { return p.data._is121; }},
            postSortRows: function (p) { p.nodes.sort(function (a, b) { return Number(b.data._pin) - Number(a.data._pin) || Number(b.data._is_first) - Number(a.data._is_first); }); },
            onCellClicked: function (e) { if (e.colDef.field === 'price' || e.colDef.field === 'item') openSniperItem(e.data); },
            onCellKeyDown: function (e) { if (e.event.key === 'Enter' && e.data) openSniperItem(e.data); },
            getRowId: function (p) { return String(p.data._id) + '_' + p.data._ilvl; }});
        applyAppearance();
    } else _grid.setGridOption('rowData', rows);
    onSelChange();
}
function _gridOrCreate(rows) {
    if (!_bGrid) {
        var columns = [{field: 'star', headerName: '☆', width: 42, sortable: false,
            cellRenderer: function (p) { return '<button class="text-button" style="font-size:17px;color:var(--gold)" aria-label="' + t('add_snipe') + '" onclick="event.stopPropagation();browserToggleStar(' + p.data._id + ',this)">' + (p.data._snipe ? '★' : '☆') + '</button>'; }},
            {field: 'discount', headerName: '%', width: 76, cellRenderer: function (p) { return discHtml(p.value); }, sort: 'desc'},
            {field: 'item', headerName: t('grid_item'), width: 290, minWidth: 150, cellRenderer: gridItemCell},
            {field: 'ilvl', headerName: 'ilvl', width: 65, cellClass: 'numeric'},
            {field: 'min', headerName: uiText('Мин. цена, g', 'Min. price, g'), width: 125, cellClass: 'numeric', cellRenderer: function (p) { return fmtGold(p.value); }},
            {field: 'stats', headerName: 'Stats · socket · extra', width: 300, minWidth: 210,
                valueGetter: function (p) { return Boe.label({stats_label: p.data.stats, sockets: p.data.sockets, effects: p.data.effects}, p.data._ilvl > 0); }},
            {field: 'avg', headerName: 'Avg, g', width: 125, cellClass: 'numeric', cellRenderer: function (p) { return fmtGold(p.value); }},
            {field: 'realms', headerName: uiText('Реалмов', 'Realms'), width: 85, cellClass: 'numeric'},
            {field: 'quality', headerName: t('quality'), width: 105}];
        _bGrid = agGrid.createGrid(document.getElementById('browserGrid'), {theme: marketTheme(), rowData: rows, columnDefs: columns,
            defaultColDef: {resizable: true, sortable: true, tooltipValueGetter: function (p) { return p.value == null ? '' : String(p.value); }},
            getRowId: function (p) { return String(p.data._id) + '_' + p.data._ilvl; },
            onCellClicked: function (e) {
                if (e.colDef.field === 'star') return;
                var enabled = document.getElementById('browserBoeOnly').checked;
                openItemDetail(e.data._id, e.data._ilvl, enabled ? document.getElementById('browserBoeStats').value : '',
                    enabled ? document.getElementById('browserBoeSocket').value : '', enabled ? document.getElementById('browserBoeEffect').value : '');
            },
            overlayNoRowsTemplate: '<div class="ag-overlay-no-rows-center">' + uiText('По этим условиям ничего не найдено.', 'No items match these filters.') + '</div>'});
        applyAppearance();
    } else _bGrid.setGridOption('rowData', rows);
}
