import openai from '@lobehub/icons-static-svg/icons/openai.svg';
import anthropic from '@lobehub/icons-static-svg/icons/anthropic.svg';
import gemini from '@lobehub/icons-static-svg/icons/gemini-color.svg';
import deepseek from '@lobehub/icons-static-svg/icons/deepseek-color.svg';
import bailian from '@lobehub/icons-static-svg/icons/bailian-color.svg';
import ollama from '@lobehub/icons-static-svg/icons/ollama.svg';
import { Icon } from './Icon';

const logos: Record<string, { src: string; color?: boolean }> = {
  openai: { src: openai }, anthropic: { src: anthropic }, google: { src: gemini, color: true },
  deepseek: { src: deepseek, color: true }, qwen: { src: bailian, color: true }, ollama: { src: ollama },
};
export function ProviderLogo({ icon, size = 22 }: { icon: string; size?: number }) {
  const logo = logos[icon];
  return <span className="provider-logo" style={{ width: size, height: size }} data-provider-logo={icon} aria-hidden="true">
    {logo ? logo.color ? <img src={logo.src} alt="" draggable={false} />
      : <span style={{ maskImage: `url("${logo.src}")`, WebkitMaskImage: `url("${logo.src}")` }} />
      : icon === 'zhizengzeng' ? <b className="provider-logo-initial">Z</b> : <Icon name="cube" size={size} />}
  </span>;
}
