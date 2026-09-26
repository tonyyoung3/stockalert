const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('web/static/index.html','utf8');
const source = html.slice(html.indexOf('let stockPriceRequest ='),html.indexOf('async function loadStock(){'));
const elements = Object.fromEntries(['stock-price-summary','c-stock-price','c-stock-volume'].map(id=>[id,{style:{},textContent:''}]));
let pending = [];
const context = vm.createContext({stockId:'2330', document:{getElementById:id=>elements[id]},
  charts:{}, ZOOM:{}, days:()=>90, j:url=>new Promise((resolve,reject)=>pending.push({url,resolve,reject})),
  mk:(id,cfg)=>{elements[id].chart=cfg;},parseStockId:s=>s});
vm.runInContext(source,context);
const result = id=>({id,name:'測試',data:[{date:'2026-09-24',open:100,high:103,low:99,close:102,volume:1500}],
  summary:{date:'2026-09-24',close:102,change:2,change_pct:2}});
(async()=>{
 const old = context.loadStockPrice(); context.stockId='2317'; const latest=context.loadStockPrice();
 pending[1].resolve(result('2317'));await latest;
 pending[0].resolve(result('2330'));await old;
 assert.match(elements['stock-price-summary'].textContent,/2317/);
 assert.equal(elements['c-stock-volume'].chart.data.datasets[0].data[0],1.5);
 let work=context.loadStockPrice();pending[2].resolve({data:[]});await work;
 assert.equal(elements['c-stock-price'].style.display,'none');
 assert.match(elements['stock-price-summary'].textContent,/尚無股價/);
 work=context.loadStockPrice();pending[3].reject(new Error('offline'));await work;
 assert.match(elements['stock-price-summary'].textContent,/載入失敗/);
 console.log('Stock price UI: race, volume units, empty and error states passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
