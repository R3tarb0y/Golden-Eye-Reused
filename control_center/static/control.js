const $ = (q) => document.querySelector(q);
const $$ = (q) => [...document.querySelectorAll(q)];
let STATE = null;
let ACTIVE_JOB_ID = null;
let CURRENT_JOB_STEPS = [];

function toast(msg, error=false){const el=$('#toast');el.textContent=msg;el.className='toast'+(error?' error':'');setTimeout(()=>el.classList.add('hidden'),3800)}
function fmtDate(v){if(!v)return '-';try{return new Date(v).toLocaleString('id-ID',{dateStyle:'medium',timeStyle:'short'})}catch{return v}}
function statusClass(s){return 'st-'+String(s||'pending').toLowerCase()}
function stepClass(s){return 's-'+String(s||'waiting').toLowerCase()}
function statusChip(s){return `<span class="status-chip ${statusClass(s)}">${s}</span>`}
function selectedCompanies(root){return [...root.querySelectorAll('input[type=checkbox]:checked')].map(x=>x.value)}

async function jsonFetch(url, opts={}){const r=await fetch(url,{headers:{'Content-Type':'application/json'},...opts});const data=await r.json().catch(()=>({}));if(!r.ok)throw new Error(data.error||`HTTP ${r.status}`);return data}

function renderState(s){STATE=s;
  $('#suggested-month').textContent=s.suggested_month||'-';
  $('#last-success').textContent=s.latest_successful_month||'BELUM ADA';
  const active=s.jobs.filter(j=>['PENDING','RUNNING'].includes(j.status));$('#queue-count').textContent=active.length;
  const worker=(s.workers||[]).find(w=>w.online);
  $('#worker-status').textContent=worker?worker.status:'OFFLINE';
  $('#worker-meta').textContent=worker?`${worker.hostname} · PID ${worker.pid}`:'Jalankan run_control_worker.py';
  $('#sidebar-worker').textContent=worker?`Worker ${worker.status}`:'Worker offline';
  $('#sidebar-worker-dot').className='dot '+(worker?'online':'offline');
  const cfg=s.config||{};const pill=$('#config-pill');pill.textContent=cfg.complete?'SOURCE READY':'SOURCE CONFIG BELUM LENGKAP';pill.className='pill '+(cfg.complete?'good':'bad');
  renderConfig(cfg);renderHistory(s.jobs||[]);renderSchedule(s.schedule||{});renderActive(active);renderLineage(s.lineage||{});renderSourceHealth(s.health||{});renderPipelineGraph(s.lineage||{},s.health||{},CURRENT_JOB_STEPS);
}

function renderConfig(cfg){const root=$('#config-list');root.innerHTML='';(cfg.companies||[]).forEach(c=>{const pan=(c.panen_roots||[]).join(' ; ')||'PANEN ROOT belum diisi';const tr=(c.transport_roots||[]).join(' ; ');root.insertAdjacentHTML('beforeend',`<div class="config-row"><div class="config-company">${c.company}</div><div class="config-paths" title="${pan}${tr?' | Transport: '+tr:''}">${pan}${tr?' · Transport configured':''}</div><div class="${c.ok?'config-ok':'config-bad'}">${c.ok?'✓':'!'}</div></div>`)})}



function renderLineage(lineage){
  const rules=$('#trace-rules');
  if(rules){
    const items=(lineage.trace_rules||[]).map(x=>`<li>${escapeHtml(x)}</li>`).join('');
    rules.innerHTML=`<ul class="trace-rule-list">${items}</ul>`;
  }
  const systemsRoot=$('#lineage-systems');
  if(systemsRoot){
    systemsRoot.innerHTML='';
    (lineage.systems||[]).forEach(sys=>{
      const tables=(sys.tables||[]).map(t=>`<div class="table-chip"><div class="table-chip-name">${escapeHtml(t.name)}</div><div class="table-chip-role">${escapeHtml(t.role)}</div></div>`).join('');
      systemsRoot.insertAdjacentHTML('beforeend',`<article class="system-card kind-${(sys.kind||'').toLowerCase()}"><div class="system-head"><div><div class="system-kind">${escapeHtml(sys.engine||'')}</div><h3>${escapeHtml(sys.title||'')}</h3></div><div class="system-db">${escapeHtml(sys.database||'')}</div></div><p class="system-desc">${escapeHtml(sys.description||'')}</p><div class="table-chip-wrap">${tables}</div></article>`);
    });
  }
  const pipeRoot=$('#lineage-pipelines');
  if(pipeRoot){
    pipeRoot.innerHTML='';
    (lineage.pipelines||[]).forEach((pipe,idx)=>{
      const steps=(pipe.steps||[]).map((step,stepIdx)=>{
        const reads=(step.reads||[]).map(x=>`<span class="io-tag read">${escapeHtml(x)}</span>`).join('');
        const writes=(step.writes||[]).map(x=>`<span class="io-tag write">${escapeHtml(x)}</span>`).join('');
        return `<div class="trace-step"><div class="trace-step-index">${String(stepIdx+1).padStart(2,'0')}</div><div class="trace-step-body"><div class="trace-step-title">${escapeHtml(step.name)}</div><div class="trace-step-command">${escapeHtml(step.command)}</div><div class="trace-io-grid"><div><div class="io-title">READS</div><div class="io-wrap">${reads}</div></div><div><div class="io-title">WRITES</div><div class="io-wrap">${writes}</div></div></div></div></div>`;
      }).join('');
      pipeRoot.insertAdjacentHTML('beforeend',`<section class="pipeline-card"><div class="pipeline-card-head"><div><div class="section-kicker">PHASE ${idx+1}</div><h3>${escapeHtml(pipe.title||'')}</h3></div></div><p class="pipeline-desc">${escapeHtml(pipe.description||'')}</p><div class="trace-step-list">${steps}</div></section>`);
    });
  }
}

