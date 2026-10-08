/* Account-scoped preset cache and durable outbox. One atomic storage record. */
(function (root) {
'use strict';
function create(storage, key, send, status, changed) {
    status = status || function () {};
    changed = changed || function () {};
    var state = {version: 2, list: [], pending: {}, aliases: {}}, busy = null;
    var generation = 0, readRequest = 0, sequence = 0, storageDirty = false, readFailed = false;
    var instance = Date.now().toString(36) + Math.random().toString(36).slice(2);
    function clone(value) { return JSON.parse(JSON.stringify(value)); }
    function read() {
        if (storageDirty) return;
        try {
            var saved = JSON.parse(storage.getItem(key) || 'null');
            if (saved && saved.version === 2 && Array.isArray(saved.list) && saved.pending) {
                state = saved;
                state.aliases = state.aliases || {};
            } else if (!saved || (typeof saved === 'object' && !Array.isArray(saved))) {
                state.pending = saved || {};
                var list = JSON.parse(storage.getItem(key.replace(/:pending$/, ':list')) || '[]');
                if (Array.isArray(list)) state.list = list;
            }
        } catch (e) { /* Keep the in-memory draft when storage is unavailable. */ }
    }
    read();
    function persist() {
        try { storage.setItem(key, JSON.stringify(state)); storageDirty = false; return true; }
        catch (e) { storageDirty = true; status('storage'); return false; }
    }
    function resolve(id) { return state.aliases[String(id)] == null ? id : state.aliases[String(id)]; }
    function pendingFor(id) { return state.pending[String(resolve(id))]; }
    function overlay(p) {
        var draft = pendingFor(p.id);
        return draft ? Object.assign({}, p, clone(draft.data)) : clone(p);
    }
    function list() {
        var result = state.list.map(overlay);
        Object.keys(state.pending).forEach(function (id) {
            var entry = state.pending[id];
            if (!result.some(function (p) { return String(p.id) === id; })) result.push(clone(entry.data));
        });
        return result;
    }
    function put(data) {
        read();
        var next = clone(data), id = resolve(next.id);
        next.id = id;
        var old = pendingFor(id);
        var base = state.list.find(function (p) { return String(p.id) === String(id); }) || {};
        next = Object.assign({}, base, old ? old.data : {}, next);
        state.pending[String(id)] = {data: next, rev: instance + ':' + (++sequence)};
        ++generation;
        if (persist()) status('pending');
        changed(list());
        return next;
    }
    function discard(id) {
        read(); id = resolve(id);
        delete state.pending[String(id)];
        state.list = state.list.filter(function (p) { return String(p.id) !== String(id); });
        ++generation; persist(); changed(list());
    }
    async function refresh(fetcher) {
        read();
        var start = generation, request = ++readRequest, storedAtStart;
        try { storedAtStart = storage.getItem(key); } catch (e) {}
        var remote;
        try { remote = await fetcher(); } catch (e) {}
        var storageChanged = false;
        try { storageChanged = storage.getItem(key) !== storedAtStart; } catch (e) {}
        if (request !== readRequest) return list();
        if (Array.isArray(remote) && start === generation && !storageChanged) {
            readFailed = false; state.list = clone(remote); persist();
        } else if (!Array.isArray(remote)) { readFailed = true; status('offline'); }
        if (storageChanged) read();
        return list();
    }
    function hasPending(id) { return id == null ? Object.keys(state.pending).length > 0 : !!pendingFor(id); }
    function flush(keepalive) {
        if (busy) return busy;
        async function run() {
            read();
            var blocked = null;
            // Snapshot IDs; edits made during a request remain queued.
            for (var id of Object.keys(state.pending)) {
                var entry = state.pending[id];
                if (!entry) continue;
                if (entry.error) { blocked = entry.error; continue; }
                var result, payload = clone(entry.data);
                if (String(payload.id).startsWith('draft:')) delete payload.id;
                try { result = await send(payload, !!keepalive); } catch (e) { result = null; }
                if (!result || !result.ok) {
                    if (result && ['free_limit', 'preset_not_found', 'invalid_boe_filters', 'invalid_preset', 'invalid_client_id'].includes(result.error)) {
                        read();
                        if (state.pending[id] && state.pending[id].rev === entry.rev) {
                            state.pending[id].error = result; persist(); blocked = result;
                        }
                        continue;
                    }
                    status(result && result.error === 'unauthorized' ? 'auth' : result && (result.error === 'free_limit' || result.error === 'preset_not_found') ? 'blocked' : 'offline', result);
                    return false;
                }
                read();
                readFailed = false;
                var savedId = result.id == null ? entry.data.id : result.id;
                var confirmed = Object.assign({}, entry.data, {id: savedId});
                var index = state.list.findIndex(function (p) { return String(p.id) === String(savedId); });
                if (index < 0) state.list.push(confirmed); else state.list[index] = confirmed;
                var current = state.pending[id];
                if (current && current.rev === entry.rev) delete state.pending[id];
                if (String(savedId) !== id) {
                    state.aliases[id] = savedId;
                    if (current && current.rev !== entry.rev) {
                        state.pending[String(savedId)] = {data: Object.assign({}, current.data, {id: savedId}), rev: current.rev};
                        delete state.pending[id];
                    }
                    state.list = state.list.filter(function (p) { return String(p.id) !== id; });
                }
                ++generation;
                if (!persist()) { state.pending[String(savedId)] = current || {data: confirmed, rev: entry.rev}; return false; }
                changed(list(), {from: entry.data.id, to: savedId});
            }
            status(blocked ? 'blocked' : hasPending() ? 'pending' : readFailed ? 'offline' : 'saved', blocked);
            return !blocked;
        }
        var locks = root.navigator && root.navigator.locks;
        busy = (locks ? locks.request(key, run) : run()).finally(function () { busy = null; });
        return busy;
    }
    return {put: put, overlay: overlay, list: list, refresh: refresh, flush: flush,
        discard: discard, hasPending: hasPending, resolve: resolve,
        sync: function () { read(); ++generation; changed(list()); }};
}
root.PresetStore = {create: create};
if (typeof module !== 'undefined') module.exports = root.PresetStore;
})(typeof window !== 'undefined' ? window : globalThis);
