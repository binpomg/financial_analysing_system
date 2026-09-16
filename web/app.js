const $=id=>document.getElementById(id);
const names={ready:'已完整准备',incomplete:'准备不完整',queued:'排队中',running:'处理中',awaiting_audit:'等待审核',auditing:'独立审核中',pass:'通过',revise:'需要修订',insufficient_evidence:'证据不足',audited:'已完成独立审核',completed:'已完成',completed_with_issues:'已结束，存在问题',no_documents:'本批无可处理新原件',failed:'失败',interrupted:'已中断',reading_incomplete:'阅读不完整',blocked:'未通过',audit_passed:'审核通过',requires_review:'需要修订',needs_data:'待补资料',not_applicable:'全文阅读后不适用',pending:'待处理',candidate_unvalidated:'候选待验证',no_issues:'无待核实问题',already_reviewed:'已有核实认领记录',recovered:'已关联核实终态',registering_feedback:'核实登记中',skipped:'本阶段未执行',confirmed:'问题成立',rejected:'问题不成立',uncertain:'待进一步核实'};
names.paused='已暂停';names.stopping='当前材料结束后暂停';names.exhausted='当前范围无更多新原件';names.source_exhausted='当前范围无更多新原件';names.prepared='待启动';
let state=null,selectedRun=null,selectedPipeline=null,selectedCampaign=null,lastSignature='',pipelineSubmitting=false,campaignStopSubmitting=false;
const text=(tag,value,cls)=>{let el=document.createElement(tag);el.textContent=value??'';if(cls)el.className=cls;return el};
const badge=v=>text('span',names[v]||v||'—','badge '+(['failed','blocked','incomplete'].includes(v)?'fail':['requires_review','needs_data','reading_incomplete','completed_with_issues','interrupted','candidate_unvalidated'].includes(v)?'warn':''));
function notice(message,error=false){$('notice').hidden=false;$('notice').textContent=message;$('notice').className='notice'+(error?' error':'')}
async function api(path,body){let res=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});let value=await res.json();if(!res.ok)throw Error(value.error||'请求失败');return value}
function timestamp(value){return new Date(value).toISOString()}
function options(id,items,label){const select=$(id),chosen=[...select.selectedOptions].map(o=>o.value);select.replaceChildren();items.forEach(item=>{const o=text('option',label(item));o.value=item.id;o.selected=chosen.includes(item.id);select.append(o)})}
function link(label,url){const a=text('a',label);a.href=url;a.target='_blank';a.rel='noopener';return a}
function render(){const s=state,labels=Object.fromEntries(s.lines.map(l=>[l.id,l.name]));$('connection').textContent=s.environment.credential_available?'模型认证已配置':'尚未配置模型认证';
 const stats=[['原始资料',s.documents.length],['研究案例',s.cases.length],['完成审核',s.runs.filter(r=>r.status==='audited').length],['待修订 / 失败',s.runs.filter(r=>['failed','interrupted','reading_incomplete'].includes(r.status)||r.release_status==='requires_review').length]];
 $('stats').replaceChildren(...stats.map(([name,value])=>{const d=text('div','','stat');d.append(text('span',name),text('b',value));return d}));
 options('documents',s.documents,d=>`${d.name} · ${names[d.preparation]}`);options('cases',s.cases,c=>`${c.id.slice(-8)} · ${c.task.slice(0,45)}`);
 if(!$('line-checkboxes').querySelector('input')){s.lines.forEach(l=>{const label=text('label',''),input=document.createElement('input');input.type='checkbox';input.value=l.id;input.checked=l.id==='extraction';label.append(input,document.createTextNode(l.name));$('line-checkboxes').append(label)})}
 $('document-rows').replaceChildren(...s.documents.map(d=>{const tr=document.createElement('tr'),name=document.createElement('td');name.append(link(d.name,'/api/original?id='+d.id),text('small',d.group_id));const status=document.createElement('td');status.append(badge(d.preparation));if(d.warnings.length)status.append(text('small',d.warnings.join('；')));tr.append(name,text('td',d.published_at?new Date(d.published_at).toLocaleString():'待登记'),text('td',d.units),status);return tr}));
 $('run-rows').replaceChildren(...[...s.runs].reverse().map(r=>{const tr=document.createElement('tr'),name=text('td',labels[r.line]||r.line);name.append(text('small',r.id));const status=document.createElement('td'),release=document.createElement('td'),action=document.createElement('td');status.append(badge(r.status));release.append(badge(r.effective_release_status||r.release_status));if(r.error)release.append(text('small',r.error));const b=text('button','查看成果');b.addEventListener('click',()=>showRun(r.id));action.append(b);tr.append(name,status,release,action);return tr}));
 $('line-cards').replaceChildren(...s.lines.map((l,i)=>{const d=text('div','','line-card');d.append(text('b',String(i+1).padStart(2,'0')+' / '+l.name),text('small','独立 Skill · 独立评价与审核'),document.createElement('br'),text('small',l.maturity==='uncalibrated'?'方法已建立 · 尚待专业校准':l.maturity));return d}));
 $('jobs').replaceChildren(...s.jobs.slice(-4).map(j=>text('div',`${j.id} · ${names[j.status]||j.status}${j.error?' · '+j.error:''}`)));
 renderCampaigns();renderPipelines();
 $('footer').textContent=`资料存储：${s.environment.data_directory} ｜ 采集 gpt-5.6-luna · xhigh ｜ 业务/审核 gpt-6-astra · xhigh`;
}
async function refresh(){try{const s=await api('/api/state');const sig=JSON.stringify(s);state=s;if(sig!==lastSignature){render();lastSignature=sig;if(selectedPipeline)await showPipeline(selectedPipeline,false);if(selectedCampaign)await showCampaign(selectedCampaign,false)}}catch(e){notice(e.message,true)}}
function campaignActive(c){return ['queued','running','stopping'].includes(c.status)}
function pipelineBusy(){return pipelineSubmitting||(state?.jobs||[]).some(j=>['queued','running'].includes(j.status))||(state?.pipelines||[]).some(p=>p.status==='running')||(state?.campaigns||[]).some(campaignActive)}
function renderCampaigns(){
 const campaigns=state.campaigns||[];$('campaigns').hidden=!campaigns.length;
 $('campaign-cards').replaceChildren(...[...campaigns].reverse().map(c=>{
  const card=text('div','','campaign-card'),p=c.progress||{},target=c.target_documents||100;
  const head=text('div','','campaign-head');head.append(text('b',`${p.processed_documents||0} / ${target} 份材料已完成七线尝试`),badge(c.status));card.append(head);
  const meter=document.createElement('progress');meter.max=target;meter.value=p.processed_documents||0;meter.setAttribute('aria-label','已完成七线尝试的材料数量');card.append(meter);
  card.append(text('p',`已进入处理 ${p.attempted_documents||0} 份 · 完成独立审核 ${p.audited_runs||0} 条 · 失败或阅读不完整 ${p.failed_runs||0} 条`),text('p',`本任务已确认反馈 ${p.feedback_count||0} 条 · 候选 ${p.candidate_count||0} 个；候选不会自动晋升。`));
  if(c.current_pipeline_id){const current=text('button','查看当前材料的七线进度','secondary');current.addEventListener('click',()=>showPipeline(c.current_pipeline_id));card.append(current)}
  const view=text('button','查看训练记录','secondary');view.addEventListener('click',()=>showCampaign(c.id));card.append(view);
  if(campaignActive(c)){const stop=text('button',c.stop_requested?'已请求，当前材料结束后暂停':'当前材料结束后暂停','secondary');stop.disabled=c.stop_requested||campaignStopSubmitting;stop.addEventListener('click',()=>stopCampaign(c.id));card.append(stop)}
  if(c.error)card.append(text('p',c.error));card.append(text('small',`${c.id}${c.created_at?' · '+new Date(c.created_at).toLocaleString():''}`));return card;
 }));
}
async function stopCampaign(id){
 if(campaignStopSubmitting)return;campaignStopSubmitting=true;renderCampaigns();
 try{await api('/api/campaign/stop',{campaign_id:id});notice('已请求暂停。当前材料的七线处理与反馈阶段会继续保存，随后停止获取下一份材料。');await refresh()}
 catch(e){notice(e.message,true)}finally{campaignStopSubmitting=false;renderCampaigns()}
}
async function showCampaign(id,scroll=true){
 try{
  const c=await api('/api/campaign?id='+encodeURIComponent(id));selectedCampaign=id;const d=$('campaign-detail');d.hidden=false;d.replaceChildren(text('h2','连续训练 · '+c.id),badge(c.status));
  d.append(text('p','目标：'+c.target_documents+' 份不同原件。已完成数量只表示每份材料完成七线尝试；是否可用仍以每条成果和独立审核为准。'));
  const wrap=text('div','','table-wrap'),table=document.createElement('table'),head=document.createElement('tr');['材料批次','状态','原件数量','操作'].forEach(label=>head.append(text('th',label)));table.append(head);
  (c.pipelines||[]).forEach(p=>{const tr=document.createElement('tr'),status=text('td'),actions=text('td');status.append(badge(p.status));const view=text('button','查看七线结果');view.addEventListener('click',()=>showPipeline(p.pipeline_id));actions.append(view);tr.append(text('td',p.pipeline_id),status,text('td',(p.document_ids||[]).length),actions);table.append(tr)});wrap.append(table);d.append(wrap);
  if(c.error)d.append(text('p',c.error));const details=document.createElement('details');details.append(text('summary','完整训练记录'),text('pre',JSON.stringify(c,null,2)));d.append(details);if(scroll)d.scrollIntoView({behavior:'smooth',block:'start'});
 }catch(e){notice(e.message,true)}
}
function renderPipelines(){
 const summary=state.training_summary||{};
 $('training-summary').textContent=`反馈记录 ${summary.feedback_count||0} 条（独立模型完成核实 ${summary.model_confirmed_feedback_count||0}） · 候选记录 ${summary.candidate_count||0} 个 · 稳定版本变更记录 ${summary.adoption_count||0} 条`;
 $('start-pipeline').disabled=pipelineBusy();
 const rows=[...(state.pipelines||[])].reverse().map(p=>{
  const tr=document.createElement('tr'),name=text('td',p.id),status=text('td'),actions=text('td');
  name.append(text('small',p.created_at?new Date(p.created_at).toLocaleString():'时间待登记'));status.append(badge(p.status));if(p.error)status.append(text('small',p.error));
  const steps=p.steps||[],audited=steps.filter(step=>step.run_status==='audited').length,failed=steps.filter(step=>['failed','interrupted','reading_incomplete','blocked'].includes(step.run_status)).length;
  const progress=text('td',`完成独立审核 ${audited} / 7`);if(failed)progress.append(text('small',`失败或未完整完成 ${failed} 条`));
  const view=text('button','查看批次');view.addEventListener('click',()=>showPipeline(p.id));actions.append(view);
  if(p.status==='interrupted'){const resume=text('button','显式续批','secondary');resume.disabled=pipelineBusy();resume.addEventListener('click',()=>startPipeline(p.id));actions.append(resume)}
  tr.append(name,status,progress,actions);return tr;
 });
 if(!rows.length){const tr=document.createElement('tr'),td=text('td','还没有自动批次。启动后可在这里跟踪七条线的进度和反馈。');td.colSpan=4;tr.append(td);rows.push(tr)}
 $('pipeline-rows').replaceChildren(...rows);
}
async function startPipeline(id){
 if(pipelineBusy())return;
 pipelineSubmitting=true;renderPipelines();
 try{const job=await api('/api/pipeline',id?{pipeline_id:id}:{});notice(`${id?'续批':'自动批次'}已加入队列（${job.job_id}）。已发起的失败请求不会自动重试，所有阶段记录会保留。`);await refresh()}
 catch(e){notice(e.message,true)}
 finally{pipelineSubmitting=false;renderPipelines()}
}
async function showPipeline(id,scroll=true){
 try{
  const p=await api('/api/pipeline?id='+encodeURIComponent(id));selectedPipeline=id;
  const d=$('pipeline-detail');d.hidden=false;d.replaceChildren(text('h2','自动批次 · '+p.id),badge(p.status));
  if(p.error)d.append(text('p',p.error));
  (p.document_ids||[]).forEach(id=>{const original=state.documents.find(doc=>doc.id===id);d.append(link(original?.name||'查看本批原件','/api/original?id='+encodeURIComponent(id)))});
  if(p.case_ids?.length)d.append(text('p','开发案例：'+p.case_ids.join('、')));
  const table=document.createElement('table'),head=document.createElement('tr');['业务线','执行与审核','业务结果','反馈核实','成果'].forEach(label=>head.append(text('th',label)));table.append(head);
  const labels=Object.fromEntries(state.lines.map(line=>[line.id,line.name]));
  (p.steps||[]).forEach(step=>{const tr=document.createElement('tr'),execution=text('td'),release=text('td'),feedback=text('td'),actions=text('td');execution.append(badge(step.run_status||step.status));release.append(badge(step.release_status));feedback.append(badge(step.status==='done'&&step.run_status!=='audited'&&step.feedback_status==='pending'?'skipped':step.feedback_status));if(step.feedback_error)feedback.append(text('small',step.feedback_error));if(step.error)execution.append(text('small',step.error));if(step.run_id){const view=text('button','查看成果');view.addEventListener('click',()=>showRun(step.run_id));actions.append(view)}tr.append(text('td',labels[step.line]||step.line),execution,release,feedback,actions);table.append(tr)});
  const wrap=text('div','','table-wrap');wrap.append(table);d.append(wrap,text('h3','候选积累'));
  const attempts=p.candidate_attempts||[];d.append(text('p',attempts.length?'本批记录了候选尝试；生成候选不代表验证通过或稳定版升级。':'本批尚未尝试候选；继续积累已核实反馈。'));
  attempts.forEach(attempt=>{const block=text('div');block.append(text('p',labels[attempt.line]||attempt.line),badge(attempt.status));if(attempt.candidate_id)block.append(text('p','候选：'+attempt.candidate_id));if(attempt.error)block.append(text('p','尝试未完成：'+attempt.error));if(attempt.next_step)block.append(text('p',attempt.next_step));d.append(block)});
  (p.candidate_waiting||[]).forEach(item=>{let note=`${labels[item.line]||item.line}：${item.reason||'等待后续处理'}`;if(typeof item.confirmed_case_count==='number'){const threshold=p.frozen?.settings?.min_feedback_cases;note+=`\n已核实独立材料组：${item.confirmed_case_count}${typeof threshold==='number'?' / '+threshold:''}`};d.append(text('p',note))});
  if(p.status==='interrupted'){const resume=text('button','显式续批');resume.disabled=pipelineBusy();resume.addEventListener('click',()=>startPipeline(p.id));d.append(text('p','续批继续尚未发起的阶段；已发起的失败请求保留原状态，不自动重新收费执行。'),resume)}
  const details=document.createElement('details');details.append(text('summary','完整批次记录'),text('pre',JSON.stringify(p,null,2)));d.append(details);if(scroll)d.scrollIntoView({behavior:'smooth',block:'start'});
 }catch(e){notice(e.message,true)}
}
function appendFeedbackVerifications(d,r){
 const records=r.feedback_verifications||[];if(!records.length)return;
 d.append(text('h3','审核问题的独立核实'),text('p','以下是对原审核问题的再次核实，不覆盖上方原审核。模型核实不等于专家金标；问题不成立或证据不足时，不进入已确认反馈积累。'));
 records.forEach(record=>{
  const block=text('div');block.append(text('p',`核实记录 ${record.id}${record.created_at?' · '+new Date(record.created_at).toLocaleString():''}`),badge(record.status));
  if(record.error)block.append(text('p',record.error));
  const complete=record.status==='completed';
  if(!complete&&!['no_issues','already_reviewed'].includes(record.status))block.append(text('p','本次核实未完整结束；暂存判断或部分登记文件不能当作已确认反馈。'));
  if(record.status==='no_issues')block.append(text('p','原审核没有待核实问题，本次未调用核实模型。'));
  if(record.status==='already_reviewed')block.append(text('p','相关问题已有认领记录；失败或中断的核实不会自动重试，也不代表问题已确认成立。'));
  (record.decisions||[]).forEach(decision=>{
   const issue=r.audit?.issues?.[decision.issue_index],number=Number.isInteger(decision.issue_index)?decision.issue_index+1:'—';
   block.append(text('h3',`问题 ${number}${issue?.description?' · '+issue.description:''}`));
   if(complete)block.append(badge(decision.verdict));else block.append(text('p','暂存判断（核实未完成）：'+(names[decision.verdict]||decision.verdict)));
   block.append(text('p',decision.reason));if(decision.pending_reason)block.append(text('p','保留原因：'+decision.pending_reason));
   if(decision.result_pointers?.length)block.append(text('p','成果位置：'+decision.result_pointers.join('、')));
   (decision.evidence||[]).forEach(e=>{block.append(text('blockquote',e.quote));if(e.document_id)block.append(link(`查看原件 · ${e.unit_id||'原文位置'}`,'/api/original?id='+encodeURIComponent(e.document_id)))});
   const registered=complete&&decision.verdict==='confirmed'&&decision.feedback_registered;
   let registration=registered?'已登记反馈：'+decision.registered_feedback_ids.join('、'):!complete?'未完成反馈登记，不计入已确认反馈。':decision.verdict==='rejected'?'未登记反馈：本条审核问题被判定不成立。':decision.verdict==='uncertain'?'未登记反馈：证据不足，保留待核实状态。':'尚未完成有效反馈登记。';
   block.append(text('p',registration));
  });
  if(record.remaining_issue_indices?.length)block.append(text('p','尚未认领的待核实问题：'+record.remaining_issue_indices.map(i=>i+1).join('、')));
  d.append(block);
 });
}
async function showRun(id){try{selectedRun=await api('/api/run?id='+id);const r=selectedRun,d=$('detail');d.hidden=false;d.replaceChildren(text('h2','研究成果 · '+(state.lines.find(l=>l.id===r.line)?.name||r.line)));d.append(text('p',`运行 ${r.id}\nSkill ${r.skill_version}`));const links=text('div','','detail-links');(r.result?['report.md','records.csv','result.json']:['report.md','result.json']).forEach(f=>links.append(link(f,'/api/export?id='+id+'&file='+f)));d.append(links);
 if(r.error)d.append(text('p',r.error));if(r.result){d.append(badge(r.effective_release_status||r.release_status),text('p',r.result.summary));const table=document.createElement('table');table.className='record-grid';const head=document.createElement('tr');['主体 / 期间','字段','原值 → 规范值','单位','原文证据'].forEach(v=>head.append(text('th',v)));table.append(head);r.result.records.forEach(row=>{const tr=document.createElement('tr'),ev=document.createElement('td');row.evidence.forEach(e=>{ev.append(link(`${e.document_id.slice(0,8)}/${e.unit_id}`,'/api/original?id='+e.document_id),text('small',e.quote))});tr.append(text('td',row.entity+' / '+row.period),text('td',row.field),text('td',row.raw_value+' → '+row.value),text('td',row.unit+' '+row.currency),ev);table.append(tr)});const wrap=text('div','','table-wrap');wrap.append(table);d.append(wrap);r.result.analysis.forEach(a=>{d.append(text('h3',a.heading),text('p',a.text));a.evidence.forEach(e=>d.append(text('blockquote',e.quote+' ['+e.unit_id+']')))});if(r.result.missing_data.length)d.append(text('h3','待补资料'),text('p',r.result.missing_data.join('\n')));if(r.result.warnings.length)d.append(text('h3','处理说明'),text('p',r.result.warnings.join('\n')))}
 if(r.audit){d.append(text('h3','独立审核'),badge(r.audit.verdict),text('p',r.audit.summary));r.audit.issues.forEach((issue,index)=>{const block=text('div');block.append(text('p',`[${issue.severity}] ${issue.description}\n修改建议：${issue.correction}`));if(!['holdout','calibration'].includes(r.partition)){const b=text('button','记录已核实反馈');b.addEventListener('click',async()=>{const note=prompt('请填写回查原件后的核实依据。此操作会将问题加入改进资料。');if(note){try{await api('/api/confirm',{run_id:r.id,issue:index,note});notice('核实反馈已加入任务队列。')}catch(e){notice(e.message,true)}}});block.append(b)}d.append(block)});r.audit.dimensions.forEach(v=>d.append(text('p',`${v.name} · ${v.judgment}\n${v.reason}`)))}appendFeedbackVerifications(d,r);const details=document.createElement('details');details.append(text('summary','查看完整运行记录'),text('pre',JSON.stringify(r,null,2)));d.append(details);d.scrollIntoView({behavior:'smooth',block:'start'})}catch(e){notice(e.message,true)}}
