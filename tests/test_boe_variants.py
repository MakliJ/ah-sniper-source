"""BoE lot identity and local/web parity. No network or production DB writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'desktop'), str(ROOT)]
import ahgem
import main

IID = 271445
G = 10000


def lot(price, stats=(36, 49), socket=False, effect=6652, iid=IID, ilbonus=12850):
    return {'buyout': price * G, 'quantity': 1, 'item': {
        'id': iid, 'bonus_lists': [effect, 13695 if socket else 13696, 13662, 13335, ilbonus, 10844],
        'modifiers': [{'type': 29, 'value': stats[0]}, {'type': 30, 'value': stats[1]}]}}


class BoETest(unittest.TestCase):
    def setUp(self):
        main._cache.clear(); main._boe_variant_cache.clear(); main._realm_keys.clear()
        main._avg.clear(); main._prev_snapshot_items.clear(); main._prev_snapshot_items.add(IID)
        main._st['state'] = 'idle'
        main._rnames.update({1: 'Realm A', 2: 'Realm B', 3: 'Legacy'})
        main._inames[IID] = 'Test Belt'
        main._update_realm_in_cache(1, ahgem.process_auctions({'auctions': [
            lot(100, (32, 40)), lot(200), lot(300, socket=True), lot(400, socket=True)]}))
        main._update_realm_in_cache(2, ahgem.process_auctions({'auctions': [
            lot(150, (32, 40), True), lot(250, socket=True), lot(450)]}))
        main._avg[(IID, 321)] = 500 * G
        main.app.config['TESTING'] = True
        self.c = main.app.test_client()

    def get(self, path, **params):
        response = self.c.get(path, query_string=params)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def test_decoder_uses_modifiers_not_item_ilvl_or_price(self):
        a = ahgem.process_auctions({'auctions': [lot(200), lot(200, (32, 40)), lot(200, socket=True)]})
        self.assertEqual(len(a[(IID, 321)]['variants']), 3)
        self.assertEqual(ahgem.decode_boe_variant(IID, [41, 13695], [{'type': 29, 'value': 49}, {'type': 30, 'value': 36}])['stats_label'], 'Haste + Mastery + Leech')
        self.assertIsNone(ahgem.decode_boe_variant(1, [99999], [])['sockets'])
        self.assertEqual(ahgem.compute_ilvl([12850, 13335]), 321)
        self.assertEqual(ahgem.compute_ilvl([13335, 12850]), 321)
        self.assertEqual(ahgem.decode_boe_variant(271638, [13668], [])['sockets'], 1)

    def test_browser_and_detail_select_same_lot(self):
        filters = dict(boe_stats='haste+mastery', boe_socket='yes')
        row = self.get('/browser/api/items', boe='true', **filters)['items'][0]
        self.assertEqual(row['min_price'], 250 * G)
        self.assertEqual(row['realm_count'], 2)
        detail = self.get('/browser/api/item', id=IID, ilvl=321, **filters)
        self.assertEqual(detail['min_price'], row['min_price'])
        self.assertEqual([r['min_buyout'] for r in detail['realm_data']], [250*G, 300*G])
        self.assertEqual([r['realm_id'] for r in detail['realm_data']], [2, 1])
        self.assertTrue(all(r['sockets'] == 1 and r['stats'] == ['haste', 'mastery'] for r in detail['realm_data']))
        self.assertEqual(self.get('/browser/api/item', id=IID, ilvl=321)['realm_count'], 2)

    def test_disabled_browser_filters_and_non_boe(self):
        row = self.get('/browser/api/items', boe_stats='haste+mastery', boe_socket='yes')['items'][0]
        self.assertEqual(row['min_price'], 100*G)
        main._update_realm_in_cache(3, {(999, 0): {'min_buyout': 10, 'count': 1}})
        row = next(r for r in self.get('/api/items', item_ids='999')['items'] if r['item_id'] == 999)
        self.assertFalse(row['boe']); self.assertEqual(row['stats_label'], '')

    def test_independent_items_and_boe_switches_and_empty_ids(self):
        main._update_realm_in_cache(3, {(999, 0): {'min_buyout': 10*G, 'count': 1}})
        rule = json.dumps([{'ids': str(IID), 'stats': 'haste+mastery', 'socket': 'yes'}])
        def ids(**params):
            return {r['item_id'] for r in self.get('/api/items', **params)['items']}
        self.assertEqual(ids(item_ids='999'), {999})  # Old presets default to enabled.
        self.assertEqual(ids(item_ids='999', items_enabled='false'), set())
        self.assertEqual(ids(item_ids='999', boe_filters=rule), {999, IID})
        self.assertEqual(ids(item_ids='999', items_enabled='false', boe_filters=rule), {IID})
        for empty in ('', ' , ', 'invalid'):
            self.assertEqual(ids(item_ids=empty), set())
            self.assertEqual(ids(item_ids=empty, min_discount=1, min_avg=1, search='Test'), set())
            self.assertEqual(ids(item_ids=empty, min_discount=100, min_avg=999999,
                                 search='unrelated', boe_filters=rule), {IID})
        # Browser catalog remains unfiltered by the sniper's ID requirement.
        self.assertIn(999, {r['item_id'] for r in self.get('/browser/api/items')['items']})

    def test_unknown_realms_never_inherit_catalog_or_pass_no_socket(self):
        main._update_realm_in_cache(3, {(IID, 321): {'min_buyout': 50*G, 'count': 1}})
        row = self.get('/browser/api/items', boe='true')['items'][0]
        self.assertEqual(row['min_price'], 50*G); self.assertEqual(row['stats'], [])
        self.assertIsNone(row['sockets'])
        detail = self.get('/browser/api/item', id=IID, ilvl=321, boe_socket='no')
        self.assertNotIn(3, [r['realm_id'] for r in detail['realm_data']])

    def test_new_lots_and_removals_replace_prior_variants(self):
        main._update_realm_in_cache(1, ahgem.process_auctions({'auctions': [lot(90, (40,49), True)]}))
        row = self.get('/browser/api/items', boe='true')['items'][0]
        self.assertEqual(row['stats'], ['versatility', 'mastery']); self.assertEqual(row['min_price'], 90*G)
        main._update_realm_in_cache(1, {})
        self.assertNotIn(1, [r['realm_id'] for r in main._boe_variant_rows(IID,321)])

    def test_tertiary_filter_in_sniper_browser_and_detail(self):
        main._update_realm_in_cache(1, ahgem.process_auctions({'auctions': [
            lot(80), lot(100, effect=40), lot(120, effect=41),
            lot(140, effect=42), lot(160, effect=43), lot(60, (32,40), True, effect=41)]}))
        for effect, price in [('none',80),('avoidance',100),('leech',120),('speed',140),('indestructible',160)]:
            with self.subTest(effect=effect):
                params=dict(boe_stats='haste+mastery',boe_socket='no',boe_effect=effect)
                row=self.get('/browser/api/items',boe='true',**params)['items'][0]
                self.assertEqual(row['min_price'],price*G)
                detail=self.get('/browser/api/item',id=IID,ilvl=321,**params)
                self.assertEqual(detail['min_price'],price*G)
                filters=[dict(ids=str(IID),stats='haste+mastery',socket='no',effect=effect)]
                sniper=self.get('/api/items',boe_filters=json.dumps(filters))['items'][0]
                self.assertEqual(sniper['price_raw'],price*G)
                self.assertEqual(sniper['effects'],[] if effect=='none' else [effect])
        self.assertFalse(main._boe_variant_matches({'effects':None},wanted_effect='none'))
        self.assertEqual(self.get('/browser/api/items',boe_effect='speed')['items'][0]['min_price'],60*G)

    def test_subfilters_discount_top_realms_and_supabase_parity(self):
        filters = [dict(ids=str(IID), ilvl_min=292, ilvl_max=324, stats='haste+mastery', socket='yes', discount=90, topx=2),
                   dict(ids=str(IID), ilvl_min=292, ilvl_max=324, stats='haste+mastery', socket='yes', effect='none', discount=40, topx=2)]
        local = self.get('/api/items', boe_filters=json.dumps(filters))['items'][0]
        self.assertEqual(local['price_raw'], 250*G); self.assertEqual(local['realm_id'], 2)
        self.assertEqual(local['discount_raw'], 50)
        self.assertEqual(local['top3'], 'Realm B (250.00), Realm A (300.00)')
        # Capture the actual production payload without contacting Supabase.
        response = Mock(status_code=204)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, 'supabase_config.json').write_text(json.dumps({'supabase_url':'https://example.invalid','service_key':'test-only'}))
            with patch.object(main, 'ADMIN_BUILD', True), patch.object(main, 'data', side_effect=lambda f: os.path.join(tmp, f)), patch.object(main.requests, 'patch', return_value=response) as push:
                main._push_to_supabase()
        self.assertTrue(push.called)
        payload = json.loads(push.call_args.kwargs['json']['items_json'])
        self.assertEqual(len(payload['items'][0]['variants']), 6)
        js = "const b=require('./static/boe.js');let p=JSON.parse(require('fs').readFileSync(0,'utf8'));process.stdout.write(JSON.stringify(b.filterItems(p.items,p.config)));"
        web = json.loads(subprocess.check_output(['node','-e',js], cwd=ROOT,
              input=json.dumps({'items':payload['items'], 'config':{'boeFilters':filters}}).encode()))[0]
        for field in ('price_raw','realm_id','stats','sockets','effects','discount_raw','quantity','top3','sale_realm','sale_price'):
            self.assertEqual(local[field], web[field], field)
        self.assertEqual(self.get('/api/items', boe_filters=json.dumps(filters[:1]))['items'], [])

    def test_snapshot_restore_and_mismatched_snapshot_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_boe_live_path', return_value=os.path.join(tmp,'variants.json')):
            main._cat = 'test-snapshot'
            main._save_boe_variant_cache(); main._boe_variant_cache.clear(); main._load_boe_variant_cache()
            self.assertEqual(len(main._boe_variant_cache), 2)
            main._boe_variant_cache.clear(); main._cat = 'another-snapshot'; main._load_boe_variant_cache()
            self.assertEqual(main._boe_variant_cache, {})

    def test_admin_startup_offline_does_not_disable_user_license(self):
        import license_manager
        with patch.object(license_manager, 'check_license', return_value={'valid': False}) as check:
            with patch.object(main, 'ADMIN_BUILD', True):
                self.assertTrue(main._check_license_valid())
                self.assertTrue(self.get('/api/license/status')['valid'])
                check.assert_not_called()
            with patch.object(main, 'ADMIN_BUILD', False):
                self.assertFalse(main._check_license_valid())
                check.assert_called_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
