import { useId, useRef, useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { Command } from 'cmdk';
import { Icon } from './Icon';
import { ProviderLogo } from './ProviderLogo';
import { useI18n } from '../i18n';
import './ProviderSelect.css';

type Option = { value: string; label: string; icon: string };
type Props = { label: string; value: string; options: Option[]; disabled?: boolean; onChange: (value: string) => void };

export function ProviderSelect({ label, value, options, disabled, onChange }: Props) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [highlighted, setHighlighted] = useState(value);
  const [search, setSearch] = useState('');
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  const selected = options.find(option => option.value === value);
  return <div className="pm-select-field">
    <label id={`${id}-label`} htmlFor={id}>{label}</label>
    <Popover.Root open={open} onOpenChange={next => { setOpen(next); setSearch(''); setHighlighted(value); }}>
      <Popover.Trigger asChild>
        <button id={id} type="button" className="provider-select-trigger" aria-label={label}
          aria-haspopup="dialog" data-provider-picker-trigger disabled={disabled}>
          <ProviderLogo icon={selected?.icon || 'custom'} size={21} />
          <span id={`${id}-value`}>{selected?.label || t('Select', '选择')}</span><Icon name="chevron" size={16} />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content className="provider-select-popup" data-provider-picker-popup sideOffset={6} collisionPadding={12}
          aria-label={label} onOpenAutoFocus={event => { event.preventDefault(); input.current?.focus(); }}>
          <Command loop value={highlighted} onValueChange={setHighlighted} label={label}>
            <div className="provider-select-search"><Icon name="search" size={16} />
              <Command.Input ref={input} value={search} onValueChange={next => {
                  setSearch(next);
                  const query = next.trim().toLocaleLowerCase();
                  const match = options.find(option => `${option.label} ${option.value}`.toLocaleLowerCase().includes(query));
                  // Keep Enter usable in the same frame as an input change.
                  setHighlighted(match?.value || '');
                }} placeholder={t('Search', '搜索')}
                aria-label={t(`Search ${label}`, `搜索${label}`)} />
            </div>
            <Command.List>
              <Command.Empty>{t('No results', '无匹配结果')}</Command.Empty>
              {options.map(option => <Command.Item key={option.value} value={option.value} keywords={[option.label]}
                onSelect={() => { onChange(option.value); setOpen(false); }}>
                <ProviderLogo icon={option.icon} size={22} /><span>{option.label}</span>
                {value === option.value && <Icon name="check" size={16} />}
              </Command.Item>)}
            </Command.List>
          </Command>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  </div>;
}
