'use strict';

(() => {
  const root=document.documentElement,shell=document.querySelector('.app-shell');
  if(!shell)return;
  const storageKey='workbench.rail-widths';
  const rails=[
    {key:'navigation',property:'--navigation-width',handle:document.getElementById('navigation-resizer'),panel:document.getElementById('workspace-sidebar'),initial:232,min:200,max:360,direction:1},
    {key:'activity',property:'--activity-width',handle:document.getElementById('activity-resizer'),panel:document.getElementById('activity-sidebar'),initial:264,min:220,max:420,direction:-1},
  ].filter(rail=>rail.handle && rail.panel);
  const widths={};
  let active=null;

  function visible(rail){return rail.panel.parentElement===shell && rail.handle.getClientRects().length>0;}
  function limits(rail){
    const other=rails.find(item=>item!==rail),space=shell.clientWidth;
    const otherWidth=other && visible(other)?other.panel.getBoundingClientRect().width:0;
    const fraction=rail.key==='navigation'?(otherWidth ? .24 : .36):.30;
    return {min:rail.min,max:Math.max(rail.min,Math.floor(Math.min(rail.max,space*fraction,space-otherWidth-480)))};
  }
  function setWidth(rail,width){
    const {min,max}=limits(rail),next=Math.round(Math.min(max,Math.max(min,width)));
    widths[rail.key]=next;root.style.setProperty(rail.property,next+'px');
    updateHandles();
  }
  function updateHandles(){
    for(const rail of rails){
      if(!visible(rail))continue;
      const {min,max}=limits(rail);
      rail.handle.setAttribute('aria-valuemin',String(min));
      rail.handle.setAttribute('aria-valuemax',String(max));
      rail.handle.setAttribute('aria-valuenow',String(Math.round(rail.panel.getBoundingClientRect().width)));
    }
  }
  function save(){try{localStorage.setItem(storageKey,JSON.stringify(widths));}catch{}}
  function finish(event){
    if(!active || (event?.pointerId!==undefined && event.pointerId!==active.pointerId))return;
    const previous=active;active=null;root.classList.remove('is-resizing-rails');
    if(previous.rail.handle.hasPointerCapture(previous.pointerId))previous.rail.handle.releasePointerCapture(previous.pointerId);
    save();
  }
  try{
    const saved=JSON.parse(localStorage.getItem(storageKey));
    if(saved && typeof saved==='object')for(const rail of rails){
      const width=saved[rail.key];
      if(typeof width==='number' && Number.isFinite(width)){
        widths[rail.key]=Math.round(Math.min(rail.max,Math.max(rail.min,width)));
        root.style.setProperty(rail.property,widths[rail.key]+'px');
      }
    }
  }catch{}
  for(const rail of rails){
    rail.handle.addEventListener('pointerdown',event=>{
      if(event.button!==0 || active || !visible(rail))return;
      event.preventDefault();rail.handle.focus({preventScroll:true});
      active={rail,pointerId:event.pointerId,x:event.clientX,width:rail.panel.getBoundingClientRect().width};
      rail.handle.setPointerCapture(event.pointerId);root.classList.add('is-resizing-rails');
    });
    rail.handle.addEventListener('pointermove',event=>{
      if(!active || active.rail!==rail || event.pointerId!==active.pointerId)return;
      if(!visible(rail)){finish(event);return;}
      setWidth(rail,active.width+(event.clientX-active.x)*rail.direction);
    });
    rail.handle.addEventListener('pointerup',finish);
    rail.handle.addEventListener('pointercancel',finish);
    rail.handle.addEventListener('lostpointercapture',finish);
    rail.handle.addEventListener('keydown',event=>{
      if(!visible(rail) || !['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();const {min,max}=limits(rail),current=rail.panel.getBoundingClientRect().width;
      const next=event.key==='Home'?min:event.key==='End'?max:current+(event.key==='ArrowRight'?1:-1)*rail.direction*(event.shiftKey?32:8);
      setWidth(rail,next);save();
    });
    rail.handle.addEventListener('dblclick',()=>{if(visible(rail)){setWidth(rail,rail.initial);save();}});
  }
  window.addEventListener('resize',()=>{
    finish();
    updateHandles();
  });
  updateHandles();
})();
