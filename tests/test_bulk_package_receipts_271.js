const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
function extract(source, name) {
    const start = source.indexOf('    function ' + name + '(');
    assert(start >= 0);
    const brace = source.indexOf('{', start);
    let level = 0;
    for (let i = brace; i < source.length; i++) {
        if (source[i] === '{') level++;
        if (source[i] === '}' && --level === 0) return source.slice(start, i + 1);
    }
    throw Error('Unclosed function');
}
async function check(template) {
    const source = fs.readFileSync('web/templates/' + template, 'utf8');
    const nodes = Object.fromEntries(['cp_type','cp_id','cp_username','cp_pkg_id','modalChangePkg','formChangePkg'].map(id => [id, {value:'', innerText:'', dataset:{}, classList:{remove(){}}}]));
    const sent = [];
    let sequence = 0;
    class Data {
        constructor() { this.values = new Map([['target_type',nodes.cp_type.value],['target_ids',nodes.cp_id.value],['new_package_id',nodes.cp_pkg_id.value]]); }
        set(key,value) { this.values.set(key,value); }
    }
    const context = {document:{getElementById:id=>nodes[id]}, FormData:Data,
        closeAllActionDropdowns(){},closeModal(){},appendTargetIds(){},Swal:{showLoading(){}},
        window:{newSessionOperationId:()=>String(++sequence).padStart(32,'0'),createOperationNotification:()=>({fire(){},close(){},uncertain(){}})},
        fetch:async (url, options)=>{sent.push({url, body:new Map(options.body.values)});return {json:async()=>({success:false,message:'isolated deferred response'})};}};
    vm.createContext(context);
    vm.runInContext(extract(source,'openChangePackageModal')+'\n'+extract(source,'submitChangePackage'),context);
    context.openChangePackageModal('subscriber',['11','22'],'local',13);
    context.submitChangePackage({preventDefault(){}});
    context.submitChangePackage({preventDefault(){}});
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(sent.length,2);
    assert.match(sent[0].body.get('operation_id'),/^[a-f0-9]{32}$/);
    assert.equal(sent[0].body.get('operation_id'),sent[1].body.get('operation_id'));
    assert.equal(sent[0].body.get('target_ids'),'11,22');
    assert.equal(sent[0].body.get('new_package_id'),13);
    context.openChangePackageModal('subscriber',['11','22'],'local',13);
    context.submitChangePackage({preventDefault(){}});
    await new Promise(resolve=>setImmediate(resolve));
    assert.notEqual(sent[0].body.get('operation_id'),sent[2].body.get('operation_id'));
    console.log(template+' stable intent / new modal intent PASS');
}
(async()=>{await check('subscribers.html');await check('active_card_users.html');})().catch(error=>{console.error(error);process.exitCode=1;});
