// Native Chromium CDP smoke test. Use a dedicated temporary browser profile.
// No npm dependencies and no access to the user's real browser/account data.
import assert from "node:assert/strict";
import {mkdir, writeFile} from "node:fs/promises";

const endpoint = process.env.RUNNEOS_CDP || "http://127.0.0.1:9225";
const url = process.env.RUNNEOS_PREVIEW || "http://127.0.0.1:8876/";
const output = process.env.RUNNEOS_SCREENSHOTS || "/tmp/opencode/runneos-design-screenshots";
for (const value of [endpoint, url]) assert.equal(new URL(value).hostname, "127.0.0.1", "Only local preview/test browser is allowed");
const target = await (await fetch(`${endpoint}/json/new?${encodeURIComponent("about:blank")}`, {method:"PUT"})).json();
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {socket.addEventListener("open",resolve,{once:true});socket.addEventListener("error",reject,{once:true});});
let id=0;const pending=new Map();const exceptions=[];const network=[];
socket.addEventListener("message", event => {
  const message=JSON.parse(event.data);
  if (message.id) {const request=pending.get(message.id);if(request){clearTimeout(request.timer);pending.delete(message.id);message.error?request.reject(new Error(JSON.stringify(message.error))):request.resolve(message.result);}}
  if(message.method==="Runtime.exceptionThrown")exceptions.push(message.params.exceptionDetails.text);
  if(message.method==="Network.requestWillBeSent")network.push(message.params.request.url);
});
function call(method,params={}) {return new Promise((resolve,reject)=>{const number=++id;const timer=setTimeout(()=>{pending.delete(number);reject(new Error(`CDP timeout: ${method}`));},15000);pending.set(number,{resolve,reject,timer});socket.send(JSON.stringify({id:number,method,params}));});}
async function evaluate(expression) {const result=await call("Runtime.evaluate",{expression,returnByValue:true,awaitPromise:true});if(result.exceptionDetails)throw new Error(JSON.stringify(result.exceptionDetails));return result.result.value;}
async function settle() {await evaluate("new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))");}
async function click(selector) {
  const rect=await evaluate(`(()=>{const el=document.querySelector(${JSON.stringify(selector)});el.scrollIntoView({block:'center'});const r=el.getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2};})()`);
  await call("Input.dispatchMouseEvent",{type:"mousePressed",...rect,button:"left",clickCount:1});
  await call("Input.dispatchMouseEvent",{type:"mouseReleased",...rect,button:"left",clickCount:1});await settle();
}
async function choose(selector,value) {await evaluate(`(()=>{const s=document.querySelector(${JSON.stringify(selector)});s.value=${JSON.stringify(value)};s.dispatchEvent(new Event('change',{bubbles:true}));})()`);await settle();}
async function screenshot(name) {await evaluate("scrollTo(0,0)");await settle();const {data}=await call("Page.captureScreenshot",{format:"png",captureBeyondViewport:false});await writeFile(`${output}/${name}.png`,Buffer.from(data,"base64"));}
let assertions=0;
try {
  await call("Page.enable");await call("Runtime.enable");await call("Network.enable");
  await call("Emulation.setDeviceMetricsOverride",{width:1440,height:1100,deviceScaleFactor:1,mobile:false});
  await call("Page.navigate",{url});
  const deadline=Date.now()+15000;
  while(!await evaluate("!!document.querySelector('.account-row')")){if(Date.now()>deadline)throw new Error("Preview did not render");await new Promise(resolve=>setTimeout(resolve,100));}
  await mkdir(output,{recursive:true});
  for(const mode of ["carbon","cloud"]){
    await choose("#theme",mode);await click("#home");await choose("#scenario","ready");
    await screenshot(`${mode}-desktop`);
    await click("#ai-toggle");assert.equal(await evaluate("document.querySelector('#ai-popup').open"),true);assertions++;
    await screenshot(`${mode}-ai-popout`);
    await call("Input.dispatchKeyEvent",{type:"keyDown",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});
    await call("Input.dispatchKeyEvent",{type:"keyUp",key:"Escape",code:"Escape",windowsVirtualKeyCode:27});await settle();
    assert.equal(await evaluate("document.querySelector('#ai-popup').open"),false);assertions++;
    await click('[data-view="accounts"]');
    for(const scenario of ["ready","stale","auth","low","empty","loading","unknown","zero"]){
      await choose("#scenario",scenario);
      const body=await evaluate("document.querySelector('main').innerText");
      const expected={ready:"62%",stale:"42 минуты",auth:"нужно войти",low:"8%",empty:"Подключите свой AI",loading:"Получаем первые данные",unknown:"Неизвестно",zero:"$0,00"};
      assert.ok(body.includes(expected[scenario]),`${mode}: ${scenario}`);assertions++;
    }
    await choose("#scenario","ready");await click('[data-details]');await screenshot(`${mode}-ai-details`);
    await click('[data-open-accounts]');await click('[data-account="deepseek"]');
    assert.ok((await evaluate("document.querySelector('main').innerText")).includes('Баланс API'));assertions++;
    for(const width of [375,640,1024,1440,1920]){
      await call("Emulation.setDeviceMetricsOverride",{width,height:1100,deviceScaleFactor:1,mobile:false});await settle();
      for(const view of ["overview","accounts","programs","settings"]){
        await click(`[data-view="${view}"]`);
        const sizes=await evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth})");
        assert.ok(sizes.scroll<=sizes.width,`${mode}/${width}/${view} overflow: ${JSON.stringify(sizes)}`);assertions++;
        const bounds=await evaluate("(()=>{const r=document.querySelector('main').getBoundingClientRect();return {left:r.left,right:r.right};})()");
        assert.equal(sizes.width,width,'Emulated viewport must actually match the requested width');assertions++;
        assert.ok(bounds.left>=0 && bounds.right<=width,`${mode}/${width}/${view} clipped content: ${JSON.stringify(bounds)}`);assertions++;
      }
      if(width===375){await click('[data-view="accounts"]');await screenshot(`${mode}-mobile`);}
    }
    await call("Emulation.setDeviceMetricsOverride",{width:1440,height:1100,deviceScaleFactor:1,mobile:false});await settle();
    await click('[data-view="accounts"]');await click('[data-connect]');
    assert.equal(await evaluate("document.querySelector('#connect-dialog').open"),true);assertions++;
    assert.equal(await evaluate("document.querySelectorAll('input[type=password],input[type=text]').length"),0);assertions++;
    await screenshot(`${mode}-connect`);await click("#connect-done");
  }
  assert.deepEqual(exceptions,[]);assertions++;
  assert.ok(network.every(request=>request.startsWith(url)||request==="about:blank"),"Unexpected external network request");assertions++;
  const result={ok:true,assertions,viewportWidths:[375,640,1024,1440,1920],modes:["carbon","cloud"],states:8,exceptions,output};
  await writeFile(`${output}/results.json`,JSON.stringify(result,null,2));console.log(JSON.stringify(result));
} finally {socket.close();}