function healthClass(status){
  const s=String(status||'UNKNOWN').toUpperCase();
  if(['READY','SUCCESS'].includes(s))return 'ready';
  if(['RUNNING'].includes(s))return 'running';
  if(['FAILED','OFFLINE'].includes(s))return 'failed';
  if(['WARNING'].includes(s))return 'warning';
  if(['EXTERNAL'].includes(s))return 'external';
  return 'waiting';
}

function renderSourceHealth(health){
  const root=$('#source-health-grid');if(!root)return;root.innerHTML='';
  ['scrapeops','mssql','sap_db60','nas','postgres','geospatial'].forEach(key=>{
    const h=health[key];if(!h)return;
    const missingRows=h.missing||[];const missing=missingRows.length;
    const missingNames=missingRows.slice(0,2).map(x=>typeof x==='string'?x:(x.name||x.path||x.company||'missing')).join(' · ');
    const detail=missing?`${missing} missing: ${missingNames}${missing>2?' …':''}`:(h.status==='EXTERNAL'?'not controlled by Golden Eye':'no missing dependency');
    root.insertAdjacentHTML('beforeend',`<div class="health-card hc-${healthClass(h.status)}" title="${escapeHtml(detail)}"><div class="health-top"><strong>${escapeHtml(h.label||key)}</strong><span>${escapeHtml(h.status||'')}</span></div><div class="health-msg">${escapeHtml(h.message||'')}</div><div class="health-detail">${escapeHtml(detail)}</div></div>`)
  });
}

function stepStatusForNode(node, steps){
  if(!node.step_keys||!node.step_keys.length)return null;
  const byKey=new Map((steps||[]).map(s=>[s.step_key,s]));
  const found=node.step_keys.map(k=>byKey.get(k)).filter(Boolean);
  if(!found.length)return 'WAITING';
  if(found.some(s=>s.status==='FAILED'))return 'FAILED';
  if(found.some(s=>s.status==='RUNNING'))return 'RUNNING';
  if(found.some(s=>s.status==='PENDING'))return 'PENDING';
  if(found.every(s=>['SUCCESS','SKIPPED'].includes(s.status)))return 'SUCCESS';
  if(found.some(s=>s.status==='SUCCESS'))return 'PARTIAL';
  return found[0]?.status||'WAITING';
}

function nodeStatus(node,health,steps){
  const jobStatus=stepStatusForNode(node,steps);
  if(jobStatus&&jobStatus!=='WAITING')return jobStatus;
  if(node.health&&health[node.health])return health[node.health].status||'WAITING';
  return jobStatus||'WAITING';
}

function liveDetailForNode(node,steps){
  if(!node.step_keys||!node.step_keys.length)return '';
  const relevant=(steps||[]).filter(s=>node.step_keys.includes(s.step_key)&&s.status==='RUNNING');
  if(!relevant.length)return '';
  const step=relevant[0];const tail=String(step.log_tail||'').toUpperCase();
  const candidates=[
    ['PUBLISH','PUBLISH GOLD'],['VALIDATE','GPU VALIDATE'],['MATCH','MATCH HISTORICAL'],['EMBED','EMBED NEW ONLY'],
    ['SINKRONISASI CONTEXT','SYNC CONTEXT'],['CONTEXT BISNIS','SYNC CONTEXT'],['SILVER','REGISTER NAS → SILVER'],
    ['TAHAP 1/6','SYNC DB45 → RAW'],['SYNC RAW','SYNC DB45 → RAW']
  ];
  let best='',pos=-1;candidates.forEach(([token,label])=>{const p=tail.lastIndexOf(token);if(p>pos){pos=p;best=label}});
  return best||step.step_name||'RUNNING';
}

