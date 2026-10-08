/* Exact-lot filtering shared by Supabase sniper data and the item UI. */
(function(root){
'use strict';
const statOptions = [['','Все статы'],['critical_strike+haste','Crit + Haste'],
    ['critical_strike+versatility','Crit + Versatility'],['critical_strike+mastery','Crit + Mastery'],
    ['haste+versatility','Haste + Versatility'],['haste+mastery','Haste + Mastery'],
    ['versatility+mastery','Versatility + Mastery']];
const socketOptions = [['','Сокет: любой'],['yes','С сокетом'],['no','Без сокета']];
const effectOptions = [['','Любой'],['none','Без доп. стата'],['avoidance','Avoidance'],
    ['leech','Leech'],['speed','Speed'],['indestructible','Indestructible']];
function matches(row, stats, socket, effect){
    const wanted = String(stats||'').toLowerCase().replace(/\s/g,'');
    if(wanted && wanted!=='all' && wanted!=='any' && !wanted.split('+').every(s=>(row.stats||[]).includes(s)))return false;
    if(socket==='yes'||socket==='no'){
        if(row.sockets==null || (row.sockets>0)!==(socket==='yes'))return false;
    }
    if(effect && effect!=='any' && effect!=='all'){
        if(row.effects==null)return false;
        return effect==='none'?row.effects.length===0:row.effects.includes(effect);
    }
    return true;
}
function gold(price){
    return price?Math.floor(price/10000)+'.'+String(Math.floor(price%10000/100)).padStart(2,'0'):'—';
}
function realmRows(rows,stats,socket,effect){
    const realms=new Map();
    rows.filter(r=>matches(r,stats,socket,effect)).sort((a,b)=>a.price-b.price||a.realm_id-b.realm_id).forEach(r=>{
        if(!realms.has(r.realm_id))realms.set(r.realm_id,r);
    });
    return Array.from(realms.values());
}
function select(item,rows,topx){
    const first=rows[0],last=rows.reduce((a,b)=>a.price>b.price?a:b);
    const discount=item.top10avg_raw?Math.round((1-first.price/item.top10avg_raw)*1000)/10:0;
    return Object.assign({},item,{
        price_raw:first.price,price:gold(first.price),quantity:first.quantity,
        realm:first.realm_name,realm_id:first.realm_id,stats:first.stats||[],
        stats_label:first.stats_label||'',sockets:first.sockets,effects:first.effects,
        discount_raw:discount,discount:discount+'%',
        top3:rows.slice(0,topx||3).map(r=>r.realm_name+' ('+gold(r.price)+')').join(', '),
        sale_realm:last.realm_name,sale_price:gold(last.price),
        url:'https://undermine.exchange/#'+(item.region||'eu')+'/'+first.realm_id+'/'+item.item_id
    });
}
function filterItems(items,config){
    const filters=config.boeFilters||[], ids=config.ids||[], search=(config.search||'').toLowerCase();
    const result=[];
    for(const item of items){
        const main=config.itemsEnabled!==false&&ids.includes(item.item_id)&&
            (!config.discount||item.discount_raw>=config.discount)&&(!config.avg||item.top10avg_raw>=config.avg*10000)&&
            (!search||(item.name||'').toLowerCase().includes(search)||String(item.item_id).includes(search));
        let best=null;
        for(const f of item.ilvl>0?filters:[]){
            const ids=String(f.ids||'').split(',').map(Number);
            if(!ids.includes(item.item_id)||item.ilvl<Number(f.ilvl_min||0)||item.ilvl>Number(f.ilvl_max??999))continue;
            // Schema before exact per-realm variants cannot answer selective filters.
            let rows=(item.variants||[]).filter(r=>r.price>0 && r.realm_id!=null);
            if(!rows.length)rows=[{price:item.price_raw,realm_id:item.realm_id,realm_name:item.realm,
                quantity:item.quantity,stats:[],stats_label:'',sockets:null}];
            rows=realmRows(rows,f.stats,f.socket,f.effect);
            if(!rows.length)continue;
            const candidate=select(item,rows,Math.max(1,Number(f.topx)||3));
            if(!item._is_first&&candidate.discount_raw<Number(f.discount||0))continue;
            if(!best||candidate.price_raw<best.price_raw)best=candidate;
        }
        if(best)result.push(best);else if(main)result.push(item);
    }
    return result;
}
function label(row,boe){
    if(!boe)return '';
    return (row.stats_label||row.statsText||'Статы неизвестны')+' · '+
        (row.sockets==null?'сокет неизвестен':row.sockets>0?'◇ '+row.sockets:'без сокета')+
        (Array.isArray(row.effects)&&row.effects.length?' · '+row.effects.map(function(effect){
            var option=effectOptions.find(function(o){return o[0]===effect;});return option?option[1]:effect;
        }).join(', '):'');
}
function detailSelection(item){
    return {itemId:item.item_id,ilvl:item.ilvl||0,
        stats:item.ilvl>0?(item.stats||[]).join('+'):'',
        socket:item.ilvl>0&&item.sockets!=null?(item.sockets>0?'yes':'no'):'',
        effect:item.ilvl>0&&Array.isArray(item.effects)?(item.effects[0]||'none'):'',
        realmId:item.realm_id,realm:item.realm,price:item.price_raw};
}
const api={matches,realmRows,filterItems,label,statOptions,socketOptions,effectOptions,detailSelection};
root.Boe=api;
if(typeof module!=='undefined')module.exports=api;
})(typeof globalThis!=='undefined'?globalThis:this);
