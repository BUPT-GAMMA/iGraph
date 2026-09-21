import { memo, useState } from 'react';
import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useI18n } from '../i18n';

function safeUrl(value: string, key: string): string | undefined {
  try {
    const url = new URL(value, 'https://markdown.invalid/');
    if (['http:', 'https:'].includes(url.protocol) || (key === 'href' && url.protocol === 'mailto:')) return value;
  } catch { /* Incomplete streamed URLs are not actionable yet. */ }
  return undefined;
}

function MarkdownImage({ src, alt, title }: { src?: string; alt?: string; title?: string }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  if (!src || failed) return <span className="markdown-image-fallback">{alt || t('Image', '图片')} · {t('Image unavailable', '图片不可用')}{src ? <> · <a href={src} target="_blank" rel="noopener noreferrer">{t('Open image', '打开图片')}</a></> : null}</span>;
  return <img src={src} alt={alt || ''} title={title} loading="lazy" referrerPolicy="no-referrer" onError={() => setFailed(true)} />;
}

const components: Components = {
  a: ({ href, title, children }) => href ? <a href={href} title={title} target={href.startsWith('#') ? undefined : '_blank'} rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
  img: ({ src, alt, title }) => <MarkdownImage key={src} src={src} alt={alt} title={title} />,
  table: ({ children }) => <div className="markdown-table-scroll"><table>{children}</table></div>,
};
const plugins = [remarkGfm];

/** CommonMark/GFM without raw HTML or executable URL schemes. */
export const MarkdownContent = memo(function MarkdownContent({ content }: { content: string }) {
  return <div className="markdown-content"><Markdown remarkPlugins={plugins} skipHtml urlTransform={safeUrl} components={components}>{content}</Markdown></div>;
});
