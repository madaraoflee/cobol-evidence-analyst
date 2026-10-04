'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const source=()=>fs.readFileSync(path.join(__dirname,'../web/layout.js'),'utf8');
const storageKey='workbench.rail-widths';

function harness(options={}){
  let viewport=options.width??1440;
  const stored=options.storage??new Map();
  const writes=[];
  const properties=new Map();
  const elements=new Map();
  const listeners=new Map();
  const classes=new Set();
  const listen=(target,type,handler)=>{
    const key=target+':'+type;
    if(!listeners.has(key))listeners.set(key,new Set());
    listeners.get(key).add(handler);
  };
  const unlisten=(target,type,handler)=>listeners.get(target+':'+type)?.delete(handler);
  const style={setProperty(name,value){properties.set(name,String(value));},getPropertyValue:name=>properties.get(name)??'',removeProperty:name=>properties.delete(name)};
  const classList={add:token=>classes.add(token),remove:token=>classes.delete(token),contains:token=>classes.has(token),toggle(token,force){const on=force??!classes.has(token);if(on)classes.add(token);else classes.delete(token);return on;}};
  const preference=name=>Number.parseFloat(properties.get('--'+name+'-width'))||(name==='navigation'?232:264);
  const railWidth=name=>{
    const min=name==='navigation'?200:220;
    const max=name==='navigation'?Math.min(360,viewport*(viewport>1180?.24:.36)):Math.min(420,viewport*.30);
    return Math.max(min,Math.min(max,preference(name)));
  };
  function element(id){
    const attributes=new Map();
    const item={id,style,classList,hidden:false,parentElement:null,attributes,
      setAttribute:(name,value)=>attributes.set(name,String(value)),getAttribute:name=>attributes.get(name)??null,
      addEventListener:(type,handler)=>listen(id,type,handler),removeEventListener:(type,handler)=>unlisten(id,type,handler),
      setPointerCapture(pointerId){this.captured=pointerId;},releasePointerCapture(pointerId){if(this.captured===pointerId)this.captured=null;},hasPointerCapture(pointerId){return this.captured===pointerId;},
      getClientRects(){return this.hidden||(id==='navigation-resizer'&&viewport<=760)||(id==='activity-resizer'&&viewport<=1180)?[]:[this.getBoundingClientRect()];},
      focus(){document.activeElement=this;},
      getBoundingClientRect(){
        const width=id==='app-shell'?viewport:id==='workspace-sidebar'?railWidth('navigation'):id==='activity-sidebar'?railWidth('activity'):6;
        const left=id==='activity-sidebar'?viewport-width:id==='activity-resizer'?viewport-railWidth('activity')-width:id==='navigation-resizer'?railWidth('navigation'):0;
        return {left,right:left+width,width,top:0,bottom:900,height:900};
      },
      closest(selector){return selector==='.app-shell'&&this.parentElement===shell?shell:null;}
    };
    Object.defineProperty(item,'clientWidth',{get:()=>item.getBoundingClientRect().width});
    elements.set(id,item);return item;
  }
  const shell=element('app-shell');
  for(const id of ['workspace-sidebar','activity-sidebar','navigation-resizer','activity-resizer'])element(id).parentElement=shell;
  for(const [name,controlled] of [['navigation','workspace'],['activity','activity']])elements.get(name+'-resizer').setAttribute('aria-controls',controlled+'-sidebar');
  if(options.hiddenNavigation)elements.get('navigation-resizer').hidden=true;
  if(options.hiddenActivity)elements.get('activity-resizer').hidden=true;
  if(options.detachedActivity)elements.get('activity-sidebar').parentElement={id:'activity-dialog'};
  const root={style,classList,clientWidth:viewport};
  const document={documentElement:root,body:{style,classList},getElementById:id=>elements.get(id)??null,querySelector:selector=>selector==='.app-shell'?shell:null,
    addEventListener:(type,handler)=>listen('document',type,handler),removeEventListener:(type,handler)=>unlisten('document',type,handler)};
  const window={document,addEventListener:(type,handler)=>listen('window',type,handler),removeEventListener:(type,handler)=>unlisten('window',type,handler)};
  Object.defineProperty(window,'innerWidth',{get:()=>viewport});
  const localStorage={getItem(key){if(options.storageUnavailable)throw Error('Storage unavailable');return stored.get(key)??null;},setItem(key,value){if(options.storageUnavailable)throw Error('Storage unavailable');stored.set(key,String(value));writes.push({key,value:String(value)});}};
  const getComputedStyle=item=>({display:item.hidden?'none':'block',visibility:'visible',getPropertyValue:name=>style.getPropertyValue(name),width:item.getBoundingClientRect?.().width+'px'});
  const context=vm.createContext({window,document,localStorage,getComputedStyle,Number,Math,JSON,requestAnimationFrame:callback=>{callback();return 1;},cancelAnimationFrame:()=>{},ResizeObserver:class {observe(){}},MutationObserver:class {observe(){}}});
  window.getComputedStyle=getComputedStyle;window.localStorage=localStorage;
  const dispatch=(target,type,details={})=>{
    const event={target:elements.get(target)??document,currentTarget:elements.get(target)??window,pointerId:1,button:0,clientX:0,key:'',prevented:false,preventDefault(){this.prevented=true;},stopPropagation(){},...details};
    for(const listener of [...(listeners.get(target+':'+type)??[])])listener(event);
    return event;
  };
  vm.runInContext(source(),context);
  return {elements,stored,writes,dispatch,width:railWidth,preference,properties,
    resize(width){viewport=width;root.clientWidth=width;dispatch('window','resize');},
    drag(name,start,end){dispatch(name+'-resizer','pointerdown',{clientX:start});dispatch('window','pointermove',{clientX:end});dispatch('document','pointermove',{clientX:end});dispatch(name+'-resizer','pointermove',{clientX:end});dispatch('window','pointerup',{clientX:end});dispatch('document','pointerup',{clientX:end});dispatch(name+'-resizer','pointerup',{clientX:end});}
  };
}