$('import-form').addEventListener('submit',async e=>{e.preventDefault();const file=$('file').files[0];if(!file)return;try{if(file.size>16*1024*1024)throw Error('界面导入限16MB，大文件请用命令行。');const encoded=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)});await api('/api/import',{name:file.name,base64:encoded,published_at:timestamp($('published').value),source_url:$('source').value,group_id:$('group').value});notice('原件已加入准备队列，完成后会出现在材料列表。');await refresh()}catch(err){notice(err.message,true)}});
$('case-form').addEventListener('submit',async e=>{e.preventDefault();try{await api('/api/case',{document_ids:[...$('documents').selectedOptions].map(o=>o.value),task:$('task').value,cutoff:timestamp($('cutoff').value),partition:$('partition').value});notice('研究案例已加入创建队列。');await refresh()}catch(err){notice(err.message,true)}});
$('run-form').addEventListener('submit',async e=>{e.preventDefault();try{const lines=[...$('line-checkboxes').querySelectorAll('input:checked')].map(i=>i.value);if(!lines.length)throw Error('请至少选择一条业务线。');await api('/api/run',{case_id:$('cases').value,lines});notice('已加入执行队列。每条线会完整阅读材料，再由独立审核复核；结果随进度更新。');await refresh()}catch(err){notice(err.message,true)}});
$('start-pipeline').addEventListener('click',()=>startPipeline());
refresh();setInterval(refresh,5000);
