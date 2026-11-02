const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
let light = false, onTheme;
const made=[];
const context={window:{},Intl,Number,Math,document:{documentElement:{classList:{contains:()=>light}}},
 MutationObserver:class {constructor(callback){onTheme=callback;}observe(){}},
 Chart:class {constructor(canvas,config){Object.assign(this,config);this.config=config;this.update=()=>this.updated=true;made.push(this);}}};
vm.runInNewContext(fs.readFileSync('web/static/report-charts.js','utf8'),context);
const api=context.window.MaxReportCharts, canvas=()=>({parentElement:{style:{}}});
const single=api.periodTraffic(canvas(),{labels:['2026-10-27'],download:[31],upload:[2.75]});
assert.equal(single.type,'line');assert.equal(single.options.locale,'en-US');assert.equal(single.data.datasets[0].tension,0.4);assert.equal(single.options.scales.x.offset,true);
assert.equal(single.data.datasets[0].data[0],31);assert.equal(single.data.datasets[1].data[0],2.75);
const multi=api.periodTraffic(canvas(),{labels:['a','b','c'],download:[1,null,3],upload:[0,1,2]});
assert.equal(multi.type,'line');assert.equal(multi.data.datasets[0].data[1],null);assert.equal(multi.data.datasets[1].data[0],0);assert.equal(multi.data.datasets[0].spanGaps,false);
const sales=api.sales(canvas(),{labels:['October'],sales:[5000]},'Sales');
assert.equal(sales.type,'bar');assert.equal(sales.data.datasets[0].data[0],5000);
const rows=[{nas_label:'Router',server_label:'Raw server',download:2147483648,upload:null},{nas_label:'Router',server_label:'Other server',download:1073741824,upload:0}];
const network=api.serverTraffic(canvas(),rows);
assert.equal(network.options.indexAxis,'y');assert.equal(network.data.datasets[0].data.reduce((a,b)=>a+b,0),3);assert.equal(network.data.datasets[1].data[0],null);assert.equal(network.data.datasets[1].data[1],0);
light=true;onTheme();assert.ok(made.every(chart=>chart.options.scales.x.ticks.color==='#475569'&&chart.options.scales.y.grid.color==='#e2e8f0'&&chart.updated));
api.periodTraffic(canvas(),{labels:[],download:[],upload:[]});
const invalid=api.sales(canvas(),{labels:['a','b','c'],sales:[null,-1,Infinity]},'Sales');assert.ok(invalid.data.datasets[0].data.every(v=>v===null));
console.log('PASS: single-point line; multi-period gaps; sales values; server totals/units/nulls; theme switch; empty/invalid values.');
