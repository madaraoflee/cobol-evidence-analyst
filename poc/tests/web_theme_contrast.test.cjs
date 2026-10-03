'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const css=fs.readFileSync(path.join(__dirname,'../web/appearance.css'),'utf8');
function tokens(selector){
  const start=css.indexOf(selector+' {');
  const block=css.slice(start,css.indexOf('}',start));
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[\da-f]{6});/gi)].map(m=>[m[1],m[2]]));
}
function luminance(hex){
  const channel=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);
  return channel[0]*.2126+channel[1]*.7152+channel[2]*.0722;
}
function contrast(a,b){const x=luminance(a),y=luminance(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05);}
for(const theme of [':root','html[data-theme="dark"]']){
  test(theme+' text, statuses and controls meet readable contrast',()=>{
    const t=tokens(theme);
    const pairs=[['ink','canvas'],['ink-soft','canvas'],['muted','canvas'],['faint','canvas'],['accent-deep','accent-pale'],['error','error-bg'],['amber','amber-bg'],['green','green-bg']];
    for(const [fg,bg] of pairs)assert.ok(contrast(t[fg],t[bg])>=4.5,`${fg}/${bg} must meet 4.5:1`);
    const buttonText=theme===':root'?'#ffffff':'#34101a';
    for(const fill of ['accent','accent-deep'])assert.ok(contrast(buttonText,t[fill])>=4.5,`${fill} button text must meet 4.5:1`);
    assert.ok(contrast(t.accent,t.canvas)>=3,'focus outline must meet 3:1');
    assert.notEqual(t.error,t.accent,'failure semantics have their own color role');
  });
}
