import { useContext, useEffect, useRef, useState } from 'react';
import { useI18n } from '../i18n';
import { localizeUserCase, UserCaseContext } from '../userCases';
import './HelpUserCases.css';

const recordings = [
  {
    id: 'finance_risk', file: 'financial-risk',
    value: ['Follow ownership evidence from entity checks to control chains and look-through ownership, with the relevant nodes and paths visible in the graph.', '从主体核查、最大股东识别到控制链与穿透持股计算，沿原始股权证据逐步分析，并在图中查看相关节点与路径。'],
  },
  {
    id: 'borylation', file: 'molecular-graph',
    value: ['Connect molecular properties, ring systems, candidate borylation sites, and symmetry analysis to the same molecular structure.', '围绕同一张分子结构图，将性质判断、环系分析、硼化候选位点标注与对称等价分析串联起来，直观查看每一步的结构依据。'],
  },
  {
    id: 'tianlongbabu', file: 'demi-gods-and-semi-devils',
    value: ['Explore social circles, links between factions, and family origins through a character network, tracing the relationship paths behind each answer.', '从人物圈子出发，逐步追踪势力关联、人物身世与隐秘亲缘，将小说中的复杂联系展开为可查看的关系路径。'],
  },
] as const;

function CaseVideo({ file, name, captionId }: { file: string; name: string; captionId: string }) {
  const { t } = useI18n();
  const player = useRef<HTMLVideoElement>(null);
  const [failed, setFailed] = useState(false);
  const source = `/media/user-cases/${file}.mp4`;

  useEffect(() => {
    const video = player.current;
    return () => { video?.pause(); };
  }, []);

  return <figure className="help-case-video">
    <video ref={player} controls playsInline preload="none" tabIndex={0}
      poster={`/media/user-cases/${file}.jpg`} aria-labelledby={captionId}
      onError={() => setFailed(true)}>
      <source src={source} type="video/mp4" onError={() => setFailed(true)} />
      {t('Your browser cannot play this video. Use the link below to open it.', '当前浏览器无法播放视频，请通过下方链接打开。')}
    </video>
    {failed && <p role="alert" className="help-case-video-error">{t('The video could not be loaded. Try opening it with the link below or refresh the page.', '视频暂时无法加载，请尝试通过下方链接打开或刷新页面。')}</p>}
    <figcaption id={captionId}>
      <span>{name} · {t('Chinese-language walkthrough', '中文演示')}</span>
      <a href={source} target="_blank" rel="noopener noreferrer" aria-label={t(`Open video: ${name}`, `打开视频：${name}`)}>{t('Open video', '打开视频')} <span aria-hidden="true">↗</span></a>
    </figcaption>
  </figure>;
}

export function HelpUserCases() {
  const { t } = useI18n();
  const catalog = useContext(UserCaseContext);
  const sections = useRef<Record<string, HTMLElement | null>>({});
  const cases = recordings.flatMap(recording => {
    const item = catalog.find(example => example.id === recording.id);
    return item ? [{ ...recording, example: localizeUserCase(item, t) }] : [];
  });

  return <div className="help-user-cases" onPlay={event => {
    // Keep sound from different walkthroughs from overlapping.
    event.currentTarget.querySelectorAll('video').forEach(video => {
      if (video !== event.target && !video.paused) video.pause();
    });
  }}>
    <header className="help-cases-intro">
      <span className="help-pipeline-kicker">{t('WATCH & EXPLORE', '观看演示 · 亲手体验')}</span>
      <h2>{t('Three graphs, three ways to explore', '三个案例，体验图分析')}</h2>
      <p>{t('Each walkthrough follows four example questions. Watch the video, then choose the matching example on the home page to try it yourself. After each answer, press Tab in the empty message box to fill the next question.', '每个视频按顺序演示四个问题。观看后，在首页选择对应示例图即可亲手体验；每次回答完成后，在空白输入框按 Tab 可填入下一题。')}</p>
    </header>
    {cases.length > 0 ? <>
      <nav className="help-case-jumps" aria-label={t('Jump to a case', '跳转到案例')}>
        {cases.map(({ id, example }, index) => <button key={id} type="button" onClick={() => sections.current[id]?.scrollIntoView({ block: 'start' })}>
          <span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>{example.name}
        </button>)}
      </nav>
      {cases.map(({ id, file, value, example }, index) => <article className="help-user-case" key={id} ref={element => { sections.current[id] = element; }} aria-labelledby={`help-case-${id}`}>
        <header className="help-case-heading"><span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><h2 id={`help-case-${id}`}>{example.name}</h2></header>
        <p className="help-case-description">{example.description}</p>
        <h3>{t('Example questions', '应用案例')}</h3>
        <ol className="help-case-tasks">
          {example.questions.map((question, number) => <li key={number}><strong>{example.question_titles?.[number] || t(`Question ${number + 1}`, `问题 ${number + 1}`)}</strong><p>{question}</p></li>)}
        </ol>
        <aside className="help-case-value"><h3>{t('What this demonstrates', '应用价值')}</h3><p>{t(value[0], value[1])}</p></aside>
        <CaseVideo file={file} name={example.name} captionId={`help-case-video-${id}`} />
      </article>)}
    </> : <p className="help-empty" role="status">{t('The example catalog is not available yet. Reopen Help after the home-page examples have loaded.', '示例目录暂未加载，请在首页示例加载完成后重新打开帮助。')}</p>}
  </div>;
}