function renderPipelineGraph(lineage,health,steps){
  const svg=$('#pipeline-graph');if(!svg)return;const g=lineage.graph;if(!g){svg.innerHTML='';return}
  const nodes=new Map((g.nodes||[]).map(n=>[n.id,n]));
  let out=`<defs><marker id="arrowhead" markerWidth="9" markerHeight="7" refX="8" refY="3.5" orient="auto"><polygon points="0 0, 9 3.5, 0 7" fill="#9aaabc"></polygon></marker></defs>`;
  (g.lanes||[]).forEach(l=>{out+=`<rect class="dag-lane" x="${l.x}" y="${l.y}" width="${l.w}" height="${l.h}" rx="18"></rect><text class="dag-lane-title" x="${l.x+18}" y="${l.y+30}">${escapeHtml(l.title)}</text>`});
  (g.edges||[]).forEach(e=>{
    const a=nodes.get(e.from),b=nodes.get(e.to);if(!a||!b)return;
    const x1=a.x+a.w,y1=a.y+a.h/2,x2=b.x,y2=b.y+b.h/2;const dx=Math.max(35,(x2-x1)*.45);
    const d=`M ${x1} ${y1} C ${x1+dx} ${y1}, ${x2-dx} ${y2}, ${x2} ${y2}`;
    out+=`<path class="dag-edge" d="${d}" marker-end="url(#arrowhead)"></path>`;
    if(e.label){const lx=(x1+x2)/2,ly=(y1+y2)/2-6;out+=`<text class="dag-edge-label" x="${lx}" y="${ly}" text-anchor="middle">${escapeHtml(e.label)}</text>`}
  });
  (g.nodes||[]).forEach(n=>{
    const st=nodeStatus(n,health,steps);const cls=healthClass(st);const sub=escapeHtml(n.subtitle||'');const live=liveDetailForNode(n,steps);
    out+=`<foreignObject x="${n.x}" y="${n.y}" width="${n.w}" height="${n.h}"><div xmlns="http://www.w3.org/1999/xhtml" class="dag-node dn-${escapeHtml(n.type||'process')} ds-${cls}"><div class="dag-node-top"><span class="dag-node-title">${escapeHtml(n.title||'')}</span><span class="dag-node-status">${escapeHtml(st)}</span></div><div class="dag-node-sub">${sub}</div>${live?`<div class="dag-node-live">LIVE · ${escapeHtml(live)}</div>`:''}</div></foreignObject>`;
  });
  svg.innerHTML=out;
}

function renderHistory(jobs){const body=$('#history-body');body.innerHTML='';if(!jobs.length){body.innerHTML='<tr><td colspan="7" class="muted">Belum ada job.</td></tr>';return}(jobs||[]).forEach(j=>{body.insertAdjacentHTML('beforeend',`<tr><td>${statusChip(j.status)}</td><td><strong>${j.target_month}</strong></td><td>${j.pipeline_name}</td><td>${(j.companies||[]).join(', ')}</td><td>${Number(j.progress_pct||0).toFixed(0)}%</td><td>${fmtDate(j.started_at||j.created_at)}</td><td><button class="table-action" data-job="${j.job_id}">View</button></td></tr>`)});$$('[data-job]').forEach(b=>b.onclick=()=>openJob(b.dataset.job))}

function renderSchedule(s){$('#schedule-enabled').checked=!!s.enabled;$('#schedule-day').value=s.run_day||5;$('#schedule-time').value=`${String(s.run_hour||0).padStart(2,'0')}:${String(s.run_minute||0).padStart(2,'0')}`;$('#schedule-pipeline').value=s.pipeline_name||'MONTHLY_FULL';const selected=new Set(s.companies||[]);$$('#schedule-company-grid input').forEach(x=>x.checked=selected.has(x.value))}

function renderActive(active){const running=active.find(j=>j.status==='RUNNING')||active[0];ACTIVE_JOB_ID=running?.job_id||null;if(!running){CURRENT_JOB_STEPS=[];$('#active-empty').classList.remove('hidden');$('#active-wrap').classList.add('hidden');$('#cancel-btn').classList.add('hidden');renderPipelineGraph(STATE?.lineage||{},STATE?.health||{},CURRENT_JOB_STEPS);return}$('#active-empty').classList.add('hidden');$('#active-wrap').classList.remove('hidden');$('#cancel-btn').classList.remove('hidden');$('#active-code').textContent=`${running.status} · ${String(running.job_id).slice(0,8).toUpperCase()}`;$('#active-title').textContent=`${running.pipeline_name} · ${running.target_month}`;$('#active-meta').textContent=`${(running.companies||[]).join(' + ')} · ${running.current_step||'Waiting worker'}`;const pct=Number(running.progress_pct||0);$('#active-pct').textContent=`${pct.toFixed(0)}%`;$('#active-progress').style.width=`${pct}%`;loadActiveSteps(running.job_id)}