test('each rail follows its dragged boundary and stops moving when released',()=>{
  const h=harness();
  h.drag('navigation',232,300);
  assert.equal(h.width('navigation'),300);
  assert.equal(h.width('activity'),264);
  h.drag('activity',1176,1126);
  assert.equal(h.width('activity'),314,'moving the right boundary left must expand the right rail');
  h.drag('activity',1126,1176);
  assert.equal(h.width('activity'),264,'moving the right boundary right must shrink the right rail');
  h.dispatch('navigation-resizer','pointermove',{clientX:600});
  assert.equal(h.width('navigation'),300,'released drag must not follow later pointer movement');
  assert.equal(h.elements.get('navigation-resizer').captured,null);
});

test('drag bounds retain a usable reading surface and rail minimums',()=>{
  const h=harness({width:1200});
  h.drag('navigation',232,2000);
  h.drag('activity',936,-1000);
  assert.ok(1200-h.width('navigation')-h.width('activity')>=480);
  assert.ok(h.width('navigation')<=360);
  assert.ok(h.width('activity')<=420);
  h.drag('navigation',288,-2000);
  h.drag('activity',840,3000);
  assert.equal(h.width('navigation'),200);
  assert.equal(h.width('activity'),220);
  assert.equal(h.elements.get('navigation-resizer').getAttribute('aria-valuenow'),'200');
  assert.equal(h.elements.get('activity-resizer').getAttribute('aria-valuenow'),'220');
});

test('keyboard arrows follow both boundaries and support larger steps and bounds',()=>{
  const h=harness();
  const nav=h.dispatch('navigation-resizer','keydown',{key:'ArrowRight'});
  assert.equal(nav.prevented,true);
  assert.equal(h.width('navigation'),240);
  h.dispatch('navigation-resizer','keydown',{key:'ArrowLeft',shiftKey:true});
  assert.equal(h.width('navigation'),208);
  h.dispatch('activity-resizer','keydown',{key:'ArrowLeft'});
  assert.equal(h.width('activity'),272);
  h.dispatch('activity-resizer','keydown',{key:'ArrowRight',shiftKey:true});
  assert.equal(h.width('activity'),240);
  h.dispatch('navigation-resizer','keydown',{key:'Home'});
  assert.equal(h.width('navigation'),200);
  h.dispatch('activity-resizer','keydown',{key:'End'});
  assert.equal(h.width('activity'),420);
  assert.equal(h.elements.get('activity-resizer').getAttribute('aria-valuenow'),'420');
  assert.equal(h.dispatch('activity-resizer','keydown',{key:'Enter'}).prevented,false);
});

