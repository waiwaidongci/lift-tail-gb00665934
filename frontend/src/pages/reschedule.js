import {api} from '../api/client.js';
import {empty,errorSlot} from '../components/common/layout.js';
import {table} from '../components/common/table.js';
import {states} from '../constants/enums.js';
import {bindForm} from '../hooks/forms.js';
import {state} from '../stores/session.js';
import {esc,time} from '../utils/html.js';

const clockInput = value => {
  if (!value) return '';
  const part = value.slice(-5);
  return /^\d{2}:\d{2}$/.test(part) ? part : '';
};

function outageRows(outageBox) {
  return outageBox.outages.map(item => ({
    编号: item.id,
    货梯: item.elevator_id,
    停运开始: item.start_at,
    停运结束: item.end_at,
  }));
}

function outageEditor(outageBox, dates) {
  const rows = outageBox.outages.length
    ? outageBox.outages
    : [{elevator_id: '', start_at: '19:00', end_at: '19:30'}];
  return `<form id="outage-form" class="inline-form" data-version="${outageBox.scope_version}">
    <p class="hint">整组保存所选日期的停运时段（HH:MM，限 18:00–22:00）；保存后版本号 +1，未完成的旧重排自动作废。</p>
    <div id="outage-rows">${rows.map((item,index) => `
      <div class="outage-row" data-index="${index}">
        <label>货梯<input name="elevator_id" value="${esc(item.elevator_id)}" maxlength="40" required placeholder="B-HL01"></label>
        <label>停运开始<input name="start_at" type="time" min="18:00" max="22:00" value="${esc(clockInput(item.start_at))}" required></label>
        <label>停运结束<input name="end_at" type="time" min="18:00" max="22:00" value="${esc(clockInput(item.end_at))}" required></label>
        <button type="button" class="secondary outage-remove">删除</button>
      </div>`).join('')}
    </div>
    <p><button type="button" id="outage-add" class="secondary">新增停运时段</button>
    <button type="submit">保存停运时段并重算基础</button></p>${errorSlot}
  </form>`;
}

function planView(plan) {
  const detailColumns = [
    {key:'task_id',label:'任务编号'},
    {key:'elevator_id',label:'货梯'},
    {key:'arrival_at',label:'到达时刻'},
    {key:'work_minutes',label:'实际搬运（分钟）'},
    {key:'planned_start_at',label:'新开工'},
    {key:'planned_end_at',label:'新完工'},
    {key:'is_over_limit',label:'超阈值'},
    {key:'scheduled',label:'是否排入'},
    {key:'unscheduled_reason',label:'未排原因'},
  ];
  const details = plan.details.map(row => ({
    ...row,
    is_over_limit: row.is_over_limit ? '超限' : '阈值内',
    scheduled: row.scheduled ? '已排入' : '未排入',
  }));
  const metrics = Object.entries(plan.metrics);
  return `<div class="reading">
    <h2>${esc(plan.headline)}</h2>
    <dl class="metrics">${metrics.map(([key,value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>
    <h3>逐趟新安排与未排原因</h3>
    ${table(details,detailColumns)}
    <h3>按货梯汇总</h3>
    ${table(plan.elevator_schedules.map(item => ({
      货梯:item.elevator_id,
      签收趟数:item.trips,
      排入:item.scheduled,
      未排:item.unscheduled,
      停运时段:item.outages.map(window => `${window.start_at.slice(-5)}–${window.end_at.slice(-5)}`).join('、') || '无',
    })))}
    <h3>口径与限制</h3>
    <ul>${plan.notes.map(note => `<li>${esc(note)}</li>`).join('')}</ul>
  </div>`;
}

