const escape = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const tr = (locale, zh, en) => locale === 'zh' ? zh : en;

export const diagramNames = ['analysis', 'architecture', 'engine', 'evolution'];
export function diagramTitle(name, locale) {
  return {
    analysis: tr(locale, '图驱动的数据分析流程', 'Graph-driven data analysis workflow'),
    architecture: tr(locale, '系统架构与调用关系', 'System architecture and request paths'),
    engine: tr(locale, 'Graph Engine 执行路径', 'Graph Engine execution path'),
    evolution: tr(locale, '自进化构图：候选迭代与经验回流', 'Self-evolving construction: iteration and experience'),
  }[name];
}

export function renderDiagram(name, locale, dark = false) {
  const color = dark
    ? { bg:'#111923', panel:'#192433', nested:'#202d40', line:'#3a4b62', text:'#e7eff9', muted:'#a7b7cc', accent:'#86b4ff', soft:'#213858', teal:'#71d5c0', tealBg:'#14382f' }
    : { bg:'#ffffff', panel:'#f5f8fc', nested:'#ffffff', line:'#d6e0ed', text:'#172b45', muted:'#647790', accent:'#2b64bc', soft:'#eaf2fe', teal:'#177d6a', tealBg:'#e9f6f1' };
  let shapes = '';
  const rect = (x,y,w,h,fill=color.panel,stroke=color.line,rx=10) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${rx}" fill="${fill}" stroke="${stroke}"/>`;
  const label = (x,y,value,{size=15,fill=color.text,weight=400,anchor='start'}={}) => `<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" font-weight="${weight}" text-anchor="${anchor}">${escape(value)}</text>`;
  const line = (d,fill=color.accent,dashed=false) => `<path d="${d}" fill="none" stroke="${fill}" stroke-width="1.8"${dashed?' stroke-dasharray="5 5"':''} marker-end="url(#${fill===color.teal?'teal':'arrow'})"/>`;
  const measure = (value,size) => [...value].reduce((width,c)=>width+(c.charCodeAt(0)>255?size:/[ilI.,:;| ]/.test(c)?size*0.3:/[MW@]/.test(c)?size*0.9:size*0.57),0);
  const wrap = (value,width,size=13) => {
    const tokens=value.match(/[A-Za-z0-9_./₀ᵢ-]+|[^A-Za-z0-9_./₀ᵢ-]/gu)||[];
    const lines=[]; let current='';
    for(const token of tokens) {
      if(current && measure(current+token,size)>width) {lines.push(current.trim());current=token.trimStart();}
      else current+=token;
    }
    if(current.trim()) lines.push(current.trim());
    return lines;
  };
  const node = (x,y,w,h,title,rows=[],accent=false) => {
    const titleSize=Math.min(17,Math.max(13,(w-36)/Math.max(1,measure(title,1))));
    const lines=rows.flatMap(row=>wrap(row,w-36));
    if(54+(lines.length-1)*18>h-10) throw new Error('Diagram text exceeds node: '+title);
    return rect(x,y,w,h,accent?color.soft:color.nested,accent?color.accent:color.line)
      +label(x+18,y+29,title,{size:titleSize,weight:650})
      +lines.map((row,i)=>label(x+18,y+54+i*18,row,{size:13,fill:color.muted})).join('');
  };
  const chip = (x,y,w,value,teal=false) => rect(x,y,w,32,teal?color.tealBg:color.soft,'none',5)+label(x+w/2,y+21,value,{size:13,weight:550,anchor:'middle',fill:teal?color.teal:color.accent});
  let height;
  if(name==='analysis') {
    height=742;
    shapes += node(88,24,584,88,tr(locale,'分析问题与数据上下文','Analysis question & data context'),[
      tr(locale,'明确目标、实体关系、指标含义与分析范围','Define the objective, relations, metric semantics, and scope')
    ],true);
    shapes += rect(28,148,704,390,color.panel);
    shapes += line('M380 112V128H153V170');
    shapes += node(48,176,210,160,tr(locale,'智能体编排','Analysis agent'),[
      tr(locale,'问题分解 · 技能流程','Analysis plan · Skills'),
      tr(locale,'选择图操作与工具','Choose graph operations'),
      tr(locale,'依据返回证据推进分析','Interpret returned evidence')
    ],true);
    shapes += node(502,176,210,160,'Graph Engine',[
      tr(locale,'图查询 · 结构算法','Queries · Algorithms'),
      tr(locale,'图学习 · 可视化','Graph ML · Visualization'),
      tr(locale,'在当前图上执行计算','Compute on the active graph')
    ],true);
    const points=[[324,223],[370,193],[428,224],[338,280],[400,291],[379,247]];
    for(const [a,b] of [[0,1],[1,2],[2,4],[4,3],[3,0],[0,5],[1,5],[2,5],[3,5],[4,5]]) {
      shapes += `<path d="M${points[a][0]} ${points[a][1]}L${points[b][0]} ${points[b][1]}" stroke="${color.accent}" stroke-width="1.8" opacity=".65"/>`;
    }
    for(const [i,[x,y]] of points.entries()) shapes += `<circle cx="${x}" cy="${y}" r="${i===5?12:8}" fill="${i===5?color.teal:color.soft}" stroke="${i===5?color.teal:color.accent}" stroke-width="2"/>`;
    shapes += line('M258 248H302')+line('M450 248H496');
    shapes += label(380,326,tr(locale,'会话活动图','Active session graph'),{size:16,weight:650,anchor:'middle'});
    shapes += label(380,348,tr(locale,'实体 · 关系 · 属性','Entities · Relations · Attributes'),{size:locale==='zh'?13:11,fill:color.muted,anchor:'middle'});
    shapes += line('M608 336V396');
    shapes += node(48,400,664,114,tr(locale,'结构证据与结果检查','Structural evidence & result checks'),[
      tr(locale,'节点与关系、路径与社区、指标或模型输出','Nodes and relations, paths and communities, metrics or model outputs'),
      tr(locale,'核对图版本、输入参数、适用范围与结果含义','Check graph version, parameters, scope, and result meaning')
    ]);
    shapes += line('M153 400V342',color.teal,true);
    shapes += label(166,370,tr(locale,'按证据调整下一步','Refine the next step'),{size:12,fill:color.teal});
    shapes += line('M380 514V568');
    shapes += node(88,574,584,88,tr(locale,'分析结论与可视化报告','Findings & visual reports'),[
      tr(locale,'结合图语义解释结果，给出依据与适用边界','Explain results using graph semantics, supporting evidence, and limitations')
    ],true);
    const footer=tr(locale,'技能组织分析流程；自进化构图为分析准备和优化图表示。','Skills organize analysis workflows; self-evolving construction prepares and optimizes graph representations.');
    shapes += wrap(footer,704,12).map((row,i)=>label(28,696+i*18,row,{size:12,fill:color.muted})).join('');
  } else if(name==='architecture') {
    height=568;
    shapes += label(28,32,tr(locale,'交互与编排','INTERACTION & ORCHESTRATION'),{size:12,weight:600,fill:color.muted});
    shapes += node(28,56,188,102,'Frontend',[tr(locale,'会话 · 对话 · 图视图','Sessions · Chat · Graph views')]);
    shapes += node(286,56,188,102,'Backend',[tr(locale,'身份 · 权限 · 公共 API','Identity · Access · Public API')]);
    shapes += node(544,56,188,166,'Agent Runtime',['Agno AgentOS',tr(locale,'技能 · 运行 · 审批','Skills · Runs · Approval'),tr(locale,'记忆 · 学习','Memory · Learning')],true);
    shapes += line('M216 102H282')+label(251,86,'HTTP / SSE',{anchor:'middle',size:11,fill:color.muted});
    shapes += line('M474 102H540')+label(509,86,'AgentOS',{anchor:'middle',size:11,fill:color.muted});
    shapes += line('M380 158V282')+label(394,247,'Graph API',{size:13,fill:color.accent});
    shapes += line('M586 222V254H492V282',color.teal,true)+label(513,244,'MCP',{size:13,fill:color.teal});
    shapes += label(28,255,tr(locale,'图数据与分析执行','GRAPH DATA & ANALYSIS EXECUTION'),{size:12,weight:600,fill:color.muted});
    shapes += rect(28,286,510,232,color.panel);
    shapes += label(48,320,'Graph Engine',{size:22,weight:650})+label(48,344,tr(locale,'HTTP 与 MCP 共享工具和数据定义','Shared tool and data contracts across HTTP and MCP'),{size:13,fill:color.muted});
    shapes += node(48,363,148,88,tr(locale,'构图智能体','Construction'),[tr(locale,'查询验收 · 策略经验','Validation · Experience')]);
    shapes += node(207,363,138,88,tr(locale,'图分析工具','Graph tools'),[tr(locale,'结构 · 路径 · 社区','Structure · Paths')]);
    shapes += node(356,363,162,88,'Graph ML',[tr(locale,'训练 · 推理 · 模型文件','Train · Infer · Artifacts')]);
    shapes += chip(48,468,470,tr(locale,'图状态 / 版本 / 后台作业 / 结果文件','Graph state / Revisions / Jobs / Artifacts'));
    shapes += line('M664 222V380')+label(681,299,tr(locale,'持久化','Persist'),{size:12,fill:color.muted});
    shapes += node(566,384,166,134,'PostgreSQL',[tr(locale,'会话 · 运行','Sessions · Runs'),tr(locale,'记忆 · 执行追踪','Memory · Trace'),'pgvector']);
    shapes += label(28,546,tr(locale,'实线：HTTP / 数据读写','Solid: HTTP / data access'),{size:12,fill:color.muted});
    shapes += label(389,546,tr(locale,'虚线：智能体工具调用（MCP）','Dashed: agent tool calls over MCP'),{size:12,fill:color.teal});
  } else if(name==='engine') {
    height=620;
    shapes += node(28,24,216,82,'HTTP adapter',[tr(locale,'Backend 的图数据请求','Graph requests from Backend')]);
    shapes += node(516,24,216,82,'MCP adapter',[tr(locale,'智能体的工具调用','Tool calls from the agent')]);
    shapes += line('M244 66H294')+line('M516 66H466');
    shapes += rect(298,40,164,52,color.soft,color.accent,8)+label(380,63,'Tool contract',{weight:650,anchor:'middle'})+label(380,82,'user · session · run',{size:12,anchor:'middle',fill:color.muted});
    shapes += line('M380 92V140');
    shapes += rect(28,144,704,90,color.panel);
    shapes += label(48,177,tr(locale,'资源调度与工具依赖','Resource admission & dependency scheduling'),{size:18,weight:650});
    shapes += label(48,207,tr(locale,'读写冲突 / 图版本 / 用户与会话隔离 / 有界并发','Read/write conflicts / Graph versions / Identity isolation / Bounded concurrency'),{size:13,fill:color.muted});
    shapes += line('M160 234V290')+line('M380 234V290')+line('M600 234V290');
    shapes += node(28,294,218,112,tr(locale,'图查询与算法','Graph algorithms'),['NetworkX · python-igraph',tr(locale,'版本缓存 · 多重边策略','Versioned caches · Edge policy')],true);
    shapes += node(271,294,218,112,tr(locale,'构图与优化','Construction'),['PK/FK · Query workload',tr(locale,'候选转换 · 等价性验收','Transforms · Equivalence')]);
    shapes += node(514,294,218,112,tr(locale,'训练与推理','Training & inference'),['PyTorch · Graph ML',tr(locale,'后台作业 · 模型文件','Background jobs · Artifacts')]);
    shapes += line('M136 406V454')+line('M380 406V454')+line('M622 406V454');
    shapes += rect(28,458,704,136,color.panel);
    shapes += label(48,490,tr(locale,'状态与结果','State & results'),{size:18,weight:650});
    shapes += chip(48,511,200,'GraphData / Revisions');
    shapes += chip(260,511,232,'ConstructionStore / Experience');
    shapes += chip(504,511,208,'Jobs / Model artifacts');
    shapes += label(48,573,tr(locale,'工具结果返回智能体；Graph Engine 管理图数据与结果文件','Tool results return to the agent; Graph Engine owns graphs and artifacts'),{size:13,fill:color.muted});
  } else if(name==='evolution') {
    height=1080;
    // One circular candidate loop, plus a separate cross-run experience return.
    shapes += node(28,24,704,88,tr(locale,'技能层 · graph_construction SKILL.md','Skill layer · graph_construction SKILL.md'),[
      tr(locale,'组织任务步骤、检查数据状态，并协调构图与结果加载','Organize task steps, inspect data state, and coordinate construction and loading')
    ],true);
    shapes += node(28,132,704,68,tr(locale,'SQL / CSV / TXT → 属性图基线','SQL / CSV / TXT → Baseline property graph'),[
      tr(locale,'保留记录、主外键与类型语义；固定本轮评测查询集','Preserve records, keys, and types; fix the evaluation workload for this run')
    ]);
    shapes += line('M380 200V218');

    const cx=380, cy=520, radius=244;
    const point = angle => {
      const rad=angle*Math.PI/180;
      return [cx+radius*Math.cos(rad),cy+radius*Math.sin(rad)].map(n=>n.toFixed(2)).join(' ');
    };
    shapes += `<circle cx="${cx}" cy="${cy}" r="${radius}" fill="none" stroke="${color.line}" stroke-width="7"/>`;
    for(const [from,to] of [[-56,-20],[20,58],[122,160],[200,238]]) {
      shapes += `<path d="M${point(from)} A${radius} ${radius} 0 0 1 ${point(to)}" fill="none" stroke="${color.accent}" stroke-width="3" stroke-linecap="round" marker-end="url(#arrow)"/>`;
    }

    // The invariant evaluation basis sits inside the loop, not on its feedback path.
    shapes += `<circle cx="380" cy="520" r="128" fill="${color.soft}" stroke="${color.line}"/>`;
    shapes += label(380,locale==='zh'?482:478,tr(locale,'自进化构图','Self-evolving'),{size:locale==='zh'?28:25,weight:650,anchor:'middle'});
    shapes += label(380,locale==='zh'?513:509,tr(locale,'本轮持续迭代','construction'),{size:locale==='zh'?16:23,weight:550,anchor:'middle',fill:color.accent});
    shapes += `<path d="M296 535H464" stroke="${color.line}" fill="none"/>`;
    shapes += label(380,562,tr(locale,'固定查询 · 独立对照','Fixed queries · Independent checks'),{size:locale==='zh'?13:11,anchor:'middle',fill:color.muted});
    shapes += label(380,585,tr(locale,'源表结果 ↔ 候选图结果','Source answers ↔ Graph answers'),{size:locale==='zh'?13:11,anchor:'middle',fill:color.muted});

    shapes += node(256,224,248,126,tr(locale,'01 选择策略','01 Choose a strategy'),[
      tr(locale,'领域、查询热点与历史经验','Domain, query hotspots, and experience'),
      tr(locale,'确定下一候选的优先级','Prioritize the next candidate')
    ],true);
    shapes += node(516,445,220,150,tr(locale,'02 构造候选','02 Build a candidate'),[
      tr(locale,'在当前图副本上变换','Transform a copy of the current graph'),
      tr(locale,'保留记录、字段与关系','Preserve records and relations'),
      tr(locale,'生成候选图','Produce a candidate graph')
    ]);
    shapes += node(256,704,248,142,tr(locale,'03 独立验收','03 Validate independently'),[
      tr(locale,'源数据完整性检查','Check source-data integrity'),
      tr(locale,'SQL 与图查询结果对照','Compare SQL and graph answers'),
      tr(locale,'正确性优先，再比较成本','Correctness first, then compare cost')
    ],true);
    shapes += node(24,445,220,150,tr(locale,'04 接受或拒绝','04 Accept or reject'),[
      tr(locale,'有收益：更新当前图','Gain: update the graph'),
      tr(locale,'未达标：保留当前图','Otherwise: keep the graph'),
      tr(locale,'反馈指导下一候选','Feedback guides the next choice')
    ]);

    // Exit uses the retained graph after the decision; finish is not publication.
    shapes += line('M134 595V866Q134 884 152 884H176Q190 884 190 902V906');
    shapes += label(147,872,tr(locale,'结束探索','End exploration'),{size:12,fill:color.muted});
    shapes += node(28,912,332,120,tr(locale,'最终复验与构图结果','Final validation & result'),[
      tr(locale,'对最终保留图再次独立验证','Independently revalidate the retained graph'),
      tr(locale,'通过后输出图与运行报告','On success, output the graph and run report')
    ]);
    shapes += node(428,912,304,120,tr(locale,'领域策略经验','Domain strategy experience'),[
      'accepted / no_gain / unsafe',
      tr(locale,'记录有效策略证据','Store eligible strategy evidence'),
      tr(locale,'调整后续任务候选优先级','Update priorities for later tasks')
    ]);
    shapes += line('M360 972H422',color.teal,true);
    shapes += label(394,953,tr(locale,'复验通过','Validated'),{size:11,anchor:'middle',fill:color.teal});
    shapes += line('M732 972Q748 972 748 956V230Q748 212 730 212H526Q514 212 514 224V268H509',color.teal,true);
    shapes += label(585,242,tr(locale,'经验用于下一轮构图','Reuse in the next construction run'),{size:locale==='zh'?12:10,fill:color.teal});

    shapes += label(28,1060,tr(locale,'实线圆环：本轮候选迭代','Solid circle: candidate iteration'),{size:12,fill:color.accent});
    shapes += label(386,1060,tr(locale,'虚线回流：跨任务经验复用','Dashed return: cross-run experience'),{size:12,fill:color.teal});
  } else throw new Error(`Unknown diagram: ${name}`);
  const description = name === 'analysis'
    ? tr(locale,'从用户问题和数据上下文出发，智能体组织图操作，Graph Engine在会话活动图上执行查询、算法和学习。返回的结构证据用于检查结果、调整下一步，并形成有依据的分析与报告。','Starting from the user question and data context, the agent organizes graph operations. Graph Engine executes queries, algorithms, and learning on the active session graph. Returned structural evidence supports result checks, further analysis, and findings with supporting reports.')
    : name === 'evolution'
    ? tr(locale,'顺时针圆环：选择策略、构造候选、独立验收、接受或拒绝，再返回策略选择。结束探索后最终复验，通过后输出图和报告、保存有效领域策略证据。虚线将经验返回后续构图任务。','Clockwise loop: choose a strategy, build a candidate, validate independently, accept or reject, and choose again. After exploration, validate the retained graph before producing outputs and retaining eligible strategy evidence. A dashed return carries experience into later construction runs.')
    : diagramTitle(name,locale);
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 760 ${height}" role="img" aria-labelledby="title desc"><title id="title">${escape(diagramTitle(name,locale))}</title><desc id="desc">${escape(description)}</desc><defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 1L8 5L0 9" fill="none" stroke="${color.accent}" stroke-width="1.5"/></marker><marker id="teal" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 1L8 5L0 9" fill="none" stroke="${color.teal}" stroke-width="1.5"/></marker></defs><rect width="760" height="${height}" fill="${color.bg}"/><g font-family="Inter,Segoe UI,Microsoft YaHei,Arial,sans-serif">${shapes}</g></svg>`;
}