async function loadActiveSteps(id){try{const d=await jsonFetch(`/api/jobs/${id}`);if(ACTIVE_JOB_ID!==id)return;CURRENT_JOB_STEPS=d.steps||[];const root=$('#step-list');root.innerHTML='';CURRENT_JOB_STEPS.forEach(s=>{const cp=s.checkpoint_stage?` · checkpoint ${s.checkpoint_stage}`:'';const rid=s.golden_run_id?` · run ${String(s.golden_run_id).slice(0,8)}`:'';const rc=Number(s.resume_count||0)>0?` · resumed ${s.resume_count}x`:'';const live=s.checkpoint_detail?`<div class="step-live-detail">${escapeHtml(s.checkpoint_detail)}</div>`:'';root.insertAdjacentHTML('beforeend',`<div class="step-row"><div class="step-index">${String(s.step_order).padStart(2,'0')}</div><div><div class="step-name">${s.step_name}</div><div class="step-company">${s.company||'GLOBAL'}${cp}${rid}${rc}</div>${live}</div><div class="step-status ${stepClass(s.status)}">${s.status}</div></div>`)});renderPipelineGraph(STATE?.lineage||{},STATE?.health||{},CURRENT_JOB_STEPS)}catch(e){console.warn(e)}}



function fmtInt(v){const n=Number(v||0);return Number.isFinite(n)?n.toLocaleString('id-ID'):'0'}

function visualAuditStatusClass(text){
  const s=String(text||'').toUpperCase();
  if(s.includes('SAP VERIFIED'))return 'va-good';
  if(s.includes('UNVERIFIED')||s.includes('NOT IN SAP')||s.includes('OTHER MONTH'))return 'va-warning';
  if(s.includes('OUT-OF-SCOPE')||s.includes('NOT DISPLAYED'))return 'va-danger';
  return 'va-neutral';
}

function syncVisualAuditCompanyOptions(){
  const system=$('#visual-audit-system');const company=$('#visual-audit-company');if(!system||!company)return;
  const allowed=system.value==='HARMONY'?['','PWP','DIN']:system.value==='ELHP'?['','DAN','DWT']:['','PWP','DIN','DAN','DWT'];
  [...company.options].forEach(o=>o.hidden=!allowed.includes(o.value));
  if(!allowed.includes(company.value))company.value='';
}

function renderVisualAudit(data){
  const sum=data.summary||{};
  const kpis=[
    ['Visual Reused',sum.visual_usage,'accent-blue','main display'],
    ['SAP Verified',sum.official_dashboard_usage,'accent-green','official subset'],
    ['OPH Unverified',sum.unverified_oph,'accent-orange','needs review'],
    ['Policy Bypass',sum.policy_bypass_usage,'accent-violet','display exception'],
    ['Wrong Estate',sum.wrong_estate,'accent-orange','excluded from main'],
    ['Raw Visual',sum.raw_visual_usage,'accent-blue',`estate-valid ${fmtInt(sum.estate_visual_usage)}`],
  ];
  $('#visual-audit-kpis').innerHTML=kpis.map(([label,value,tone,meta])=>`<article class="status-card ${tone}"><div class="status-label">${escapeHtml(label)}</div><div class="status-value">${fmtInt(value)}</div><div class="status-meta">${escapeHtml(meta)}</div></article>`).join('');

  const checks=data.checks||[];
  $('#visual-audit-checks').innerHTML=checks.map(c=>`<div class="va-check va-${escapeHtml(c.tone||'info')}">${escapeHtml(c.text||'')}</div>`).join('');

  const monthly=(data.monthly||[]).slice().sort((a,b)=>`${a.month}-${a.company}`.localeCompare(`${b.month}-${b.company}`));
  const max=Math.max(1,...monthly.flatMap(r=>[Number(r.visual_usage||0),Number(r.official_dashboard_usage||0),Number(r.unverified_oph||0)]));
  const chart=$('#visual-audit-chart');
  if(!monthly.length){chart.innerHTML='<div class="muted">Tidak ada data Visual Audit pada filter ini.</div>'}
  else{
    chart.innerHTML=`<div class="va-legend"><span><i class="va-dot visual"></i>Visual Display</span><span><i class="va-dot verified"></i>SAP Verified</span><span><i class="va-dot unverified"></i>OPH Unverified</span></div>`+monthly.map(r=>{
      const visual=Number(r.visual_usage||0),verified=Number(r.official_dashboard_usage||0),unverified=Number(r.unverified_oph||0);
      const bar=(value,cls)=>`<div class="va-bar-track"><div class="va-bar ${cls}" style="width:${Math.max(value?2:0,(value/max)*100).toFixed(2)}%"></div><span>${fmtInt(value)}</span></div>`;
      return `<div class="va-month-row"><div class="va-month-label"><strong>${escapeHtml(r.company||'')}</strong><span>${escapeHtml(r.month||'')}</span></div><div class="va-month-bars">${bar(visual,'visual')}${bar(verified,'verified')}${bar(unverified,'unverified')}</div></div>`;
    }).join('');
  }

  const rows=data.groups||[];$('#visual-audit-group-count').textContent=`${fmtInt(data.group_count||rows.length)} group · limit ${fmtInt(data.group_limit||0)}`;
  const body=$('#visual-audit-body');
  if(!rows.length){body.innerHTML='<tr><td colspan="10" class="muted">Tidak ada group pada filter ini.</td></tr>'}
  else body.innerHTML=rows.map(r=>`<tr><td><strong>${escapeHtml(r.company||'')}</strong></td><td>${escapeHtml(r.group_id??'')}</td><td>${escapeHtml(r.months||'')}</td><td>${escapeHtml(r.estate_list||'')}</td><td>${fmtInt(r.visual_usage)}</td><td>${fmtInt(r.verified_usage)}</td><td>${fmtInt(r.unverified_oph)}</td><td>${fmtInt(r.not_in_sap)}</td><td>${fmtInt(r.wrong_estate)}</td><td><span class="va-status ${visualAuditStatusClass(r.verification_status)}">${escapeHtml(r.verification_status||'')}</span></td></tr>`).join('');
}

