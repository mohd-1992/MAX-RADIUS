(() => {
 'use strict';
 const form=document.getElementById('quickIssueForm');
 if(form){
  const pkg=form.elements.package_id, count=form.elements.count, owner=form.elements.reseller_id;
  const total=document.getElementById('quickTotal'), balance=document.getElementById('quickBalance'), after=document.getElementById('quickAfter'), message=document.getElementById('quickMessage');
  function preview(){
   const selected=pkg.selectedOptions[0], assigned=!owner||!!owner.value;
   const cost=Number(selected?.dataset.cost||0)*Number(count.value||0);
   const available=owner?Number(owner.selectedOptions[0]?.dataset.balance||0):Number(balance.dataset.balance||0);
   total.textContent=(assigned?cost:0).toFixed(2);balance.textContent=assigned?available.toFixed(2):'—';after.textContent=assigned?(available-cost).toFixed(2):'—';
  }
  form.addEventListener('input',preview);preview();let pending=false;
  form.addEventListener('submit',async event=>{
   event.preventDefault();if(pending||!form.reportValidity())return;
   const values=new FormData(form);pending=true;const button=form.querySelector('[type="submit"]');button.disabled=true;message.textContent='جارٍ الإصدار…';
   try{
    const response=await fetch(form.dataset.url,{method:'POST',body:values,headers:{'X-Requested-With':'XMLHttpRequest'}});
    const result=await response.json();message.textContent=result.message||'تعذر إكمال الإصدار.';
    if(response.ok&&result.success){
     if(form.dataset.details){location.assign(form.dataset.details.replace('/0','/'+result.issue_id));return;}
     // Replace the reference only after the server confirms a committed issuance.
     form.elements.request_key.value=Array.from(crypto.getRandomValues(new Uint8Array(24)),value=>value.toString(16).padStart(2,'0')).join('');
     if(result.balance_after!==null){balance.dataset.balance=result.balance_after;if(owner?.value)owner.selectedOptions[0].dataset.balance=result.balance_after;preview();}
     const link=document.createElement('a');link.href=location.href;link.textContent=' تحديث سجل الإصدار';message.append(link);
    }
   }catch(error){message.textContent='تعذر تأكيد النتيجة. أعد المحاولة بنفس الصفحة والمرجع لتجنب إصدار أو خصم مكرر.';}
   finally{pending=false;button.disabled=false;}
  });
 }
 document.querySelectorAll('[data-history-page]').forEach(link=>link.addEventListener('click',event=>{event.preventDefault();const url=new URL(location.href);url.searchParams.set('page',link.dataset.historyPage);location.assign(url.href);}));
 document.querySelectorAll('.copy-quick-card').forEach(button=>button.addEventListener('click',async()=>{
  const text='اسم المستخدم: '+button.dataset.user+'\nكلمة المرور: '+button.dataset.password;
  try{if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);}else{const input=document.createElement('textarea');input.value=text;input.style.position='fixed';input.style.opacity='0';document.body.append(input);input.select();const ok=document.execCommand('copy');input.remove();if(!ok)throw Error('copy');}document.getElementById('copyMessage').textContent='تم نسخ بيانات الكرت.';}
  catch(error){document.getElementById('copyMessage').textContent='تعذر النسخ تلقائيًا؛ يمكنك تحديد البيانات ونسخها.';}
 }));
 const policy=document.getElementById('issuePolicyForm');
 if(policy){function preview(){const data=new FormData(policy),prefix=String(data.get('prefix')||''),len=Number(data.get('total_length')),chars=data.get('char_type')==='numbers'?'23456789':'AB23456789',sample=prefix+chars.repeat(8).slice(0,Math.max(0,len-prefix.length));document.getElementById('issuePreview').textContent=sample;document.getElementById('issuePasswordField').hidden=data.get('format_type')!=='user_pass';}policy.addEventListener('input',preview);preview();}
})();
