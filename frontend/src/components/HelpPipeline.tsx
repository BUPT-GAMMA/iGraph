import { useState } from 'react';
import { useI18n } from '../i18n';

type SourceFormat = 'txt' | 'sql' | 'csv' | 'pt';

/** Keep the instructions aligned with CreateGraphWorkspace and GraphConstructionPanel. */
export function HelpPipeline() {
  const { t } = useI18n();
  const [format, setFormat] = useState<SourceFormat>('txt');
  const isText = format === 'txt';
  const isPt = format === 'pt';
  const labels = isPt
    ? [t('Upload', '上传'), t('Check source', '检查数据源'), t('Select graph', '选择图'), t('Analyze', '分析')]
    : [t('Upload', '上传'), t('Configure', '配置'), t('Build', '构图'), t('Review & save', '检查与保存'), t('Select graph', '选择图'), t('Analyze', '分析')];
  const questions = isText ? [
    t('Summarize the node types, relationship types, and size of this graph. Check for isolated nodes.', '概述这张图的节点类型、关系类型和规模，并检查是否存在孤立节点。'),
    t('Find the ten most connected entities, explain how they are ranked, and visualize their relationships.', '找出连接最多的十个实体，说明排名依据，并展示它们的关系子图。'),
    t('Identify communities in this graph and summarize their representative entities using graph evidence.', '识别图中的社区，依据图数据概括各社区的代表实体。'),
  ] : isPt ? [
    t('Inspect this graph’s node count, edge count, direction, and available attributes.', '检查这张图的节点数、边数、方向和可用属性。'),
    t('Find the ten highest-degree nodes and visualize their neighborhoods. Use node IDs if names are unavailable.', '找出度最高的十个节点并展示它们的邻域；没有名称时使用节点 ID。'),
    t('Analyze connected components and communities, and explain the structural patterns supported by this graph.', '分析连通分量与社区，说明图数据支持的结构规律。'),
  ] : [
    t('Check the graph size, node types, and edge direction. Explain how the source tables are connected.', '检查图规模、节点类型和边方向，说明源表之间如何关联。'),
    t('Find the ten highest-degree nodes, show their source-table attributes, and explain what the ranking means.', '找出度最高的十个节点，展示源表属性，并解释排名的业务含义。'),
    t('Check connected components and isolated records. Visualize a representative connected subgraph.', '检查连通分量与孤立记录，并展示一个有代表性的连通子图。'),
  ];

  return <div className="help-pipeline">
    <header className="help-pipeline-intro">
      <span className="help-pipeline-kicker">{t('FROM FILE TO GRAPH ANALYSIS', '从文件到图分析')}</span>
      <h2>{isPt ? t('Upload a PT graph, then ask questions', '上传 PT 图，再开始提问') : t('Build a graph, then ask questions', '构建你的图，再开始提问')}</h2>
      <p>{isPt
        ? t('A PT graph already contains graph structure. Follow these four steps to upload and analyze it; no reconstruction or construction-model connection is needed.', 'PT 图文件已包含图结构。按以下四步上传并分析，无需重新构图，也无需配置构图模型连接。')
        : t('Choose your file format and follow the six steps below. Uploading preserves the source; save the graph draft before using it for analysis. Already have a PT graph? Select PT to follow the shorter upload workflow.', '选择文件类型，按以下六步完成操作。上传会保留源文件；构图生成草稿后，需要保存才能用于分析。已有 PT 图文件时，选择 PT 查看直接上传流程。')}</p>
    </header>
    <div className="help-format-picker" role="group" aria-label={t('Source file format', '源文件类型')}>
      {(['txt', 'sql', 'csv', 'pt'] as const).map(value => <button type="button" key={value} aria-pressed={format === value} onClick={() => setFormat(value)}>
        <strong>{value.toUpperCase()}</strong><span>{value === 'txt' ? t('Text & documents', '文本与文档') : value === 'sql' ? t('Relational data', '关系型数据') : value === 'pt' ? t('Ready-made graph', '现成图文件') : t('One or more tables', '单表或多表')}</span>
      </button>)}
    </div>
    <ol className="help-pipeline-map" aria-label={t('Pipeline overview', '流程概览')}>
      {labels.map((label, index) => <li key={index}><span>{index + 1}</span>{label}</li>)}
    </ol>
    <ol className="help-pipeline-steps" aria-label={t(`${format.toUpperCase()} to graph analysis`, `${format.toUpperCase()} 到图分析的操作步骤`)}>
      <li>
        <h3>{t('Prepare and upload your source', '准备并上传源文件')}</h3>
        <p>{t('Open “Create your graph” in the sidebar, then choose “Upload source” or “Choose source file”.', '在侧栏打开“创建你的图”，点击“上传数据源”或“选择源文件”。')}</p>
        {isPt ? <ul>
          <li>{t('Select one .pt file containing a single graph: a PyTorch Geometric Data object or a supported graph dictionary with edge_index and node data (such as num_nodes). Model checkpoint weights alone are not a graph.', '选择一个包含单图的 .pt 文件：PyTorch Geometric Data 对象，或包含 edge_index 和节点信息（如 num_nodes）的图字典。仅包含模型权重的 checkpoint 不是图数据。')}</li>
          <li>{t('Node IDs, names, features, and labels depend on what the file contains. Names and relationship meanings cannot be recovered from numeric IDs alone. Multi-graph classification collections use the separate graph-classification workflow.', '节点 ID、名称、特征与标签取决于文件内容；仅有数字 ID 时无法还原人物名称或关系语义。多图分类集合需要使用独立的图分类流程。')}</li>
        </ul> : isText ? <ul>
          <li>{t('Select one UTF-8 .txt file containing the text you want to analyze, such as a novel, report, or relationship description.', '选择一个 UTF-8 编码的 .txt 文件，可以是小说、报告或关系描述文本。')}</li>
          <li>{t('For natural-language text, use AI entity extraction in the next step. Automatic line mode alone does not identify semantic relationships.', '自然语言文本请在下一步选择智能实体抽取；仅使用自动逐行模式不会识别语义关系。')}</li>
        </ul> : format === 'sql' ? <ul>
          <li>{t('Select one .sql file containing SQLite-compatible CREATE TABLE and INSERT statements. Include both table definitions and row data, and declare primary and foreign keys.', '选择一个包含 SQLite 兼容 CREATE TABLE 与 INSERT 语句的 .sql 文件。需要同时包含表结构和记录，并声明主键、外键。')}</li>
          <li>{t('Convert MySQL or PostgreSQL-specific syntax before uploading. A query script or table definitions without records will not provide the data needed for analysis.', 'MySQL、PostgreSQL 等专有语法需先转换。仅有查询语句，或只有建表语句而没有记录，都不能提供分析所需的数据。')}</li>
        </ul> : <ul>
          <li>{t('Select one or more UTF-8, comma-separated .csv files together. Each file needs a non-empty, unique header row and the same number of columns in every record.', '可同时选择一个或多个 UTF-8、逗号分隔的 .csv 文件。每个文件应包含非空且不重复的表头，所有记录的列数应一致。')}</li>
          <li>{t('In “Configure primary and foreign keys”, set the dataset name, table names, and keys. Review the suggested keys; they are based only on column names.', '在“配置主键与外键”窗口填写数据集名称、表名和主外键。自动建议仅依据列名，需要人工核对。')}</li>
          <li>{t('A primary key uniquely identifies a row. Use “Add relationship” to link a source column to its target table and column, then click “Upload … tables”.', '主键用于唯一标识记录。点击“添加关系”，将来源列连接到目标表和目标列，再点击“上传 … 张表”。')}</li>
          <li><code>orders.customer_id → customers.customer_id</code>{t(' is an example foreign-key mapping. Check that the referenced values exist in the target table.', ' 是外键映射的例子；请确认引用值在目标表中存在。')}</li>
        </ul>}
        <p>{isPt ? t('After upload, the PT file appears in “My graph sources” with a “Start analysis” button.', '上传后，PT 文件会显示在“我的构图数据源”中，并提供“开始分析”按钮。') : t('The file appears in “My graph sources”. Open “Build graph” if the construction workspace is not already open. You can use “Rename dataset” to change its display name.', '文件会出现在“我的构图数据源”中。若未自动进入工作台，点击“构图”；可通过“重命名数据集”修改显示名称。')}</p>
      </li>
      {isPt ? <li>
        <h3>{t('Check the uploaded graph source', '确认已上传的图数据源')}</h3>
        <p>{t('Confirm the filename and PT badge in “My graph sources”. The upload is stored immediately and remains after refresh. PT imports do not produce a construction draft and do not need “Save construction result”.', '在“我的构图数据源”中确认文件名称和 PT 标记。文件上传后即存储，刷新后仍会保留。PT 导入不产生构图草稿，无需点击“保存构图结果”。')}</p>
        <p>{t('The graph loader validates the file when the analysis session loads it. If loading fails, check the supported PT structure, node count, and edge endpoints; do not rename an unrelated file to .pt.', '分析会话载入图时会校验文件。加载失败时，请检查 PT 结构、节点数量和边端点是否合法；不要将其他格式直接改名为 .pt。')}</p>
      </li> : <><li>
        <h3>{t('Choose the construction method and model', '选择构图方式与模型')}</h3>
        {isText ? <>
          <p>{t('Set “TXT processing mode” to “Extract entities and relationships with AI (LLM)”. Optionally fill “Extraction focus” and “Entity types to extract”, separating types with commas.', '将“TXT 处理方式”设为“智能抽取实体与关系（LLM）”。可填写“抽取目标”和“提取的实体类型”，类型之间用逗号分隔。')}</p>
          <p>{t('For a novel, “Novel character relationships” fills a character-focused preset. “Use Agent to refine the TXT graph” optionally checks and repairs extracted facts; its validation query count accepts 1–100.', '小说可点击“小说人物关系”应用人物抽取预设。可开启“使用 Agent refine TXT 构图”核对并修正抽取事实，验证查询数量范围为 1–100。')}</p>
          <p>{t('If the TXT is a table, choose its delimiter instead. “Force one record per line” preserves line records without inferring relationships.', '若 TXT 实际是表格，请选择对应分隔符。“强制逐行记录”会保留每行记录，不推断关系。')}</p>
        </> : <>
          <p>{t('Leave “Use Agent to optimize the graph” off to build directly from primary and foreign keys without a construction LLM. Turn it on to optimize and validate the graph with a model.', '关闭“使用 Agent 优化构图”时，直接按主外键构图，无需构图大模型；开启后，使用模型优化并验证图。')}</p>
          <p>{t('For Agent optimization, choose a domain category, describe the data and intended analysis, and set 1–100 validation queries. Review “Source schema · fields and PK/FK” against your original tables.', '开启 Agent 优化后，选择领域类别，描述数据与分析目标，并设置 1–100 条验证查询。对照原表核对“源数据 Schema · 字段与主外键”。')}</p>
        </>}
        <div className="help-pipeline-callout">
          <strong>{t('Configure the construction connection', '配置构图连接')}</strong>
          <p>{t('In “Construction model settings”, select a model or enter a custom model ID. For a new connection, enter an OpenAI-compatible API URL and API Key, then click “Save model settings” before starting.', '在“构图模型配置”中选择模型或填写自定义模型 ID。新连接需要填写 OpenAI 兼容的 API URL 和 API Key，再点击“保存模型配置”后开始构图。')}</p>
          <p>{t('URL and Key start blank. If a saved or server connection is shown as available, leaving them blank retains it. Construction settings and the analysis model in “Model services” are configured separately.', 'URL 和 Key 默认留空。若显示连接已配置或服务器已配置，留空会沿用该连接。构图配置与“模型服务”中的分析模型配置分别设置。')}</p>
        </div>
      </li>
      <li>
        <h3>{t('Start construction and monitor progress', '开始构图并查看进度')}</h3>
        <p>{t('Click “Start construction”. Watch the progress and validation results until the status becomes “Draft · Unsaved”. The job continues on the server if you leave this page; reopen it through “My graph sources” → “View progress”.', '点击“开始构图”，查看进度与验证结果，等待状态变为“草稿 · 待保存”。离开页面后任务仍在服务器运行，可从“我的构图数据源”→“查看进度”重新进入。')}</p>
        {isText && <p>{t('AI extraction sends the TXT content to the configured model in chunks. Longer texts may take more time; review the extracted facts before relying on the graph.', '智能抽取会将 TXT 内容分块发送给配置的模型。长文本可能需要更多时间，请在使用前核对抽取事实。')}</p>}
      </li>
      <li>
        <h3>{t('Review the draft and save the graph', '核对草稿并保存构图结果')}</h3>
        <p>{t('Check node and edge counts, types, direction, and sample properties against the source. Use the graph view and Graph Schema to inspect the result. If needed, apply graph optimization operations and review their previews.', '对照源文件检查节点数、边数、类型、方向和样本属性。可结合图视图与 Graph Schema 检查结果；需要调整时使用图数据优化操作，并检查预览。')}</p>
        <div className="help-pipeline-callout">
          <p>{t('To fill an extraction gap, choose “Add node” or “Add relationship” under graph data optimization. For a relationship, select its source and target, enter the type, and record the supporting text or source. Read the direction as “source is the [relationship] of target”. Click “Apply and preview”; use “Undo last step” if needed, then “Save to dataset”.', '发现抽取遗漏时，可在图数据优化中选择“添加节点”或“添加关系”。添加关系时选择起点与终点，填写类型和依据；方向按“起点是终点的某种关系”理解。点击“应用并预览”，需要时可“撤销上一步”，确认后点击“保存到数据集”。')}</p>
          <strong>{t('Save explicitly before analysis', '先手动保存，再进行分析')}</strong>
          <p>{t('Click “Save construction result” and wait for “Saved · Version …”. A draft is not yet an analysis-ready dataset. Later edits also need “Save to dataset”; previewing a change does not save it.', '点击“保存构图结果”，等待显示“已保存 · 版本 …”。草稿尚不能作为就绪数据集分析。后续修改也需要点击“保存到数据集”，仅预览不会保存修改。')}</p>
        </div>
      </li></>}
      <li>
        <h3>{t('Select the saved graph for a conversation', '将已保存的图用于会话')}</h3>
        {isPt && <p>{t('Click “Start analysis” on the PT source card to open a standalone task with that dataset selected. Confirm the filename beside the message composer before sending a question.', '在 PT 数据源卡片点击“开始分析”，打开已选中该数据集的独立任务。发送问题前，确认输入框旁显示了该数据集名称。')}</p>}
        <p>{t('Click “New task”, then the database / “Dataset” button in the message composer. For a standalone task, select the saved graph from “Personal datasets”. Confirm that its name appears beside the composer.', '点击“新建任务”，再点击输入框旁的数据库图标／“数据集”按钮。独立任务可在“个人数据集”中选择已保存的图，并确认输入框旁显示了该数据集名称。')}</p>
        {isPt ? <p>{t('To analyze a PT graph inside a project, open “Project datasets” → “Add dataset” and upload the PT file there, then select it in a conversation belonging to that project.', '若要在项目内分析 PT 图，请在“项目数据集”→“添加数据集”中上传该 PT 文件，再在该项目的会话中选中它。')}</p> : <>
        <p>{t('To work inside a project, open that project’s datasets, choose “Load constructed graph”, and click “Load into project”. This creates an independent copy. Start a conversation in that project and select the imported dataset.', '若在项目内分析，打开该项目的数据集面板，点击“加载已构图数据”，再点击“加载到项目”，生成独立副本。随后在该项目中新建会话并选择导入的数据集。')}</p>
        <p>{t('When the construction panel is opened from a conversation, a saved graph can also be attached with “Use in current conversation”.', '从会话中打开构图面板时，已保存的图也可通过“在当前会话使用”直接载入。')}</p>
        </>}
      </li>
      <li>
        <h3>{t('Choose an analysis model and ask a question', '选择分析模型并提问')}</h3>
        <p>{t('Open “Model services” to configure and test an analysis model connection if needed, then choose the model beside the message box. Start with a graph overview, then ask about specific entities, paths, rankings, or communities.', '如需配置分析连接，打开“模型服务”完成配置和测试，再在输入框旁选择模型。建议先问图概况，再问具体实体、路径、排名或社区。')}</p>
        <p>{t('Copy one of these questions to try with your selected graph:', '可复制以下问题，在选中的图上尝试：')}</p>
        <ul className="help-pipeline-questions">{questions.map(question => <li key={question}>{question}</li>)}</ul>
        <p>{t('Review tool results, graph evidence, and the answer together. New model replies follow the selected interface language. Replace names and IDs in follow-up questions with values from your own graph.', '结合工具结果、图中证据与回答进行判断。新回复会跟随当前界面语言。追问时，请使用自己图中实际存在的名称或 ID。')}</p>
      </li>
    </ol>
    <aside className="help-pipeline-shortcut">
      <h3>{t('Try a ready-made example first', '也可以先体验预置示例')}</h3>
      <p>{t('“New task” shows the financial ownership and Demi-Gods and Semi-Devils examples. Click “Use example graph” to load one, or select an example question to start immediately. No upload or reconstruction is needed.', '“新建任务”页面提供金融股权和“天龙八部”两个示例。点击“使用示例图”载入，或点击示例问题直接开始，无需重新上传或构图。')}</p>
    </aside>
    <details className="help-pipeline-troubleshooting">
      <summary>{t('If you get stuck', '遇到问题时')}</summary>
      <dl>
        {isPt ? <>
        <dt>{t('The PT file cannot be loaded', 'PT 文件无法加载')}</dt>
        <dd>{t('Check that this is a supported single-graph object, not model weights or a multi-graph collection. Invalid edges or inconsistent node fields must be corrected in the source file before uploading again.', '请确认文件是支持的单图对象，而非模型权重或多图集合。无效边、节点字段长度不一致等问题，需要修正源文件后重新上传。')}</dd>
        <dt>{t('The PT source is missing in a project', '项目中找不到 PT 数据源')}</dt>
        <dd>{t('“Create your graph” uploads to personal datasets. Use “Start analysis” for a standalone task, or upload the PT file through that project’s “Add dataset” button.', '“创建你的图”上传到个人数据集。可点击“开始分析”创建独立任务，或在目标项目中通过“添加数据集”上传 PT 文件。')}</dd>
        </> : <>
        <dt>{t('The build cannot start or fails', '无法开始构图或构图失败')}</dt>
        <dd>{t('Save any pending model settings and check the URL, Key, and model ID. For SQL, check the dialect and data statements; for CSV, check headers and key mappings. The source is retained, so inspect the error before choosing “Rebuild graph”.', '先保存尚未提交的模型配置，检查 URL、Key 和模型 ID。SQL 检查语法与数据语句，CSV 检查表头与主外键映射。源文件会保留，可先查看报错，再点击“重新构图”。')}</dd>
        <dt>{t('The graph is missing from the analysis picker', '分析时找不到已构的图')}</dt>
        <dd>{t('Confirm that you saved the construction result. A personal graph must be loaded into the target project before a conversation in that project can use it. Uploading a file or typing its name alone does not select it.', '确认已点击“保存构图结果”。在项目会话中使用个人图之前，需要先将图加载到该项目。仅上传文件或在问题中写出名称，不等于已选中数据集。')}</dd>
        <dt>{t('The graph has no relationships', '图中没有关系')}</dt>
        <dd>{t('For natural-language TXT, check that AI entity extraction is selected. For SQL or CSV, check foreign keys and matching values. Review the source and settings rather than assuming every table row should connect.', '自然语言 TXT 应选择智能实体抽取；SQL、CSV 应检查外键和引用值是否匹配。请核对源数据与设置，不应默认所有记录都存在连接。')}</dd>
        </>}
      </dl>
    </details>
  </div>;
}