async function loadVisualAudit(){
  const btn=$('#visual-audit-load'),status=$('#visual-audit-status');if(!btn||!status)return;
  btn.disabled=true;status.textContent='Loading audit…';status.className='pill neutral';
  const system=$('#visual-audit-system')?.value||'';const company=$('#visual-audit-company')?.value||'';const month=$('#visual-audit-month')?.value||'';const year=month?month.slice(0,4):new Date().getFullYear();
  const q=new URLSearchParams({year:String(year),limit:'1000'});if(system)q.set('system',system);if(company)q.set('company',company);if(month)q.set('month',month);
  try{const data=await jsonFetch(`/api/visual-audit?${q.toString()}`);renderVisualAudit(data);status.textContent='Audit ready';status.className='pill good'}
  catch(e){status.textContent='Audit error';status.className='pill bad';toast(e.message,true)}
  finally{btn.disabled=false}
}

function diagMetric(label,value,unit=''){return `<div class="diag-metric"><span>${escapeHtml(label)}</span><strong>${value===null||value===undefined?'-':escapeHtml(value)}${unit}</strong></div>`}
function renderSilverDiagnostics(data){
  const status=$('#diag-status'),root=$('#diag-result');if(!status||!root)return;
  status.textContent=data.overall||'UNKNOWN';status.className='pill '+(data.overall==='READY'?'good':data.overall==='FAILED'?'bad':'neutral');
  const pg=data.postgres||{};
  let html=`<div class="diag-summary"><strong>${escapeHtml(data.company||'')} ${escapeHtml(data.kind||'')}</strong><span>${escapeHtml(data.reason||'')}</span></div>`;
  html+=`<div class="diag-block"><div class="diag-block-title">PostgreSQL ${escapeHtml(pg.host||'')}:${escapeHtml(pg.port||'')}</div><div class="diag-metrics">${diagMetric('TCP',(pg.tcp||{}).latency_ms,' ms')}${diagMetric('SELECT 1',pg.query_latency_ms,' ms')}${diagMetric('Active',pg.active_sessions)}${diagMetric('Waiting',pg.waiting_sessions)}${diagMetric('Lock waiter',(pg.lock_waiters||[]).length)}</div>${pg.error?`<div class="diag-error">${escapeHtml(pg.error)}</div>`:''}</div>`;
  (data.nas||[]).forEach((n,idx)=>{const smb=n.smb||{};html+=`<div class="diag-block"><div class="diag-block-title">NAS ${idx+1} · ${escapeHtml(n.server||'mapped/local')} · ${escapeHtml(n.status||'')}</div><div class="diag-path" title="${escapeHtml(n.resolved_path||n.path||'')}">${escapeHtml(n.resolved_path||n.path||'')}</div><div class="diag-metrics">${diagMetric('SMB 445',smb.latency_ms,' ms')}${diagMetric('Path',n.exists_latency_ms,' ms')}${diagMetric('Scan 250',n.scandir_latency_ms,' ms')}${diagMetric('Entries/s',n.sample_rate_per_sec)}${diagMetric('Read 1MB',n.read_mb_s,' MB/s')}</div>${n.error?`<div class="diag-error">${escapeHtml(n.error)}</div>`:''}</div>`});
  const blockers=[...(pg.lock_waiters||[]),...(pg.long_queries||[])];if(blockers.length){html+=`<div class="diag-block"><div class="diag-block-title">PostgreSQL activity to inspect</div>${blockers.map(x=>`<div class="diag-query">PID ${x.pid} · ${x.seconds}s · ${escapeHtml(x.wait_event_type||x.state||'')} · ${escapeHtml(x.query||'')}</div>`).join('')}</div>`}
  root.innerHTML=html;
}
async function runSilverDiagnostics(){const btn=$('#diag-run'),status=$('#diag-status');if(!btn||!status)return;btn.disabled=true;status.textContent='Testing…';status.className='pill neutral';const company=$('#diag-company')?.value||'PWP';const kind=$('#diag-kind')?.value||'PANEN';try{const q=new URLSearchParams({company,kind});const data=await jsonFetch(`/api/diagnostics/silver?${q.toString()}`);renderSilverDiagnostics(data);toast(`Diagnostic ${data.overall}: ${data.reason||''}`,data.overall==='FAILED')}catch(e){status.textContent='ERROR';status.className='pill bad';toast(e.message,true)}finally{btn.disabled=false}}

