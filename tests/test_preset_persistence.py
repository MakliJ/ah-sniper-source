"""Cloud failures must never replace presets or report a successful save."""
import io
import json
import sys
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'desktop')]
import main

class PresetPersistenceTest(unittest.TestCase):
    def setUp(self):
        main.app.config.update(TESTING=True,SECRET_KEY='test-only')
        self.client=main.app.test_client()
        with self.client.session_transaction() as s:
            s['user_id']='account-a';s['tier']='pro'

    def test_cloud_read_failure_never_writes_empty_defaults(self):
        for response in (None,[],True,[{'presets':'broken json'}]):
            with patch.object(main,'_is_web_request',return_value=True),patch.object(main,'_supabase_web',return_value=response) as cloud:
                r=self.client.post('/api/presets',json={'id':1,'discount':50})
                self.assertEqual(r.status_code,503)
                self.assertEqual([c.args[0] for c in cloud.call_args_list],['GET'])

    def test_failed_patch_and_missing_preset_are_errors(self):
        with patch.object(main,'_is_web_request',return_value=True),patch.object(main,'_load_presets_ctx',return_value=[{'id':1}]),patch.object(main,'_save_presets_ctx',return_value=False) as save:
            self.assertEqual(self.client.post('/api/presets',json={'id':1}).status_code,503)
            self.assertEqual(self.client.post('/api/presets',json={'id':2}).status_code,404)
            self.assertEqual(save.call_count,1)

    def test_items_switch_round_trip_preserves_ids_and_boe(self):
        plist=[{'id':1,'item_ids':'999','boe_filters':'[{"ids":"271445"}]','boe_enabled':True}]
        with patch.object(main,'_is_web_request',return_value=False),patch.object(main,'_load_presets_ctx',return_value=plist),patch.object(main,'_save_presets_ctx',return_value=True):
            for enabled in (False, True):
                r=self.client.post('/api/presets',json={'id':1,'items_enabled':enabled})
                self.assertEqual(r.status_code,200)
                saved=self.client.get('/api/presets').json[0]
                self.assertIs(saved['items_enabled'],enabled)
                self.assertEqual(saved['item_ids'],'999');self.assertTrue(saved['boe_enabled'])
                self.assertEqual(saved['boe_filters'],'[{"ids":"271445"}]')

    def test_all_boe_fields_round_trip_in_existing_json(self):
        plist=[{'id':1,'name':'temporary','item_ids':'999'}]
        groups=[{'ids':'271436','ilvl_min':324,'ilvl_max':324,'stats':'haste+mastery',
                 'socket':'yes','effect':'leech','discount':30,'topx':6},
                {'ids':'271445','ilvl_min':321,'ilvl_max':328,'stats':'critical_strike+versatility',
                 'socket':'no','effect':'speed','discount':12,'topx':3}]
        def cloud(method,table,payload=None,params=None):
            if method=='GET':return [{'id':'account-a','presets':json.dumps(plist)}]
            plist[:]=json.loads(payload['presets'])
            return [{'id':'account-a'}]
        with patch.object(main,'_is_web_request',return_value=True),patch.object(main,'_supabase_web',side_effect=cloud):
            response=self.client.post('/api/presets',json={'id':1,'boe_filters':groups,'items_enabled':False,'boe_enabled':True})
            self.assertEqual(response.status_code,200)
            saved=self.client.get('/api/presets').json[0]
            self.assertEqual(json.loads(saved['boe_filters']),groups)
            self.assertFalse(saved['items_enabled']);self.assertTrue(saved['boe_enabled'])
            self.assertEqual(saved['item_ids'],'999')

    def test_create_retry_is_idempotent_and_does_not_require_new_columns(self):
        plist=[]
        with patch.object(main,'_is_web_request',return_value=False),patch.object(main,'_load_presets_ctx',return_value=plist),patch.object(main,'_save_presets_ctx',return_value=True):
            payload={'client_id':'temporary-create-token','name':'temporary','boe_filters':'[]'}
            first=self.client.post('/api/presets',json=payload)
            payload['discount']=55
            retry=self.client.post('/api/presets',json=payload)
            self.assertEqual(first.json['id'],retry.json['id']);self.assertEqual(len(plist),1)
            self.assertEqual(plist[0]['discount'],55)

    def test_patch_requires_confirmation_for_the_correct_account(self):
        for result in (True,[],[{'id':'another-account'}]):
            with main.app.test_request_context('/api/presets'),patch.object(main,'_is_web_request',return_value=True),patch.object(main,'_supabase_web',return_value=result):
                main.session['user_id']='account-a'
                self.assertFalse(main._save_presets_ctx([{'id':1}]))

    def test_rename_and_subscription_expiry_preserve_saved_boe(self):
        groups=json.dumps([{'ids':'271436','socket':'yes','effect':'leech','stats':'haste+mastery'}])
        plist=[{'id':1,'name':'before','item_ids':'999','boe_filters':groups,'boe_enabled':True}]
        with self.client.session_transaction() as s:s['tier']='none'
        with patch.object(main,'_is_web_request',return_value=True),patch.object(main,'_load_presets_ctx',return_value=plist),patch.object(main,'_save_presets_ctx',return_value=True):
            r=self.client.post('/api/presets',json={'id':1,'name':'after','boe_filters':'[]','boe_enabled':False})
            self.assertEqual(r.status_code,200);self.assertEqual(plist[0]['boe_filters'],groups)
            self.assertTrue(plist[0]['boe_enabled'])

    def test_invalid_boe_is_rejected_without_writing(self):
        with patch.object(main,'_save_presets_ctx') as save:
            for value in ('broken',{},[None]):
                self.assertEqual(self.client.post('/api/presets',json={'id':1,'boe_filters':value}).status_code,400)
            save.assert_not_called()

    def test_local_corrupt_file_is_not_overwritten_with_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'presets.json';path.write_text('{broken',encoding='utf-8')
            with patch.object(main,'PRESETS_FILE',str(path)),patch.object(main,'_is_web_request',return_value=False):
                self.assertEqual(self.client.post('/api/presets',json={'name':'temporary'}).status_code,503)
                self.assertEqual(path.read_text(encoding='utf-8'),'{broken')

    def test_picker_deduplicates_ilvls_and_finds_names_in_both_languages(self):
        with patch.object(main,'_inames',{101:'Test sword',202:'Test chair'}),patch.object(main,'_picker_names_ru',{101:'Тестовый меч'}),patch.object(main,'_cache',{(101,321):[],(101,324):[],(202,0):[]}),patch.object(main.browser,'_meta',{}):
            for query in ('sword','меч','101'):
                result=self.client.get('/browser/api/picker',query_string={'search':query,'boe':'true'}).json
                self.assertEqual([p['id'] for p in result['items']],[101])
                self.assertEqual(result['total'],1)
            unknown=self.client.get('/browser/api/picker?ids=999').json['items'][0]
            self.assertEqual(unknown['name'],'Item 999');self.assertFalse(unknown['boe'])
            self.assertNotIn('stats',unknown)

    def test_import_does_not_claim_success_on_quota_error(self):
        with patch.object(main,'_is_web_request',return_value=True),patch.object(main,'_save_presets_ctx',return_value=False):
            r=self.client.post('/profile/import_presets',data={'file':(io.BytesIO(b'[]'),'presets.json')})
            self.assertEqual(r.status_code,503);self.assertFalse(r.json['ok'])

    def test_scope_is_stable_and_account_specific(self):
        with patch.object(main,'_is_web_request',return_value=True),patch.object(main,'ADMIN_BUILD',True):
            a=self.client.get('/api/env').json['preset_scope']
            with self.client.session_transaction() as s:s['user_id']='account-b'
            b=self.client.get('/api/env').json['preset_scope']
            self.assertNotEqual(a,b)
            with self.client.session_transaction() as s:s.clear()
            self.assertIsNone(self.client.get('/api/env').json['preset_scope'])

    def test_snapshot_retry_stops_after_success(self):
        with patch.object(main,'ADMIN_BUILD',True),patch.object(main,'_supabase_retry',None),patch.object(main,'_push_to_supabase_once',side_effect=[False,True]) as push,patch.object(main.threading,'Timer') as timer:
            main._push_to_supabase()
            timer.assert_called_once_with(60,main._push_to_supabase,kwargs={'retry':True})
            timer.return_value.start.assert_called_once()
            main._push_to_supabase(retry=True)
            timer.return_value.cancel.assert_called_once()
            self.assertIsNone(main._supabase_retry)
            self.assertEqual(push.call_count,2)

if __name__=='__main__':unittest.main()