test('finished changes survive reload and window resizing preserves the preferred width',()=>{
  const h=harness({width:1600});
  h.drag('navigation',232,352);
  h.drag('activity',1336,1216);
  assert.deepEqual(JSON.parse(h.stored.get(storageKey)),{navigation:352,activity:384});
  const reloaded=harness({width:1600,storage:h.stored});
  assert.equal(reloaded.width('navigation'),352);
  assert.equal(reloaded.width('activity'),384);
  reloaded.resize(1200);
  assert.equal(reloaded.preference('navigation'),352);
  assert.equal(reloaded.preference('activity'),384);
  assert.equal(reloaded.width('navigation'),288);
  assert.equal(reloaded.width('activity'),360);
  assert.equal(reloaded.elements.get('navigation-resizer').getAttribute('aria-valuenow'),'288');
  reloaded.resize(1600);
  assert.equal(reloaded.width('navigation'),352);
  assert.equal(reloaded.width('activity'),384);
});

test('double click returns each rail to its default and saves that change',()=>{
  const h=harness();
  h.drag('navigation',232,320);
  h.drag('activity',1176,1096);
  h.dispatch('navigation-resizer','dblclick');
  h.dispatch('activity-resizer','dblclick');
  assert.equal(h.width('navigation'),232);
  assert.equal(h.width('activity'),264);
  assert.deepEqual(JSON.parse(h.stored.get(storageKey)),{navigation:232,activity:264});
});

test('invalid saved values and unavailable storage cannot prevent rail controls from working',()=>{
  for(const value of ['broken JSON','null','[]','{"navigation":"wide","activity":{}}']){
    const h=harness({storage:new Map([[storageKey,value]])});
    assert.equal(h.width('navigation'),232);
    assert.equal(h.width('activity'),264);
    h.dispatch('navigation-resizer','keydown',{key:'ArrowRight'});
    assert.equal(h.width('navigation'),240);
  }
  const bounded=harness({storage:new Map([[storageKey,'{"navigation":-1000,"activity":10000}']])});
  assert.equal(bounded.width('navigation'),200);
  assert.equal(bounded.width('activity'),420);
  const unavailable=harness({storageUnavailable:true});
  unavailable.drag('navigation',232,300);
  assert.equal(unavailable.width('navigation'),300);
  unavailable.dispatch('activity-resizer','keydown',{key:'ArrowLeft'});
  assert.equal(unavailable.width('activity'),272);
});

test('hidden handles and rails inside drawers do not change desktop width preferences',()=>{
  for(const options of [{width:600},{hiddenNavigation:true,hiddenActivity:true},{detachedActivity:true}]){
    const h=harness(options);
    const railNames=options.detachedActivity?['activity']:['navigation','activity'];
    for(const name of railNames){
      const before=h.preference(name);
      h.drag(name,400,900);
      assert.equal(h.dispatch(name+'-resizer','keydown',{key:'ArrowLeft'}).prevented,false);
      h.dispatch(name+'-resizer','dblclick');
      assert.equal(h.preference(name),before);
    }
    assert.equal(h.writes.length,0);
  }
  const h=harness({width:900});
  h.dispatch('navigation-resizer','keydown',{key:'End'});
  assert.equal(h.width('navigation'),324,'the hidden activity rail must not consume the left rail\'s space');
  assert.ok(900-h.width('navigation')>=480);
});

test('drag ignores unrelated pointers and releases capture on cancellation or viewport changes',()=>{
  for(const end of ['pointercancel','lostpointercapture','resize']){
    const h=harness();
    h.dispatch('navigation-resizer','pointerdown',{clientX:232,pointerId:7});
    h.dispatch('navigation-resizer','pointermove',{clientX:350,pointerId:8});
    h.dispatch('navigation-resizer','pointerup',{pointerId:8});
    assert.equal(h.width('navigation'),232);
    h.dispatch('navigation-resizer','pointermove',{clientX:300,pointerId:7});
    assert.equal(h.width('navigation'),300);
    if(end==='resize')h.resize(1300);else h.dispatch('navigation-resizer',end,{pointerId:7});
    h.dispatch('navigation-resizer','pointermove',{clientX:330,pointerId:7});
    assert.equal(h.width('navigation'),300);
    assert.equal(h.elements.get('navigation-resizer').captured,null);
  }
});