async function clearOrphans(){
  try{
    const r=await jsonFetch('/api/control/clear-orphans',{method:'POST',body:'{}'});
    toast(`Stale state dibersihkan: ${r.interrupted||0} job`);
    await refresh();
  }catch(e){toast(e.message,true)}
}

async function refresh(){try{renderState(await jsonFetch('/api/state'))}catch(e){toast(e.message,true)}}
async function runJob(){const companies=selectedCompanies($('#company-grid'));if(!companies.length)return toast('Pilih minimal satu company.',true);const payload={target_month:$('#target-month').value,pipeline_name:$('#pipeline-name').value,companies};try{const r=await jsonFetch('/api/jobs',{method:'POST',body:JSON.stringify(payload)});toast(`Job dibuat ${r.job_id.slice(0,8)}`);await refresh()}catch(e){toast(e.message,true)}}
async function saveSchedule(){const [h,m]=$('#schedule-time').value.split(':').map(Number);const payload={enabled:$('#schedule-enabled').checked,run_day:Number($('#schedule-day').value),run_hour:h,run_minute:m,pipeline_name:$('#schedule-pipeline').value,companies:selectedCompanies($('#schedule-company-grid'))};try{await jsonFetch('/api/schedule',{method:'POST',body:JSON.stringify(payload)});toast('Automation tersimpan.');await refresh()}catch(e){toast(e.message,true)}}
async function cancelActive(){if(!ACTIVE_JOB_ID)return;try{await jsonFetch(`/api/jobs/${ACTIVE_JOB_ID}/cancel`,{method:'POST',body:'{}'});toast('Stop requested. Worker akan berhenti setelah step berjalan selesai.')}catch(e){toast(e.message,true)}}

async function openJob(id){try{
  const d=await jsonFetch(`/api/jobs/${id}`);
  $('#drawer-title').textContent=`${d.target_month} · ${d.pipeline_name}`;
  let html=`<div class="drawer-actions">${['FAILED','PAUSED'].includes(d.status)?`<button class="secondary-btn" id="resume-job">Resume From Checkpoint</button>`:''}${d.status==='FAILED'?`<button class="ghost-btn" id="retry-job">Start New Retry Job</button>`:''}${['PENDING','RUNNING'].includes(d.status)?`<button class="danger-ghost" id="cancel-job">Stop After Current Step</button>`:''}</div><div class="run-summary">${statusChip(d.status)} · ${(d.companies||[]).join(' + ')} · ${Number(d.progress_pct||0).toFixed(0)}%</div>`;
  (d.steps||[]).forEach(s=>{
    const checkpoint=[];
    if(s.raw_complete)checkpoint.push('RAW ✓');
    if(s.silver_complete)checkpoint.push('SILVER ✓');
    if(s.checkpoint_stage)checkpoint.push(`STAGE ${s.checkpoint_stage}`);
    if(s.golden_run_id)checkpoint.push(`RUN ${String(s.golden_run_id).slice(0,8)}`);
    if(Number(s.resume_count||0)>0)checkpoint.push(`RESUME ${s.resume_count}x`);
    const cp=checkpoint.length?`<div class="checkpoint-strip">${checkpoint.map(x=>`<span>${escapeHtml(x)}</span>`).join('')}</div>`:'';
    const detail=s.checkpoint_detail?`<div class="checkpoint-detail">${escapeHtml(s.checkpoint_detail)}</div>`:'';
    html+=`<div class="drawer-step"><div class="drawer-step-head"><div><div class="drawer-step-name">${String(s.step_order).padStart(2,'0')} · ${s.step_name}</div><div class="step-company">${s.company||'GLOBAL'} · ${s.started_at?fmtDate(s.started_at):'not started'}</div></div><div class="step-status ${stepClass(s.status)}">${s.status}</div></div>${cp}${detail}${s.log_tail?`<div class="drawer-log">${escapeHtml(s.log_tail)}</div>`:''}</div>`;
  });
  $('#drawer-content').innerHTML=html;$('#drawer').classList.remove('hidden');$('#drawer-overlay').classList.remove('hidden');
  const resume=$('#resume-job');if(resume)resume.onclick=async()=>{try{const r=await jsonFetch(`/api/jobs/${id}/resume`,{method:'POST',body:'{}'});toast(r.mode==='CHECKPOINT'?`Resume checkpoint ${r.checkpoint_stage||''}`:'Step di-queue ulang.');closeDrawer();refresh()}catch(e){toast(e.message,true)}};
  const retry=$('#retry-job');if(retry)retry.onclick=async()=>{try{await jsonFetch(`/api/jobs/${id}/retry`,{method:'POST',body:'{}'});toast('Retry baru dibuat.');closeDrawer();refresh()}catch(e){toast(e.message,true)}};
  const cancel=$('#cancel-job');if(cancel)cancel.onclick=async()=>{try{await jsonFetch(`/api/jobs/${id}/cancel`,{method:'POST',body:'{}'});toast('Stop requested.');refresh()}catch(e){toast(e.message,true)}}
}catch(e){toast(e.message,true)}}

