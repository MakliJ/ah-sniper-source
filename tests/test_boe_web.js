const assert=require('node:assert/strict');
const b=require('../static/boe.js');
const item={item_id:1,ilvl:321,price_raw:1000000,top10avg_raw:5000000,discount_raw:80,name:'Belt',
    variants:[{realm_id:1,realm_name:'A',price:1000000,quantity:1,stats:['haste','mastery'],sockets:0},
              {realm_id:2,realm_name:'B',price:2000000,quantity:1,stats:['critical_strike','versatility'],sockets:1}]};
const config={boeFilters:[{ids:'1',stats:'haste+mastery',socket:'yes'}]};
assert.deepEqual(b.filterItems([item],config),[], 'Stats and socket must belong to the same lot');
assert.equal(b.matches({sockets:null},'','no'),false);
assert.equal(b.label({},false),'');
assert.equal(b.label({sockets:null},true),'Статы неизвестны · сокет неизвестен');
const copy=JSON.stringify(item);
const selected=b.filterItems([item],{boeFilters:[{ids:'1',socket:'yes'}]})[0];
assert.equal(selected.price_raw,2000000);assert.equal(selected.discount_raw,60);assert.equal(selected.realm_id,2);
assert.equal(JSON.stringify(item),copy,'Filtering must not mutate the Supabase snapshot');
assert.equal(b.filterItems([item],{ids:[1],boeFilters:[{ids:'1',socket:'yes',discount:90}]}).length,1,'Main selection remains independent');
const ordinary={item_id:99,ilvl:0,discount_raw:50,top10avg_raw:5000000,name:'Decor'};
const both=[ordinary,item], rule=[{ids:'1',socket:'yes'}];
const ids=c=>b.filterItems(both,c).map(x=>x.item_id);
assert.deepEqual(ids({ids:[99]}),[99]);
assert.deepEqual(ids({ids:[99],itemsEnabled:false}),[]);
assert.deepEqual(ids({ids:[99],boeFilters:rule}),[99,1]);
assert.deepEqual(ids({ids:[99],itemsEnabled:false,boeFilters:rule}),[1]);
assert.deepEqual(ids({ids:[]}),[]);
assert.deepEqual(ids({ids:[],discount:1,avg:1,search:'Decor'}),[]);
assert.deepEqual(ids({ids:[],discount:100,avg:999999,search:'unrelated',boeFilters:rule}),[1]);
assert.equal(b.matches({effects:null},'','','none'),false);
assert.equal(b.matches({effects:[]},'','','none'),true);
for(const effect of ['avoidance','leech','speed','indestructible']){
    assert.equal(b.matches({effects:[effect]},'','',effect),true);
    assert.equal(b.matches({effects:[effect]},'','','none'),false);
}
const effectItem={...item,variants:[
    {...item.variants[0],effects:[]},
    {...item.variants[0],price:1700000,effects:['leech']},
    {...item.variants[1],effects:['leech']}]};
assert.deepEqual(b.filterItems([effectItem],{boeFilters:[{ids:'1',stats:'haste+mastery',socket:'yes',effect:'leech'}]}),[]);
const leech=b.filterItems([effectItem],{boeFilters:[{ids:'1',stats:'haste+mastery',effect:'leech'}]})[0];
assert.equal(leech.price_raw,1700000);
assert.deepEqual(b.detailSelection(leech),{itemId:1,ilvl:321,stats:'haste+mastery',socket:'no',effect:'leech',realmId:1,realm:'A',price:1700000});
assert.equal(b.detailSelection({...leech,effects:[]}).effect,'none');
assert.equal(b.detailSelection({...leech,effects:null,sockets:null}).effect,'');
assert.deepEqual(b.detailSelection({item_id:99,ilvl:0}).stats,'');
console.log('BoE web tests passed');

// A completed 304 scan restores the same snapshot marker: still refresh once.
async function testRestoredSnapshotRefresh(){
    const fs=require('node:fs'),vm=require('node:vm');
    const html=fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8');
    const code=html.slice(html.indexOf('async function pollStatus(){'),html.indexOf('setInterval(pollStatus,2000);'));
    let refreshes=0;
    const context={_isWeb:false,_grid:{},_prevState:'collecting',_lastDataMarker:'saved|1',
        api:async path=>path==='/api/status'?{state:'monitoring',collected_at:'saved',gen:1,realms_done:92}:{logs:[]},
        document:{getElementById:()=>({})},setProgress:()=>{},toast:()=>{},t:x=>x,playNotifySound:()=>{},
        applyFilters:()=>refreshes++};
    vm.createContext(context);vm.runInContext(code,context);
    await context.pollStatus();assert.equal(refreshes,1,'Refresh after same-snapshot restore');
    await context.pollStatus();assert.equal(refreshes,1,'Do not reload unchanged monitoring data repeatedly');
    console.log('Snapshot restore UI test passed');
}
testRestoredSnapshotRefresh().catch(error=>{console.error(error);process.exitCode=1;});
