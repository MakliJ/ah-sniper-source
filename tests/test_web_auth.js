// Execute the scripts actually rendered by Flask, including configured analytics.
const assert = require('node:assert/strict');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const source = spawnSync('python', ['-c', `
import sys,json,os
sys.path.insert(0,'desktop')
import main
os.environ['YANDEX_METRIKA_ID']='12345'
os.environ['GA_MEASUREMENT_ID']='G-TEST'
with main.app.test_request_context('/analytics.js',base_url='https://example.invalid'):
 print(json.dumps({'login':main.LOGIN_PAGE,'register':main.REGISTER_PAGE,'analytics':main._analytics_script().get_data(as_text=True)}))
`], {cwd:path.resolve(__dirname,'..'), encoding:'utf8'});
assert.equal(source.status, 0, source.stderr);
const pages = JSON.parse(source.stdout);
function setup(page='login', track=()=>{}) {
  const elements = {email:{value:'test@example.invalid'},pass:{value:'test-password'},btn:{disabled:false},msg:{},authForm:{}};
  const timers = new Map(); let nextTimer=0;
  const context = {AbortController, location:{hostname:'example.invalid',pathname:'/'+page,replace(url){context.destination=url;}},
    document:{getElementById:id=>elements[id],createElement:()=>({}),scripts:[],head:{appendChild(){}},addEventListener(){}},
    setTimeout(fn){timers.set(++nextTimer,fn);return nextTimer;},clearTimeout(id){timers.delete(id);},
    ahTrack:track, fetch:async()=>({ok:true,json:async()=>({ok:true})})};
  elements.authForm.addEventListener=(name,fn)=>{context.submit=fn;};
  context.window=context;
  vm.createContext(context);
  vm.runInContext(pages[page].match(/<script>([\s\S]*?)<\/script>/)[1],context);
  return {context,elements,timers};
}
(async()=>{
  for(const page of ['login','register']) {
    const {context,elements,timers}=setup(page,()=>{throw Error('blocked analytics');});
    await (page==='login'?context.doLogin():context.doReg());
    assert.equal(context.destination,'/app','analytics failure must not block navigation');
    assert.equal(timers.size,0);assert.equal(elements.btn.disabled,false);
  }
  for(const failure of ['network','html','timeout','credentials','quota']) {
    const {context,elements,timers}=setup();
    context.fetch=async(url,opts)=>{
      if(failure==='network')throw TypeError('offline');
      if(failure==='timeout')return new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(Object.assign(Error(),{name:'AbortError'}))));
      return {ok:failure==='credentials',json:async()=>{
        if(failure==='html')throw SyntaxError('HTML gateway error');
        return {ok:false,reason:'Try again'};
      }};
    };
    const pending=context.doLogin();
    if(failure==='timeout')[...timers.values()][0]();
    await pending;
    assert.equal(context.destination,undefined);assert.ok(elements.msg.textContent);
    assert.equal(elements.btn.disabled,false);assert.equal(timers.size,0);
    context.fetch=async()=>({ok:true,json:async()=>({ok:true})});
    await context.doLogin();assert.equal(context.destination,'/app','retry without refresh');
  }
  {
    const {context}=setup();let calls=0,finish;
    context.fetch=()=>{calls++;return new Promise(resolve=>{finish=resolve;});};
    const first=context.doLogin();await context.doLogin();assert.equal(calls,1);
    finish({ok:true,json:async()=>({ok:true})});await first;
  }
  {
    const {context}=setup();let prevented=false;
    context.submit({preventDefault(){prevented=true;}});
    await new Promise(resolve=>setImmediate(resolve));
    assert.ok(prevented);assert.equal(context.destination,'/app');
  }
  for(const preinitialized of [false,true]) {
    const {context}=setup();const events=[];
    if(preinitialized){context.AH_YM_ID=12345;context.ym=(...args)=>events.push(args);}
    vm.runInContext(pages.analytics,context);
    context.ym=(...args)=>events.push(args);
    context.ahTrack('login_success');
    assert.ok(events.some(args=>args[1]==='reachGoal'&&args[2]==='login_success'));
    context.gtag=()=>{throw Error('GA unavailable');};
    context.ahTrack('registration_success');
    assert.ok(events.some(args=>args[2]==='registration_success'));
    context.ym=()=>{throw Error('YM unavailable');};
    assert.doesNotThrow(()=>context.ahTrack('login_success'));
  }
  console.log('PASS: auth redirects, analytics, network/HTML/timeout recovery, retries, duplicate submit, Enter');
})().catch(error=>{console.error(error);process.exitCode=1;});
