const assert=require('node:assert/strict');
const fs=require('node:fs');
const {create}=require('../static/preset-store.js');
const values=new Map();
const storage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v)};
(async()=>{
    let online=false,calls=[];
    const send=async d=>{calls.push(d);return online?{ok:true}:null;};
    let a=create(storage,'account-a',send,()=>{});
    a.put({id:0,discount:44,boe_filters:'[]',items_enabled:false});
    await a.flush();assert(a.hasPending());
    a=create(storage,'account-a',send,()=>{});
    assert.equal(a.overlay({id:0,discount:1}).discount,44);
    assert.equal(a.overlay({id:0,items_enabled:true}).items_enabled,false);
    const b=create(storage,'account-b',send,()=>{});
    assert.equal(b.overlay({id:0,discount:1}).discount,1);
    a.put({id:2,discount:70});online=true;await a.flush();
    assert.deepEqual(calls.slice(-2).map(d=>[d.id,d.discount]),[[0,44],[2,70]]);
    assert(!a.hasPending());
    let finish;
    const c=create(storage,'slow',()=>new Promise(r=>finish=r),()=>{});
    c.put({id:1,discount:20});const first=c.flush();
    c.put({id:1,discount:30});assert.strictEqual(c.flush(),first);
    finish({ok:true});await first;
    assert.equal(c.overlay({id:1}).discount,30);assert(c.hasPending());
    const second=c.flush();finish({ok:true});await second;assert(!c.hasPending());
    // A confirmed write must update the local cache, including offline reloads.
    const afterAck=create(storage,'slow',send,()=>{});
    assert.equal((await afterAck.refresh(async()=>null))[0].discount,30);
    // An older GET may arrive after a PATCH; it must not replace that confirmation.
    let completeRead;
    const stale=afterAck.refresh(()=>new Promise(resolve=>completeRead=resolve));
    afterAck.put({id:1,discount:62});await afterAck.flush();
    completeRead([{id:1,discount:10}]);
    assert.equal((await stale)[0].discount,62);
    const groups=JSON.stringify([{ids:'271436',ilvl_min:324,ilvl_max:324,discount:11,topx:6,
        stats:'haste+mastery',socket:'yes',effect:'leech'},
        {ids:'271445',ilvl_min:321,ilvl_max:328,discount:22,topx:3,stats:'',socket:'no',effect:'speed'}]);
    online=false;
    let offline=create(storage,'all-fields',send,()=>{});
    offline.put({id:7,name:'offline test',boe_filters:groups,items_enabled:false,boe_enabled:true});await offline.flush();
    offline=create(storage,'all-fields',send,()=>{});
    const restored=(await offline.refresh(async()=>null))[0];
    assert.equal(restored.boe_filters,groups);assert.equal(restored.items_enabled,false);assert.equal(restored.boe_enabled,true);
    // Create while offline, then retry a lost response without changing the token.
    let createCalls=[],finishCreate;
    const drafts=create(storage,'new-draft',d=>{createCalls.push(d);return new Promise(r=>finishCreate=r);},()=>{});
    drafts.put({id:'draft:token-123',client_id:'token-123',name:'draft',boe_filters:groups});
    const creating=drafts.flush();drafts.put({id:'draft:token-123',discount:72});
    finishCreate({ok:true,id:81});await creating;
    assert(!Object.hasOwn(createCalls[0],'id'));assert.equal(createCalls[0].client_id,'token-123');
    assert.equal(drafts.resolve('draft:token-123'),81);assert(drafts.hasPending(81));
    assert.equal(drafts.list().length,1);assert.equal(drafts.list()[0].discount,72);
    const followup=drafts.flush();finishCreate({ok:true,id:81});await followup;
    assert.equal(createCalls[1].id,81);assert.equal(drafts.list()[0].boe_filters,groups);
    // Two tabs must not erase each other's queued presets.
    const tabA=create(storage,'shared-tabs',send,()=>{}),tabB=create(storage,'shared-tabs',send,()=>{});
    tabA.put({id:1,discount:20});tabB.put({id:2,discount:40});tabA.put({id:1,boe_filters:groups});
    assert.equal(tabA.list().length,2);assert.equal(tabA.list().find(p=>p.id===2).discount,40);
    // A full browser storage must be reported, and must not erase in-memory edits.
    let full=false,states=[];
    const limited={getItem:k=>storage.getItem(k),setItem:(k,v)=>{if(full)throw Error('quota');storage.setItem(k,v);}};
    const memory=create(limited,'limited',send,s=>states.push(s));
    memory.put({id:1,discount:20});full=true;memory.put({id:1,discount:30});memory.put({id:1,boe_filters:groups});
    assert.equal(memory.list()[0].discount,30);assert.equal(memory.list()[0].boe_filters,groups);assert(states.includes('storage'));
    let blockedCalls=[];
    const blocked=create(storage,'blocked-one',async d=>{blockedCalls.push(d.id);return d.id===1?{ok:false,error:'preset_not_found'}:{ok:true,id:d.id};},()=>{});
    blocked.put({id:1,discount:20});blocked.put({id:2,discount:40});
    assert.equal(await blocked.flush(),false);assert(blocked.hasPending(1));assert(!blocked.hasPending(2));
    await blocked.flush();assert.deepEqual(blockedCalls,[1,2],'One deleted preset must not block the others or retry forever');
    const html=fs.readFileSync(require('node:path').join(__dirname,'../static/index.html'),'utf8');
    for(const id of ['minDiscount','minAvg','itemIds'])
        assert.match(html.match(new RegExp('id="'+id+'"[^>]*'))[0],/oninput="(?:applyFilters\(\);)?autoSavePreset\(\)"/);
    assert.match(html.match(/id="itemIds"[^>]*/)[0],/oninput="applyFilters\(\);autoSavePreset\(\)"/);
    assert(html.includes('detectEnv().then(function(){initWorkspace();return loadPresets();})'));
    for(const script of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new Function(script[1]);
    for(const file of ['presets.js','workspace.js','item-detail.js','market-grid.js'])new Function(fs.readFileSync(require('node:path').join(__dirname,'../static',file),'utf8'));
    console.log('Preset persistence: outage/reload, confirmation cache, stale reads, offline creation, account/tab isolation, independent BoE groups, storage errors passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
