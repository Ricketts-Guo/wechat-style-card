"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const types = {text:"文字",file:"文件",sticker:"表情包",voice:"语音",image:"图片",video:"视频",link:"链接",other:"其他"};
  const forms = ["text","file","sticker","voice"];
  const formIcons = {text:"T",file:"▤",sticker:"☺",voice:"♫"};
  const emotions = {positive:"正面",neutral:"中性",negative:"负面",mixed:"混合",unknown:"无法判断"};
  const intents = {support:"鼓励与感谢",question:"提问",coordination:"协调安排",sharing:"分享",complaint:"抱怨",other:"其他或不明确"};
  const styles = {playful:"轻松幽默",polite:"礼貌",direct:"直接",unknown:"无法判断"};
  const dimensionNames={emotion:"情绪",intent:"表达意图",style:"文字风格"};
  const emotionColors = {positive:"#288664",neutral:"#8d9eac",negative:"#c45f4c",mixed:"#b79955",unknown:"#c8d0ce"};
  const state = {messages:[],analyses:[],summary:null,source:"",sourceKind:"",selectedPerson:null,settings:{configured:false},warnings:[],job:null,preview:[],previewId:null,previewSelected:new Set(),summaryRequest:0,datasetVersion:0,drawerLimit:100,drawerOrigin:null};
  let toastTimer, previewRequest=0, jobTimer;

  function node(tag, className, text) {
    const n=document.createElement(tag);
    if(className) n.className=className;
    if(text!==undefined) n.textContent=String(text);
    return n;
  }
  function number(value) { return (Number(value)||0).toLocaleString("zh-CN"); }
  function percent(value) { const v=Number(value)||0;return `${Math.round(v*10)/10}%`; }
  function timestamp(value) {
    if(value===undefined || value===null || value==="") return null;
    let d;
    if(typeof value==="number" || /^\d{10,13}$/.test(String(value))) {const v=Number(value);d=new Date(v<1e12?v*1000:v);}
    else d=new Date(value);
    return Number.isNaN(d.getTime())?null:d;
  }
  function formatTime(value) {
    const d=timestamp(value);
    return d?new Intl.DateTimeFormat("zh-CN",{timeZone:"Asia/Shanghai",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",hour12:false}).format(d):"时间未提供";
  }
  function filters() {return {chat_id:$("chat-filter").value,start:$("date-start").value,end:$("date-end").value};}
  function filteredMessages() {
    const f=filters(), start=f.start?new Date(`${f.start}T00:00:00+08:00`).getTime():null,end=f.end?new Date(`${f.end}T23:59:59.999+08:00`).getTime():null;
    return state.messages.filter(m=>{
      if(f.chat_id && String(m.chat_id)!==f.chat_id) return false;
      const d=timestamp(m.timestamp);
      if((start!==null || end!==null) && !d) return false;
      return !(start!==null && d.getTime()<start || end!==null && d.getTime()>end);
    });
  }
  async function api(path, body) {
    const opts=body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)};
    const response=await fetch(path,opts);
    let result;
    try {result=await response.json();} catch {throw new Error("本地服务返回了无法读取的结果，请检查服务是否正常运行。");}
    if(!response.ok) throw new Error(typeof result.error==="string"?result.error:result.error?.message||result.message||`请求失败（${response.status}）`);
    return result;
  }
  function toast(message) {
    $("toast").textContent=message;$("toast").hidden=false;
    clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$("toast").hidden=true;},4500);
  }
  function showError(element,message) {element.textContent=message;element.hidden=!message;}
  function hasDemoAnalyses() {return state.analyses.some(a=>["demo","demo_annotation","example","manual_demo"].includes(a.source));}
  function testModel(model) {return /test.double|fixture|mock/i.test(model||"");}
  function analysisSource(a) {
    if(!a)return "可人工标注";
    if(a.source==="manual")return `人工修正：${manualDimensions(a).map(d=>dimensionNames[d]).join("、")}`;
    if(["demo","demo_annotation","example","manual_demo"].includes(a.source))return "示例标注";
    if(testModel(a.model))return "测试替身响应";
    if(a.source==="import")return "导入标注";
    return "Jev 判断";
  }
  function reviewDimensions(a) {
    if(!a)return [];
    const keys=["emotion","intent","style"],raw=a.review_dimensions;
    if(Array.isArray(raw))return keys.filter(key=>raw.includes(key));
    if(raw&&typeof raw==="object")return keys.filter(key=>raw[key]===true);
    const selected=[];
    if(a.emotion==="unknown"||a.sentiment==="unknown"||a.needs_review===true)selected.push("emotion");
    [["emotion","confidence",a.emotion??a.sentiment],["intent","intent_confidence",a.intent],["style","style_confidence",a.style]].forEach(([key,field,label])=>{const v=a[field];if(label!=null&&typeof v==="number"&&Number.isFinite(v)&&v>=0&&v<.55&&!selected.includes(key))selected.push(key);});
    if(a.style==="unknown"&&!selected.includes("style"))selected.push("style");
    return selected;
  }
  function originalLabel(a, key, current) {return Object.prototype.hasOwnProperty.call(a,key)?a[key]:current;}
  function manualDimensions(a) {return Array.isArray(a?.manual_dimensions)?["emotion","intent","style"].filter(d=>a.manual_dimensions.includes(d)):a?.source==="manual"?["emotion"]:[];}
  function dimensionValue(a,d) {return d==="emotion"?a?.emotion??a?.sentiment??null:a?.[d]??null;}
  function initialReviewDimensions(a) {
    if(Array.isArray(a?.manual_original_review_dimensions))return ["emotion","intent","style"].filter(d=>a.manual_original_review_dimensions.includes(d));
    const original=reviewDimensions(a);
    manualDimensions(a).forEach(d=>{const before=a[`manual_original_${d}`]??(d==="emotion"?a.pre_manual_emotion:undefined)??a[`original_${d}`],score=a[d==="emotion"?"confidence":`${d}_confidence`];if((before==="unknown"||typeof score==="number"&&score>=0&&score<.55)&&!original.includes(d))original.push(d);});
    return ["emotion","intent","style"].filter(d=>original.includes(d));
  }
  async function correctDimension(messageId,d,value,reset=false) {
    const previous=state.analyses.find(a=>String(a.message_id)===String(messageId));
    const update={...previous,message_id:messageId},manual=new Set(manualDimensions(previous)),originalReview=initialReviewDimensions(previous);
    if(!Object.prototype.hasOwnProperty.call(update,"original_source"))update.original_source=previous?.source==="manual"?(previous?.model?"jev":"import"):previous?.source||(previous?"import":null);
    if(!Object.prototype.hasOwnProperty.call(update,"manual_original_review_dimensions"))update.manual_original_review_dimensions=originalReview;
    if(reset){
      if(!manual.has(d))return;
      const field=`manual_original_${d}`;value=Object.prototype.hasOwnProperty.call(update,field)?update[field]:d==="emotion"?update.pre_manual_emotion??update.original_emotion:update[`original_${d}`];manual.delete(d);
    } else {
      if(!value)return;
      if(!Object.prototype.hasOwnProperty.call(update,`manual_original_${d}`))update[`manual_original_${d}`]=dimensionValue(previous,d);
      if(d==="emotion"&&!Object.prototype.hasOwnProperty.call(update,"pre_manual_emotion"))update.pre_manual_emotion=dimensionValue(previous,d);
      manual.add(d);
    }
    if(d==="emotion"){
      if(value===undefined||value===null){delete update.emotion;delete update.sentiment;delete update.label;}
      else {update.emotion=value;update.sentiment=value;}
    } else {if(value===undefined||value===null)delete update[d];else update[d]=value;}
    update.manual_dimensions=["emotion","intent","style"].filter(key=>manual.has(key));update.review_dimensions=originalReview.filter(key=>!manual.has(key));update.needs_review=update.review_dimensions.length>0;update.source=manual.size?"manual":update.original_source;
    state.analyses=state.analyses.filter(a=>String(a.message_id)!==String(messageId));if(manual.size||update.original_source!==null)state.analyses.push(update);
    await refreshSummary();renderEvidence();[...$("evidence-list").querySelectorAll("select")].find(s=>s.dataset.messageId===String(messageId)&&s.dataset.dimension===d)?.focus();toast(reset?`已还原${dimensionNames[d]}的修正前统计值。`:`已修正${dimensionNames[d]}；其他维度保持当前判断。`);
  }
  function appendModelReview(item,a) {
    if(!a)return;
    const dimensions=reviewDimensions(a),manual=manualDimensions(a);
    if(dimensions.length)item.append(node("p","review-note",`建议复核：${dimensions.map(d=>dimensionNames[d]).join("、")}。`));
    if(manual.length){const remaining=["emotion","intent","style"].filter(d=>!manual.includes(d)&&dimensionValue(a,d)!==null);item.append(node("p","manual-note",`${manual.map(d=>dimensionNames[d]).join("、")}已人工修正${remaining.length?`；${remaining.map(d=>dimensionNames[d]).join("、")}${testModel(a.model)?"仍是测试替身响应":"仍为模型判断"}`:"；原始标签保留供对照"}。`));}
    const configs=[["emotion","情绪",emotions,"original_emotion",a.emotion??a.sentiment,"confidence","probabilities"],["intent","表达意图",intents,"original_intent",a.intent,"intent_confidence","intent_probabilities"],["style","文字风格",styles,"original_style",a.style,"style_confidence","style_probabilities"]];
    const originals=node("div","original-labels");let originalCount=0;
    configs.forEach(([key,title,labels,field,current,confidenceField])=>{
      const hasOriginal=Object.prototype.hasOwnProperty.call(a,field),original=hasOriginal?a[field]:Object.prototype.hasOwnProperty.call(a,`manual_original_${key}`)?a[`manual_original_${key}`]:current;if(original===undefined||original===null)return;
      const different=original!==current,score=a[confidenceField],low=typeof score==="number"&&Number.isFinite(score)&&score>=0&&score<.55;
      const suffix=different&&!manual.includes(key)&&low?`；区分度较低，当前统计为${labels[current]||"无法判断"}`:different?`；当前统计为${labels[current]||"未分析"}`:"";
      originals.append(node("p","manual-note",`${hasOriginal?"原始":"既有"}${title}标签：${labels[original]||String(original)}${suffix}`));originalCount++;
    });
    if(originalCount)item.append(originals);
    const available=configs.filter(([, , , , ,confidenceField,probabilityField])=>typeof a[confidenceField]==="number"||a[probabilityField]&&Object.keys(a[probabilityField]).length);
    if(available.length){
      const detail=node("details","model-distribution");detail.append(node("summary","","查看模型选项分布（仅作参考）"),node("p","helper","这些数值反映模型在给定选项间的倾向；区分度不是准确率，也不是判断正确的概率。人工修正不会改变原始模型分布。"));
      available.forEach(([,title,labels,,,confidenceField,probabilityField])=>{
        const row=node("div","distribution-row"),score=a[confidenceField],valid=typeof score==="number"&&Number.isFinite(score)&&score>=0&&score<=1;row.append(node("strong","",title+(valid?` · 区分度 ${score.toFixed(2)}`:"")));
        const raw=a[probabilityField];if(raw&&typeof raw==="object")Object.entries(raw).filter(([,v])=>typeof v==="number"&&Number.isFinite(v)&&v>=0&&v<=1).sort((a,b)=>b[1]-a[1]).forEach(([key,value])=>row.append(node("span","",`${labels[key]||key} ${percent(value*100)}`)));
        detail.append(row);
      });item.append(detail);
    }
  }
  function showDialog(id) {const dialog=$(id);if(!dialog.open) dialog.showModal();}
  function closeDialog(id) {$(id).close();}
  function setNotices(extra=[]) {
    $("notices").replaceChildren();
    if(state.sourceKind==="demo") {
      const n=node("div","notice demo",hasDemoAnalyses()?"正在体验虚构聊天。当前含情绪、意图与风格的预设示例标注；这些标注未经 Jev 调用，可用自己的密钥重新分析。":"当前聊天为虚构示例。判断来源以原文复核页的标识为准。");
      $("notices").append(n);
    }
    if(testModel(state.settings.model))$("notices").append(node("div","notice demo","当前配置使用测试替身，仅用于验证功能流程；这里的模型结果不代表真实 Jev 判断。"));
    else if(state.analyses.some(a=>testModel(a.model)))$("notices").append(node("div","notice demo","当前结果含测试替身响应，仅用于验证功能流程，不代表真实 Jev 判断。实际返回模型可在本轮分析信息和原文复核页查看。"));
    [...new Set([...state.warnings,...(state.summary?.warnings||[]),...extra].map(w=>typeof w==="string"?w:JSON.stringify(w)))].filter(w=>!(state.sourceKind==="demo"&&/(虚构|示例|演示)/.test(w)&&/(情绪|标注|Jev)/.test(w))).forEach(w=>{
      const n=node("div","notice",w),x=node("button","", "×");x.setAttribute("aria-label","关闭这条提示");x.addEventListener("click",()=>n.remove());n.append(x);$("notices").append(n);
    });
  }
  async function busy(button, action) {
    const old=button.textContent;button.disabled=true;button.textContent="处理中…";
    try{return await action();} finally {button.textContent=old;button.disabled=false;}
  }

  async function loadDataset(data, source, kind="import") {
    state.datasetVersion++;clearTimeout(jobTimer);state.job=null;$("job-progress").hidden=true;
    const dataset=data.dataset||data;
    if(!Array.isArray(dataset.messages)) throw new Error("没有找到有效的 messages 消息列表。");
    state.messages=dataset.messages;
    state.analyses=(dataset.analyses||data.analyses||[]).map(a=>({...a,emotion:a.emotion||a.sentiment,source:a.source||(kind==="demo"?"demo_annotation":"import")}));
    state.source=source;state.sourceKind=kind;state.warnings=dataset.warnings||[];state.selectedPerson=null;
    $("source-name").textContent=source;$("source-count").textContent=`${number(state.messages.length)} 条消息 · 已载入本机`;
    $("dataset-source").hidden=false;
    $("chat-filter").replaceChildren(new Option("全部会话",""));
    $("date-start").value="";$("date-end").value="";
    const chats=new Map();state.messages.forEach(m=>{if(m.chat_id!==undefined) chats.set(String(m.chat_id),m.chat_name||String(m.chat_id));});
    for(const [id,name] of chats) $("chat-filter").add(new Option(name,id));
    await refreshSummary();
  }
  async function refreshSummary() {
    const request=++state.summaryRequest;
    const f=filters();
    if(f.start && f.end && f.start>f.end) {toast("开始日期不能晚于结束日期。");return;}
    try {
      const result=await api("/api/summary",{messages:state.messages,analyses:state.analyses,filters:f});
      if(request!==state.summaryRequest) return;
      state.summary=result.summary||result;renderSummary();
    } catch(error) {if(request===state.summaryRequest) setNotices([error.message]);}
  }
  function renderSummary() {
    const summary=state.summary,people=summary.people||[];
    const analyzed=people.reduce((sum,p)=>sum+(Number(p.emotion?.analyzed)||0),0),totalText=people.reduce((sum,p)=>sum+(Number(p.emotion?.total_text)||0),0);
    $("stat-messages").textContent=number(summary.total_messages);
    $("stat-people").textContent=number(summary.total_people??people.length);
    $("stat-coverage").textContent=percent(totalText?analyzed/totalText*100:0);
    $("coverage-note").textContent=`${number(analyzed)} / ${number(totalText)} 条文字${hasDemoAnalyses()?" · 含示例标注":""}`;
    const f=filters();$("stat-range").textContent=f.start||f.end?`${f.start||"最早"} → ${f.end||"最新"}`:state.messages.length?"当前全部记录":"等待导入";
    $("empty-state").hidden=state.messages.length>0;
    $("results").hidden=!state.messages.length;
    $("export-report").disabled=!people.length;
    $("people-count").textContent=`${number(people.length)} 人`;
    $("people-list").replaceChildren();
    if(!people.some(p=>String(p.id)===String(state.selectedPerson))) state.selectedPerson=people[0]?.id??null;
    people.forEach((p,index)=>{
      const button=node("button",`person-choice${String(p.id)===String(state.selectedPerson)?" active":""}`);
      button.setAttribute("aria-pressed",String(String(p.id)===String(state.selectedPerson)));
      button.append(node("span",`avatar alt${index%4}`,String(p.name||"未知").slice(0,1)));
      const label=node("span");label.append(node("strong","",p.name||"未知参与者"),node("small","",`${number(p.total)} 条消息`));button.append(label);
      button.addEventListener("click",()=>{state.selectedPerson=p.id;renderSummary();});$("people-list").append(button);
    });
    renderPerson();setNotices();
    $("analyze-open").disabled=Boolean(state.job)||!filteredMessages().some(m=>m.type==="text"&&m.text?.trim());
    if(state.messages.length && !people.length) $("person-card").replaceChildren(node("div","preview-empty","当前筛选范围内没有消息。请调整会话或日期。"));
  }
  function renderPerson() {
    const people=state.summary?.people||[],index=people.findIndex(p=>String(p.id)===String(state.selectedPerson)),p=people[index];
    if(!p) return;
    const personMessages=new Set(filteredMessages().filter(m=>String(m.sender_id)===String(p.id)).map(m=>String(m.id))),personDemo=state.analyses.some(a=>personMessages.has(String(a.message_id))&&["demo","demo_annotation","example","manual_demo"].includes(a.source));
    const card=$("person-card");card.replaceChildren();
    const header=node("div","person-card-header"),identity=node("div","person-identity"),name=node("div");
    identity.append(node("span","avatar",String(p.name||"未知").slice(0,1)));name.append(node("h3","",p.name||"未知参与者"),node("p","",`${number(p.total)} 条消息 · 文字中位长度 ${number(p.text_stats?.median_length)} 字`));identity.append(name);
    header.append(identity,node("span","card-number",`CHATPRINT / ${String(index+1).padStart(2,"0")}`));card.append(header);
    const body=node("div","card-body"),title=node("div","card-section-title");title.append(node("h4","","表达形式偏好"),node("span","","仅在下列四类消息中计算占比"));body.append(title);
    const grid=node("div","form-grid"),formTotal=p.form_total??forms.reduce((sum,type)=>sum+(Number(p.types?.[type])||0),0);
    forms.forEach(type=>{
      const count=Number(p.types?.[type])||0,share=p.form_percentages?.[type]??(formTotal?count/formTotal*100:0);
      const item=node("div",`form-item${p.dominant_form===type?" dominant":""}`),top=node("div","form-top"),value=node("strong","",String(Math.round(Number(share)*10)/10));
      top.append(node("span","form-icon",formIcons[type]),node("span","form-label",types[type]));value.append(node("small","","%"));
      item.append(top,value,node("small","",`${number(count)} 条消息`));const bar=node("div","mini-bar"),fill=node("span");fill.style.width=`${Math.min(100,Math.max(0,Number(share)||0))}%`;bar.append(fill);item.append(bar);grid.append(item);
    });body.append(grid);
    const other=["image","video","link","other"].map(t=>`${types[t]} ${number(p.types?.[t])} 条`).join(" · ");body.append(node("p","other-types",`${formTotal?"四类合计 "+number(formTotal)+" 条。":"没有这四类消息。"}其他形式另列：${other}`));
    const tags=node("section","tags-section"),tagTitle=node("div","card-section-title");tagTitle.append(node("h4","","这段聊天里的 TA"),node("span","","悬停标签查看依据"));tags.append(tagTitle);
    const list=node("div","tag-list");(p.tags||[]).forEach(tag=>{const t=node("button","tag",typeof tag==="string"?tag:tag.label);t.title=tag.reason||"根据当前消息统计";t.addEventListener("click",()=>toast(t.title));list.append(t);});
    if(!(p.tags||[]).length) list.append(node("span","helper","样本较少，暂不生成风格标签。"));
    tags.append(list,node("p","tag-caption","描述样本中的表达习惯，标签随聊天范围变化。"));body.append(tags);
    const expression=node("section","expression-section"),exTitle=node("div","card-section-title");exTitle.append(node("h4","","表达意图与文字风格"),node("span","",personDemo?"含示例标注 · 分别统计覆盖":"每个维度单独统计覆盖"));expression.append(exTitle);
    const ex=p.expression||{};
    if(!(Number(ex.intent_analyzed)||Number(ex.style_analyzed))) expression.append(node("div","emotion-empty","这两个维度尚未分析。用 Jev 分析文字后，可看到鼓励、提问、分享等表达意图，以及轻松、礼貌、直接等文字风格。"));
    else {
      const exGrid=node("div","expression-grid");
      [["intent","表达意图",intents],["style","文字风格",styles]].forEach(([dimension,label,labels])=>{
        const block=node("div","expression-block"),n=Number(ex[`${dimension}_analyzed`])||0;block.append(node("strong","expression-title",label),node("small","expression-coverage",`已分析 ${number(n)} / ${number(p.emotion?.total_text)} 条文字`));
        if(!n)block.append(node("p","helper","该维度暂无结果。"));
        else Object.entries(labels).sort((a,b)=>(Number(ex[`${dimension}_counts`]?.[b[0]])||0)-(Number(ex[`${dimension}_counts`]?.[a[0]])||0)).forEach(([key,name])=>{
          const count=Number(ex[`${dimension}_counts`]?.[key])||0,share=ex[`${dimension}_percentages`]?.[key]??count/n*100,row=node("div","semantic-row"),heading=node("div"),bar=node("div","mini-bar"),fill=node("span");heading.append(node("span","",name),node("small","",`${number(count)} 条 · ${percent(share)}`));fill.style.width=`${Math.min(100,Math.max(0,Number(share)||0))}%`;bar.append(fill);row.append(heading,bar);block.append(row);
        });
        if(n>0&&n<10)block.append(node("p","semantic-note","少于 10 条结果，暂不生成此维度的风格标签。"));
        exGrid.append(block);
      });expression.append(exGrid,node("p","tag-caption","每项占比以该维度已分析结果为分母；“其他或不明确”和“无法判断”单独保留。"));
    }body.append(expression);
    const emotion=node("section","emotion-section"),emotionHeading=node("div","emotion-heading"),evidenceButton=node("button","text-button","查看原文与修正 ↗");evidenceButton.addEventListener("click",()=>openEvidence(p));
    emotionHeading.append(node("h4","","文字里的情绪"),evidenceButton);emotion.append(emotionHeading);
    const ec=p.emotion||{},analyzed=Number(ec.analyzed)||0,total=Number(ec.total_text)||0;
    const cov=node("p","emotion-coverage",`${personDemo?"含示例标注 · ":""}已分析 ${number(analyzed)} / ${number(total)} 条文字 · 覆盖 ${percent(total?analyzed/total*100:0)}`);emotion.append(cov);
    if(analyzed) {
      const track=node("div","emotion-track"),legend=node("div","emotion-legend");
      Object.entries(emotions).forEach(([key,label])=>{
        const count=Number(ec.counts?.[key])||0,ratio=analyzed?count/analyzed*100:0,segment=node("span");segment.style.width=`${ratio}%`;segment.style.background=emotionColors[key];segment.title=`${label}：${number(count)} 条`;track.append(segment);
        const entry=node("span"),dot=node("i"),val=node("strong","",`${Math.round(ratio)}%`);dot.style.background=emotionColors[key];entry.append(dot,document.createTextNode(label+" "),val);entry.title=`${number(count)} 条`;legend.append(entry);
      });emotion.append(track,legend);
      emotion.append(node("p","tag-caption","占比以已分析文字为分母；未分析的消息不计入情绪分布。"));
    } else emotion.append(node("div","emotion-empty",total?"还没有分析这段文字。可在下方选择消息，预览后用 Jev 分析；也可以先查看原文。":"当前范围没有可分析的文字消息。语音、表情包与文件保留为形式统计。"));
    body.append(emotion);card.append(body);
  }

  async function importFile(file) {
    if(!file) return;
    if(state.job){toast("当前有分析任务进行中，请等本轮完成后再更换数据。");return;}
    if(file.size>10*1024*1024) {toast("文件大于 10 MB，请按会话或时间拆分后导入。");return;}
    $("drop-zone").classList.add("loading");
    try {const result=await api("/api/import",{content:await file.text(),filename:file.name});await loadDataset(result,file.name);toast(`已载入 ${number(state.messages.length)} 条消息。`);}
    catch(error){setNotices([error.message]);toast("导入未完成，请检查文件格式。");}
    finally{$("drop-zone").classList.remove("loading");$("file-input").value="";}
  }
  async function loadDemo(button) {
    if(state.job){toast("当前有分析任务进行中，请等本轮完成后再更换数据。");return;}
    await busy(button,async()=>{try {await loadDataset(await api("/api/demo"),"示例 · 四种聊天节奏","demo");toast("示例已载入，情绪为预设标注。");}catch(error){setNotices([error.message]);}});
  }

  async function loadSettings() {
    try {state.settings=await api("/api/settings");renderSettings();}
    catch(error){$("settings-connection").textContent="无法连接本地服务";$("key-status").textContent="服务未连接";showError($("settings-error"),error.message);}
  }
  function renderSettings() {
    const configured=Boolean(state.settings.configured);
    $("key-status").textContent=configured?"已连接":"未连接";$("key-status").classList.toggle("connected",configured);
    $("settings-connection").textContent=configured?"密钥已保存在本次服务进程":"尚未设置 API Key";
    $("settings-dot").style.background=configured?"var(--green)":"#aeb8b0";$("settings-model").textContent=state.settings.model||"Jev";
    $("key-clear").hidden=!configured;$("api-key").placeholder=configured?"输入新密钥以替换当前密钥":"粘贴你的 API Key";
  }
  async function saveSettings(event) {
    event.preventDefault();const key=$("api-key").value.trim();showError($("settings-error"),"");
    if(!key){showError($("settings-error"),"请填写 API Key。");return;}
    await busy($("settings-save"),async()=>{
      try {await api("/api/settings",{api_key:key});$("api-key").value="";await loadSettings();closeDialog("settings-dialog");toast("密钥已保存。现在可以选择文本进行分析。");}
      catch(error){showError($("settings-error"),error.message);}
    });
  }
  async function openAnalysis() {
    showError($("analysis-error"),"");
    if(!state.settings.configured){showDialog("settings-dialog");toast("先设置你自己的 TypeSafe API Key，再分析文字。");$("api-key").focus();return;}
    showDialog("analysis-dialog");await loadPreview();
  }
  async function loadPreview() {
    const request=++previewRequest;
    $("analysis-send").disabled=true;$("preview-messages").replaceChildren(node("div","preview-empty","正在准备脱敏预览…"));showError($("analysis-error"),"");
    const all=filteredMessages().filter(m=>m.type==="text"&&String(m.text||"").trim()),ids=new Set(state.analyses.map(a=>String(a.message_id)));
    const excluded=$("skip-analyzed").checked?[...ids]:[],candidates=$("skip-analyzed").checked?all.filter(m=>!ids.has(String(m.id))):all;
    const limit=Math.min(200,Math.max(1,Number($("analysis-limit").value)||30));
    try {
      const result=await api("/api/preview",{messages:filteredMessages(),exclude_ids:excluded,limit,anonymize:true});
      if(request!==previewRequest) return;
      state.previewId=result.preview_id||null;state.preview=(result.messages||[]).slice(0,limit);state.previewSelected=new Set(state.preview.map(m=>String(m.id??m.message_id)));
      $("preview-warnings").textContent=(result.warnings||[]).map(w=>typeof w==="string"?w:JSON.stringify(w)).join("；");
      renderPreview();
      if(!state.preview.length) $("preview-messages").replaceChildren(node("div","preview-empty",candidates.length?"暂无可预览的文本。":"没有待分析文字。可扩大筛选范围，或取消“跳过已分析”。"));
    } catch(error) {if(request===previewRequest){state.preview=[];state.previewId=null;state.previewSelected.clear();renderPreview();showError($("analysis-error"),error.message);}}
  }
  function renderPreview() {
    const container=$("preview-messages");container.replaceChildren();
    state.preview.forEach((m,index)=>{
      const id=String(m.id??m.message_id),item=node("label","preview-item"),checkbox=document.createElement("input");checkbox.type="checkbox";checkbox.checked=state.previewSelected.has(id);checkbox.setAttribute("aria-label",`选择第 ${index+1} 条文字`);
      checkbox.addEventListener("change",()=>{checkbox.checked?state.previewSelected.add(id):state.previewSelected.delete(id);updatePreviewCount();});
      const text=node("div"),original=state.messages.find(msg=>String(msg.id)===id);
      text.append(node("div","meta",`文字 ${String(index+1).padStart(2,"0")} · ${formatTime(original?.timestamp)}`),node("p","",m.text||""));
      if(Array.isArray(m.context)&&m.context.length){const context=node("details","preview-context");context.append(node("summary","",`随附 ${m.context.length} 条上下文`));m.context.forEach(c=>context.append(node("p","",`${c.sender||"参与者"}：${c.text||""}`)));text.append(context);}
      item.append(checkbox,text);container.append(item);
    });updatePreviewCount();
  }
  function updatePreviewCount() {
    const count=state.previewSelected.size;$("preview-count").textContent=`已选 ${number(count)} / ${number(state.preview.length)} 条`;
    $("preview-all").checked=count>0&&count===state.preview.length;$("preview-all").indeterminate=count>0&&count<state.preview.length;
    $("analysis-send").disabled=!count||Boolean(state.job);
    $("analysis-cost").textContent=count?`${number(count)} 条目标文本及上下文 · 费用由你的账户承担`:"请选择至少一条文本";
  }
  async function startAnalysis() {
    const selected=state.previewSelected;const messages=filteredMessages().filter(m=>selected.has(String(m.id)));
    if(!messages.length) return;
    if(!state.previewId){showError($("analysis-error"),"预览未生成有效的发送快照，请重新选择分析数量以刷新预览。");return;}
    const version=state.datasetVersion;
    await busy($("analysis-send"),async()=>{
      try {
        const result=await api("/api/analyze",{preview_id:state.previewId,selected_ids:[...selected]});
        if(version!==state.datasetVersion) return;
        if(!result.job_id) throw new Error("本地服务没有返回分析任务编号。");
        state.job={id:result.job_id,version,failures:0};closeDialog("analysis-dialog");$("job-progress").hidden=false;$("analyze-open").disabled=true;$("job-retry").hidden=true;
        $("job-label").textContent="Jev 正在阅读这些文字";$("job-count").textContent=`0 / ${number(messages.length)}`;$("job-bar").value=0;$("job-detail").textContent="完成后可查看原文，并修正判断。";await pollJob();
      } catch(error){showError($("analysis-error"),error.message);}
    });
  }
  function mergeAnalyses(analyses) {
    const map=new Map(state.analyses.map(a=>[String(a.message_id),a]));
    analyses.forEach(a=>{
      const id=String(a.message_id),old=map.get(id),incoming={...a,emotion:a.emotion||a.sentiment,source:a.source||"jev"},manual=manualDimensions(old);
      if(!manual.length){map.set(id,incoming);return;}
      const merged={...incoming,source:"manual",original_source:incoming.source,manual_dimensions:manual};
      manual.forEach(d=>{
        const fields=d==="emotion"?["emotion","sentiment","original_emotion","confidence","probabilities","manual_original_emotion","pre_manual_emotion"]:[d,`original_${d}`,`${d}_confidence`,`${d}_probabilities`,`manual_original_${d}`,`pre_manual_${d}`];
        fields.forEach(field=>{if(Object.prototype.hasOwnProperty.call(old,field))merged[field]=old[field];else delete merged[field];});
      });
      const freshReview=reviewDimensions(incoming),oldReview=initialReviewDimensions(old),baseline=["emotion","intent","style"].filter(d=>manual.includes(d)?oldReview.includes(d):freshReview.includes(d));
      merged.manual_original_review_dimensions=baseline;merged.review_dimensions=baseline.filter(d=>!manual.includes(d));merged.needs_review=merged.review_dimensions.length>0;map.set(id,merged);
    });state.analyses=[...map.values()];
  }
  async function pollJob() {
    const job=state.job;if(!job||job.version!==state.datasetVersion)return;
    try {
      const result=await api(`/api/jobs/${encodeURIComponent(job.id)}`);
      if(state.job!==job) return;
      job.failures=0;$("job-retry").hidden=true;$("job-label").textContent="Jev 正在阅读这些文字";
      const completed=Number(result.completed)||0,total=Number(result.total)||0;$("job-count").textContent=`${number(completed)} / ${number(total)}`;$("job-bar").value=total?completed/total*100:0;
      if(result.analyses?.length){mergeAnalyses(result.analyses);await refreshSummary();}
      const usage=result.usage||{},models=[...new Set((Array.isArray(result.models)?result.models:[]).concat((result.analyses||[]).map(a=>a.model).filter(Boolean)))],modelInfo=models.length?`返回模型：${models.join("、")}${models.some(testModel)?"（测试替身，非真实 Jev）":""} · `:"";
      $("job-detail").textContent=`${modelInfo}输入 ${number(usage.input_tokens)} tokens · 输出 ${number(usage.output_tokens)} tokens${result.errors?.length?` · ${number(result.errors.length)} 条未成功`:""}`;
      if(["completed","complete","done","failed","error","cancelled"].includes(result.status)) {
        state.job=null;$("analyze-open").disabled=false;
        const fail=["failed","error"].includes(result.status);$("job-label").textContent=fail?"本轮分析未完成":"本轮分析结束";
        if(result.errors?.length) setNotices(result.errors.map(e=>typeof e==="string"?e:e.error||e.message||JSON.stringify(e)));
        toast(fail?"分析遇到问题，请查看提示并检查 API 设置。":"分析结束。点击“查看原文与修正”复核结果。");
      } else jobTimer=setTimeout(pollJob,900);
    } catch(error){if(state.job!==job)return;job.failures++;$("job-label").textContent="暂时无法读取进度";$("job-detail").textContent=job.failures<=3?`${error.message} · 正在重试（${job.failures}/3）`:`${error.message} · 任务可能仍在本机运行，可重新读取进度。`;if(job.failures<=3)jobTimer=setTimeout(pollJob,job.failures*1500);else $("job-retry").hidden=false;}
  }

  function openEvidence(person) {
    state.drawerOrigin=document.activeElement;state.drawerLimit=100;$("evidence-title").textContent=`${person.name||"参与者"} · 原文与复核`;$("evidence-drawer").dataset.personId=String(person.id);renderEvidence();
    $("drawer-backdrop").hidden=false;$("evidence-drawer").hidden=false;document.body.style.overflow="hidden";$("drawer-close").focus();
  }
  function closeEvidence() {$("drawer-backdrop").hidden=true;$("evidence-drawer").hidden=true;document.body.style.overflow="";state.drawerOrigin?.focus();}
  function renderEvidence() {
    const id=$("evidence-drawer").dataset.personId,messages=filteredMessages().filter(m=>String(m.sender_id)===id),analyses=new Map(state.analyses.map(a=>[String(a.message_id),a]));
    $("evidence-description").textContent=`当前范围共 ${number(messages.length)} 条消息。三个维度可独立修正；“还原”恢复该维修正前的统计值和复核标记。修正只保留在本次页面中。`;
    const list=$("evidence-list");list.replaceChildren();
    messages.slice(0,state.drawerLimit).forEach((m,index)=>{
      const item=node("article","evidence-item"),meta=node("div","evidence-meta");meta.append(node("span","",formatTime(m.timestamp)),node("span","message-type",types[m.type]||"其他"));item.append(meta,node("p","",m.text||`[${types[m.type]||"其他"}消息 · 仅统计形式]`));
      if(m.type==="text") {
        const a=analyses.get(String(m.id)),footer=node("footer"),manual=manualDimensions(a);footer.append(node("span","",analysisSource(a)));item.append(footer);
        const controls=node("div","correction-controls");
        [["emotion",emotions],["intent",intents],["style",styles]].forEach(([d,labels])=>{
          const row=node("div","correction-row"),label=node("label","",dimensionNames[d]),select=document.createElement("select"),current=dimensionValue(a,d);select.id=`correction-${index}-${d}`;label.htmlFor=select.id;select.setAttribute("aria-label",`修正此消息的${dimensionNames[d]}`);select.dataset.messageId=String(m.id);select.dataset.dimension=d;
          if(current===null)select.add(new Option("未分析",""));Object.entries(labels).forEach(([value,title])=>select.add(new Option(title,value)));select.value=current||"";
          const reset=node("button","text-button correction-reset","还原");reset.disabled=!manual.includes(d);reset.title="还原该维修正前的统计值";reset.dataset.dimension=d;reset.dataset.messageId=String(m.id);reset.setAttribute("aria-label",`还原此消息的${dimensionNames[d]}判断`);
          const source=manual.includes(d)?"人工":!a||current===null?"未分析":["demo","demo_annotation","example","manual_demo"].includes(a.original_source||a.source)?"示例":testModel(a.model)?"测试替身":a.source==="import"||a.original_source==="import"?"导入":"模型";
          row.append(label,select,node("small","dimension-source",source),reset);select.addEventListener("change",()=>correctDimension(m.id,d,select.value));reset.addEventListener("click",()=>correctDimension(m.id,d,null,true));controls.append(row);
        });item.append(controls);
        if(manual.length){const previousText=manual.map(d=>{const labels=d==="emotion"?emotions:d==="intent"?intents:styles,value=Object.prototype.hasOwnProperty.call(a,`manual_original_${d}`)?a[`manual_original_${d}`]:d==="emotion"?a.pre_manual_emotion??a.original_emotion:a[`original_${d}`];return `${dimensionNames[d]}修正前：${labels[value]||"未分析"}`;}).join("；");item.append(node("p","manual-note",previousText));}
        else if(a?.reason)item.append(node("p","manual-note",a.reason));appendModelReview(item,a);
      }list.append(item);
    });
    if(messages.length>state.drawerLimit){const more=node("button","button full secondary",`再显示 100 条（还有 ${number(messages.length-state.drawerLimit)} 条）`);more.addEventListener("click",()=>{state.drawerLimit+=100;renderEvidence();});list.append(more);}
    if(!messages.length)list.append(node("div","preview-empty","当前范围没有消息。"));
  }

  async function exportReport() {
    await busy($("export-report"),async()=>{
      try {const messages=filteredMessages(),ids=new Set(messages.map(m=>String(m.id)));const result=await api("/api/report",{messages,analyses:state.analyses.filter(a=>ids.has(String(a.message_id))),anonymize:true});
        if(typeof result.markdown!=="string")throw new Error("本地服务没有返回有效报告。");
        const blob=new Blob([result.markdown],{type:"text/markdown;charset=utf-8"}),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=`chatprint-${new Date().toLocaleDateString("sv-SE")}.md`;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast("已导出匿名报告。姓名已隐去，请检查文字中仍可能出现的个人信息。");
      }catch(error){setNotices([error.message]);}
    });
  }
  async function wechatStatus() {
    try {const result=await api("/api/wechat/status");$("wechat-status").textContent=result.available?(result.initialized===false?"已检测到读取工具，尚未初始化。请先在终端完成工具初始化。":!result.command&&result.python_adapter_available?"已检测到 Python 读取适配器。可手动输入聊天名或稳定 ID。":"已检测到本机微信 CLI。可选择会话读取。"):"未检测到 CLI，仍可导入聊天文件。";$("wechat-connect").disabled=!result.available||result.initialized===false;}
    catch{$("wechat-status").textContent="未能检测 CLI，仍可导入聊天文件。";}
  }
  async function connectWechat() {
    await busy($("wechat-connect"),async()=>{try {
      const result=await api("/api/wechat/sessions");let sessions=result.sessions||result.data?.sessions||result.data||[];if(!Array.isArray(sessions)&&typeof sessions==="object")sessions=Object.values(sessions);
      $("wechat-chat").replaceChildren();$("wechat-manual-chat").value="";sessions.forEach(s=>{const id=s.username||s.chat_id||s.id||s.wxid||s.name;const name=s.display_name||s.displayName||s.nickname||s.chat_name||s.name||id;if(id)$("wechat-chat").add(new Option(name,String(id)));});
      const noSessions=!$("wechat-chat").options.length;if(noSessions)$("wechat-chat").add(new Option("请在下方手动输入会话",""));$("wechat-chat").disabled=noSessions;
      const mode=result.manual_only?"当前通过 Python 适配器读取，无法列出会话。请手动输入聊天名或稳定聊天 ID。同名聊天请使用稳定 ID。":"填写后优先使用此输入。同名聊天请使用稳定聊天 ID，避免读取错误的会话。";
      $("wechat-mode").textContent=[mode,...(result.warnings||[]).map(w=>typeof w==="string"?w:JSON.stringify(w))].join(" ");showError($("wechat-error"),"");updateWechatImport();showDialog("wechat-dialog");if(noSessions)$("wechat-manual-chat").focus();
    }catch(error){$("wechat-chat").replaceChildren(new Option("会话列表暂不可用",""));$("wechat-chat").disabled=true;$("wechat-manual-chat").value="";$("wechat-mode").textContent="会话列表暂不可用，可尝试手动输入聊天名或稳定聊天 ID。同名聊天请使用稳定 ID。";showError($("wechat-error"),error.message);updateWechatImport();showDialog("wechat-dialog");}});
  }
  function updateWechatImport() {$("wechat-import").disabled=!$("wechat-manual-chat").value.trim()&&!$("wechat-chat").value;}
  async function importWechat() {
    if(state.job){showError($("wechat-error"),"当前有分析任务进行中，请等本轮完成后再更换数据。");return;}
    const manual=$("wechat-manual-chat").value.trim(),chat=manual||$("wechat-chat").value,limit=Math.min(10000,Math.max(1,Number($("wechat-limit").value)||1000));if(!chat)return;
    await busy($("wechat-import"),async()=>{try{const result=await api("/api/wechat/history",{chat,limit});await loadDataset(result,`微信 · ${manual||$("wechat-chat").selectedOptions[0].textContent}`,"wechat");closeDialog("wechat-dialog");toast("本机会话已载入。");}catch(error){showError($("wechat-error"),error.message);}});updateWechatImport();
  }

  $("drop-zone").addEventListener("click",()=>$("file-input").click());$("drop-zone").addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();$("file-input").click();}});
  $("file-input").addEventListener("change",()=>importFile($("file-input").files[0]));
  ["dragenter","dragover"].forEach(event=>$("drop-zone").addEventListener(event,e=>{e.preventDefault();$("drop-zone").classList.add("dragging");}));
  ["dragleave","drop"].forEach(event=>$("drop-zone").addEventListener(event,e=>{e.preventDefault();$("drop-zone").classList.remove("dragging");}));
  $("drop-zone").addEventListener("drop",e=>importFile(e.dataTransfer.files[0]));
  $("demo-load").addEventListener("click",()=>loadDemo($("demo-load")));$("empty-demo").addEventListener("click",()=>loadDemo($("empty-demo")));
  $("clear-data").addEventListener("click",()=>{if(state.job){toast("当前有分析任务进行中，请等本轮完成后再清空数据。");return;}loadDataset({messages:[]},"","import").then(()=>{$("dataset-source").hidden=true;toast("当前页面数据已清空。");});});
  ["chat-filter","date-start","date-end"].forEach(id=>$(id).addEventListener("change",refreshSummary));
  $("filters-reset").addEventListener("click",()=>{$("chat-filter").value="";$("date-start").value="";$("date-end").value="";refreshSummary();});
  $("settings-open").addEventListener("click",()=>{showError($("settings-error"),"");showDialog("settings-dialog");loadSettings();});
  $("settings-form").addEventListener("submit",saveSettings);
  $("key-show").addEventListener("click",()=>{const hidden=$("api-key").type==="password";$("api-key").type=hidden?"text":"password";$("key-show").textContent=hidden?"隐藏":"显示";$("key-show").setAttribute("aria-label",hidden?"隐藏密钥":"显示密钥");});
  $("key-clear").addEventListener("click",async()=>{await busy($("key-clear"),async()=>{try{await api("/api/settings",{api_key:""});$("api-key").value="";await loadSettings();toast("密钥已从进程内存中清除。");}catch(error){showError($("settings-error"),error.message);}});});
  document.querySelectorAll("[data-close]").forEach(button=>button.addEventListener("click",()=>closeDialog(button.dataset.close)));
  $("settings-dialog").addEventListener("close",()=>{$("api-key").value="";$("api-key").type="password";$("key-show").textContent="显示";});
  $("analyze-open").addEventListener("click",openAnalysis);["analysis-limit","skip-analyzed"].forEach(id=>$(id).addEventListener("change",loadPreview));
  $("preview-all").addEventListener("change",()=>{state.previewSelected=$("preview-all").checked?new Set(state.preview.map(m=>String(m.id??m.message_id))):new Set();renderPreview();});
  $("analysis-send").addEventListener("click",startAnalysis);$("analysis-dialog").addEventListener("close",()=>{previewRequest++;state.preview=[];state.previewId=null;state.previewSelected.clear();});
  $("job-retry").addEventListener("click",()=>{if(state.job){state.job.failures=0;$("job-retry").hidden=true;pollJob();}});
  $("drawer-close").addEventListener("click",closeEvidence);$("drawer-backdrop").addEventListener("click",closeEvidence);
  document.addEventListener("keydown",e=>{if(!$("evidence-drawer").hidden){if(e.key==="Escape"){e.preventDefault();closeEvidence();}if(e.key==="Tab"){const focus=[...$("evidence-drawer").querySelectorAll("button,select,[tabindex]:not([tabindex='-1'])")];const first=focus[0],last=focus[focus.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}}});
  $("export-report").addEventListener("click",exportReport);$("wechat-connect").addEventListener("click",connectWechat);$("wechat-import").addEventListener("click",importWechat);
  $("wechat-manual-chat").addEventListener("input",updateWechatImport);$("wechat-chat").addEventListener("change",updateWechatImport);
  loadSettings();wechatStatus();
})();