function escapeHtml(s){return String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))}
function closeDrawer(){$('#drawer').classList.add('hidden');$('#drawer-overlay').classList.add('hidden')}

$('#run-btn').onclick=runJob;$('#refresh-btn').onclick=refresh;const cob=$('#clear-orphans-btn');if(cob)cob.onclick=clearOrphans;$('#save-schedule').onclick=saveSchedule;$('#cancel-btn').onclick=cancelActive;$('#drawer-close').onclick=closeDrawer;$('#drawer-overlay').onclick=closeDrawer;const vab=$('#visual-audit-load');if(vab)vab.onclick=loadVisualAudit;const vas=$('#visual-audit-system');if(vas)vas.onchange=()=>{syncVisualAuditCompanyOptions()};const drb=$('#diag-run');if(drb)drb.onclick=runSilverDiagnostics;const srb=$('#storage-refresh');if(srb)srb.onclick=loadPgStorage;const spb=$('#storage-preview-delete');if(spb)spb.onclick=previewStorageDelete;
const hotAuditBtn=$('#hot-evidence-audit-run');if(hotAuditBtn)hotAuditBtn.onclick=runHotEvidenceAudit;
async function runHotEvidenceAudit(){
  const btn=$('#hot-evidence-audit-run'), status=$('#hot-evidence-audit-result');
  btn.disabled=true;status.textContent='Audit berjalan: memetakan UID/nama Gold aktif dan KT Before ke hot store tanpa mengambil BYTEA. Query bisa memerlukan waktu untuk pertama kali.';
  try{
    const r=await jsonFetch('/api/storage/hot-evidence-audit');
    const total=(r.summary||[]).reduce((a,x)=>a+Number(x.rows||0),0);
    const physical=readableBytes(r.physical_bytes);
    status.innerHTML=`<strong>${fmtInt(total)} baris · ${physical} physical</strong>` +
      (r.summary||[]).map(x=>`<div class="diag-metric"><span>${escapeHtml(x.category)}</span><strong>${fmtInt(x.rows)} rows / ${readableBytes(x.logical_bytes)}</strong></div>`).join('')+
      `<p class="muted small">${escapeHtml(r.note||'')}</p>`;
    const body=$('#hot-evidence-audit-rows');body.innerHTML='';
    (r.by_company||[]).forEach(x=>body.insertAdjacentHTML('beforeend',`<tr><td>${escapeHtml(x.source_group)}</td><td>${escapeHtml(x.category)}</td><td>${fmtInt(x.rows)}</td><td>${readableBytes(x.logical_bytes)}</td></tr>`));
  }catch(e){status.textContent=`Gagal audit: ${e.message}`;toast(e.message,true)}finally{btn.disabled=false}
}
function readableBytes(n){
  let v=Number(n||0);const u=['B','KB','MB','GB','TB'];let i=0;
  while(v>=1024&&i<u.length-1){v/=1024;i++}
  return `${v.toLocaleString('id-ID',{maximumFractionDigits:2})} ${u[i]}`;
}
async function loadPgStorage(){
  const btn=$('#storage-refresh');if(btn)btn.disabled=true;
  try{
    const data=await jsonFetch('/api/storage/relations?limit=100');
    $('#storage-db-size').textContent=readableBytes(data.database_bytes);
    $('#storage-db-name').textContent=`${data.database} · PostgreSQL ${data.server_version}`;
    const versions=data.snapshots||[];
    $('#storage-read-models').innerHTML=versions.length
      ?'<strong>PostgreSQL physical read-model</strong>'+versions.map(v=>`<div class="diag-metric"><span>${escapeHtml(v.source)}</span><strong>${fmtInt(v.rows)} rows · ${escapeHtml(v.refreshed_at||'')}</strong></div>`).join('')
      :'<strong>Read-model belum dibuat.</strong> Jalankan python dashboard_read_models.py --apply setelah publish Gold.';
    const body=$('#storage-table-body');body.innerHTML='';
    (data.relations||[]).forEach(r=>{
      const allowed=r.policy==='BACKUP_REVIEW'&&data.deletion_enabled;
      body.insertAdjacentHTML('beforeend',`<tr><td>${escapeHtml(r.relation)}</td><td>${readableBytes(r.total_bytes)}</td><td>${readableBytes(r.toast_bytes)}</td><td>${readableBytes(r.index_bytes)}</td><td>${fmtInt(r.estimated_rows)}</td><td>${fmtInt(r.dead_tuples)}</td><td>${escapeHtml(r.policy)}</td><td>${allowed?`<button class="table-action" data-storage-backup="${escapeHtml(r.relation)}">Review Delete</button>`:'Protected'}</td></tr>`);
    });
    $$('[data-storage-backup]').forEach(b=>b.onclick=()=>{$('#storage-delete-relation').value=b.dataset.storageBackup;document.getElementById('storage-delete-relation').scrollIntoView({behavior:'smooth',block:'center'});});
  }catch(e){toast(e.message,true)}finally{if(btn)btn.disabled=false}
}
async function previewStorageDelete(){
  const relation=$('#storage-delete-relation').value;
  const token=$('#storage-admin-token').value;
  const msg=$('#storage-delete-message');
  if(!relation||!token||!$('#storage-backup-verified').checked){toast('Pilih relation, isi token dan cek konfirmasi backup.',true);return}
  try{
    const result=await jsonFetch('/api/storage/delete-preview',{
      method:'POST',headers:{'Content-Type':'application/json','X-Storage-Admin-Token':token},
      body:JSON.stringify({relation,backup_verified:true})
    });
    msg.textContent=`PREVIEW ${relation}, ${readableBytes(result.total_bytes)}. ${result.warning}`;
    const typed=prompt(`PERINGATAN: DROP PERMANEN ${relation}. Ketik ulang nama tabel lengkap untuk konfirmasi:`);
    if(typed!==relation){toast('Penghapusan dibatalkan. Nama tidak sesuai.',true);return}
    if(!confirm(`Konfirmasi akhir: HAPUS PERMANEN ${relation}? Tidak bisa undo.`))return;
    const done=await jsonFetch('/api/storage/delete-confirm',{
      method:'POST',headers:{'Content-Type':'application/json','X-Storage-Admin-Token':token},
      body:JSON.stringify({relation,typed_name:typed,preview_token:result.preview_token})
    });
    msg.textContent=`DROP berhasil: ${done.deleted}. Refresh ukuran DB untuk memverifikasi.`;
    $('#storage-admin-token').value='';$('#storage-backup-verified').checked=false;
    await loadPgStorage();
  }catch(e){msg.textContent=e.message;toast(e.message,true)}
}

$$('.nav-item').forEach(b=>b.onclick=()=>{document.getElementById(b.dataset.scroll).scrollIntoView({behavior:'smooth'});if(b.dataset.scroll==='storage')loadPgStorage();$$('.nav-item').forEach(x=>x.classList.remove('active'));b.classList.add('active')});
$('#pipeline-name').onchange=()=>{const v=$('#pipeline-name').value;const map={MONTHLY_FULL:'Urutan: Reused Photo semua PT → Panen vs Transport PWP/DIN → Align/verify KPI → report.',DATA_AND_REUSED:'Reused Photo semua PT: sync metadata bulan target + register gambar NAS + embed new only + match historical + validate + publish.',TRANSPORT_ONLY:'Khusus index Panen, pull Foto Transport, embedding, dan similarity.',REPORT_ONLY:'Tidak memproses data. Hanya generate report dari data yang sudah tersedia.'};$('#run-summary').textContent=map[v]||''};
setInterval(()=>{$('#clock').textContent=new Date().toLocaleTimeString('id-ID')},1000);setInterval(refresh,3500);syncVisualAuditCompanyOptions();refresh(); // Visual Audit hanya dimuat atas permintaan agar membuka Control Center/PG Storage tidak memicu query Gold berat.
