/* tests/harness.js —— 极简 DOM 桩
   只提供被测函数真正用到的读写接口。故意不做成完整 DOM:
   桩越小,测试挂掉时越容易定位是页面代码的问题还是桩的问题。 */
var T = JSON.parse(read('__FIXTURE__'));

var VALS = {q:"", sortSel:"competition", dirSel:"", regSel:"", cmpA:"", cmpB:""};
var CHK  = {cAnch:false, cMeas:false};

function mkEl(id){
  return {id:id,
    tagName:(id in VALS)?"SELECT":"INPUT", type:"checkbox",
    get value(){return VALS[id]!==undefined?VALS[id]:"";}, set value(v){VALS[id]=v;},
    get checked(){return !!CHK[id];}, set checked(v){CHK[id]=v;},
    selectedIndex:0, href:"", download:"",
    innerHTML:"", textContent:"",
    style:{}, classList:{add:function(){}, remove:function(){}, toggle:function(){},
                         contains:function(){return false;}},
    insertAdjacentHTML:function(pos,h){ this.innerHTML += h; },
    getAttribute:function(k){return this["_"+k]||"";},
    setAttribute:function(k,v){this["_"+k]=v;},
    addEventListener:function(){}, querySelectorAll:function(){return [];},
    appendChild:function(){}, removeChild:function(){}, click:function(){},
    set onclick(f){}, get onclick(){return null;},
    set onchange(f){}, get onchange(){return null;},
    set oninput(f){}, get oninput(){return null;}};
}
var ELS = {};
var document = {
  getElementById:function(id){ if(!ELS[id])ELS[id]=mkEl(id); return ELS[id]; },
  querySelectorAll:function(){ return {forEach:function(){}}; },
  createElement:mkEl,
  body:{appendChild:function(){}, removeChild:function(){}},
  addEventListener:function(){}
};
var window = {};
var localStorage = {getItem:function(){return null;}, setItem:function(){}};
var URL = {createObjectURL:function(){return "blob:stub";}};
function Blob(parts){ this.parts=parts; }
var LAST_CSV = "";
