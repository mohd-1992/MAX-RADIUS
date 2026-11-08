const fs=require('node:fs');
const assert=require('node:assert/strict');
const source=fs.readFileSync('web/templates/system_services.html','utf8');
const line=source.split('\n').find(line=>line.includes('<p>')&&line.includes('data.data.radius_healthy'));
assert(line);
const render=new Function('data','return `'+line+'`;');
for(const [value,label,colour] of [[null,'الاستجابة غير متحققة','text-slate-400'],[undefined,'الاستجابة غير متحققة','text-slate-400'],[true,'يستجيب للطلبات','text-emerald-400'],[false,'غير مستجيب','text-rose-400']]) {
 const html=render({data:{radius_healthy:value}});
 assert(html.includes(label)&&html.includes(colour));
 assert.equal((html.match(/<p>/g)||[]).length,1);
 assert.equal((html.match(/<strong /g)||[]).length,1);
}
console.log('Unknown / verified healthy / verified failure message states PASS');