export async function reschedulePage() {
  const isAnalyst = state.account.role === 'analyst';
  const [datasets,jobs] = await Promise.all([api('/datasets'),api('/reschedule')]);
  if (!datasets.length) return empty('请先导入签收台账数据版本。','#1','查看数据台账');
  const datasetId = state.datasetId || datasets[0].id;
  const datesBox = await api(`/reschedule/dates/${datasetId}`);
  if (!datesBox.dates.length) return empty('该数据版本没有撤展卸货（18:00–22:00）记录。','#1','查看数据台账');
  const planDate = datesBox.dates.includes(state.planDate) ? state.planDate : datesBox.dates[datesBox.dates.length - 1];
  state.planDate = planDate;
  const outageBox = await api(`/reschedule/outages/${datasetId}/${planDate}`);

  const datasetPicker = `<label>数据版本<select id="pick-dataset">${datasets.map(row =>
    `<option value="${row.id}" ${row.id === datasetId ? 'selected' : ''}>${esc(row.name)} · #${row.id}</option>`).join('')}</select></label>`;
  const datePicker = `<label>重排日期<select id="pick-date">${datesBox.dates.map(date =>
    `<option value="${esc(date)}" ${date === planDate ? 'selected' : ''}>${esc(date)}</option>`).join('')}</select></label>`;

  const queueForm = isAnalyst ? `<form id="reschedule-form" class="inline-form">
      <label>固定超限阈值（分钟）<input name="threshold_minutes" type="number" step="any" min="1" max="480" value="30" required></label>
      <button type="submit">按当前停运时段重排</button>${errorSlot}
    </form>` : '<p class="hint">分析员可提交重排，本账号可查看结果。</p>';

  const scopedJobs = jobs.filter(job => job.dataset_id === datasetId && job.plan_date === planDate);
  const deepLinkId = state.runId && scopedJobs.some(job => job.id === state.runId) ? state.runId : null;
  const selectedId = deepLinkId
    ?? (state.rescheduleId && scopedJobs.some(job => job.id === state.rescheduleId)
      ? state.rescheduleId : (scopedJobs[0]?.id ?? null));
  state.rescheduleId = selectedId;
  const jobTable = table(scopedJobs.map(job => ({
    任务: {link:`#6/${job.id}`,text:`#${job.id}`},
    阈值: job.threshold_minutes,
    版本: `v${job.scope_version}`,
    状态: states[job.state] ?? job.state,
    提交时间: time(job.created),
    提交人: job.owner,
  })));

  let detail = '';
  if (selectedId) {
    const job = await api(`/reschedule/${selectedId}`);
    detail = job.result
      ? planView(job.result)
      : `<p ${job.state === 'queued' || job.state === 'running' ? 'data-poll' : ''} class="${job.error ? 'error' : ''}">${esc(job.error || states[job.state] || job.state)}</p>`;
    if (job.state === 'queued' || job.state === 'running') detail += '<p data-poll class="hint">重排计算中，页面自动刷新…</p>';
  }

  return `<section>
      <h2>停运时段与重排条件</h2>
      <div class="inline-form">${datasetPicker}${datePicker}</div>
      <p class="hint">当前停运时段版本 v${outageBox.scope_version} · ${outageBox.outages.length} 个停运段</p>
      ${isAnalyst ? outageEditor(outageBox,datesBox.dates) : table(outageRows(outageBox))}
      ${queueForm}
    </section>
    <section><h2>重排任务（${esc(planDate)}）</h2>${jobTable}</section>
    <section><h2>排班结果</h2>${detail || empty('提交重排后在这里逐趟查看新安排。')}</section>`;
}

function collectOutages(form) {
  return [...form.querySelectorAll('.outage-row')].map(row => ({
    elevator_id: row.elements.elevator_id.value.trim(),
    start_at: row.elements.start_at.value,
    end_at: row.elements.end_at.value,
  })).filter(item => item.elevator_id);
}

export function bindReschedule(root,refresh) {
  root.querySelector('#pick-dataset')?.addEventListener('change',event => {
    state.datasetId = Number(event.target.value);
    state.rescheduleId = null;
    refresh();
  });
  root.querySelector('#pick-date')?.addEventListener('change',event => {
    state.planDate = event.target.value;
    state.rescheduleId = null;
    refresh();
  });
  root.querySelector('#outage-add')?.addEventListener('click',() => {
    const wrap = root.querySelector('#outage-rows');
    const index = wrap.children.length;
    wrap.insertAdjacentHTML('beforeend',`<div class="outage-row" data-index="${index}">
      <label>货梯<input name="elevator_id" maxlength="40" required placeholder="B-HL01"></label>
      <label>停运开始<input name="start_at" type="time" min="18:00" max="22:00" value="19:00" required></label>
      <label>停运结束<input name="end_at" type="time" min="18:00" max="22:00" value="19:30" required></label>
      <button type="button" class="secondary outage-remove">删除</button></div>`);
  });
  root.querySelector('#outage-rows')?.addEventListener('click',event => {
    if (event.target.classList.contains('outage-remove')) {
      event.target.closest('.outage-row').remove();
    }
  });
  bindForm(root,'#outage-form',async form => {
    const datasetId = Number(root.querySelector('#pick-dataset').value);
    const planDate = root.querySelector('#pick-date').value;
    await api('/reschedule/outages',{
      dataset_id:datasetId, plan_date:planDate, outages:collectOutages(form),
    },'PUT');
    state.status = '停运时段已更新，旧版本重排已作废，可提交新重排。';
    state.rescheduleId = null;
  },refresh);
  bindForm(root,'#reschedule-form',async form => {
    const datasetId = Number(root.querySelector('#pick-dataset').value);
    const planDate = root.querySelector('#pick-date').value;
    const result = await api('/reschedule',{
      dataset_id:datasetId, plan_date:planDate,
      threshold_minutes:Number(form.elements.threshold_minutes.value),
    });
    state.rescheduleId = result.id;
    state.status = result.reused ? '同一版本参数已有重排任务，已直接复用，未新增排班。' : '重排任务已提交。';
  },refresh);
}
