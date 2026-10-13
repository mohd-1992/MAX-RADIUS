(() => {
 let active=null;
 const visible = menu => [...menu.querySelectorAll('button,a')].filter(el=>getComputedStyle(el).display!=='none');
 const close = (focus=false) => {
  if(!active)return;
  const {menu,button,parent}=active;
  menu.classList.add('hidden');button.setAttribute('aria-expanded','false');parent.appendChild(menu);
  active=null;if(focus)button.focus();
 };
 document.querySelectorAll('.manager-menu-toggle').forEach(button=> {
  const menu=document.getElementById(button.getAttribute('aria-controls'));
  const refresh=()=>button.style.display=visible(menu).length?'':'none';
  // Hidden container does not change a child's computed display; responsive display still applies.
  refresh();window.addEventListener('resize',refresh);
  button.addEventListener('click',()=> {
   if(active?.button===button){close(true);return;}close();
   const parent=menu.parentNode;document.body.appendChild(menu);menu.classList.remove('hidden');
   button.setAttribute('aria-expanded','true');active={menu,button,parent};
   const box=button.getBoundingClientRect();const width=menu.offsetWidth,height=menu.offsetHeight;
   menu.style.left=Math.max(8,Math.min(innerWidth-width-8,box.right-width))+'px';
   menu.style.top=Math.max(8,box.bottom+height+8<=innerHeight?box.bottom+4:box.top-height-4)+'px';
   visible(menu)[0]?.focus();
  });
  menu.addEventListener('click',e=>{if(e.target.closest('button,a'))close();});
 });
 document.addEventListener('click',e=>{if(active&&!active.menu.contains(e.target)&&!active.button.contains(e.target))close();});
 document.addEventListener('keydown',e=> {
  if(!active)return;
  if(e.key==='Escape'){e.preventDefault();close(true);return;}
  if(e.key==='Tab'){close();return;}
  if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)) {
   e.preventDefault();const items=visible(active.menu);const i=items.indexOf(document.activeElement);
   const next=e.key==='Home'?0:e.key==='End'?items.length-1:(i+(e.key==='ArrowDown'?1:-1)+items.length)%items.length;
   items[next]?.focus();
  }
 });
 window.addEventListener('resize',()=>close());
 window.addEventListener('scroll',e=>{if(active&&!active.menu.contains(e.target))close();},true);
})();
