import { useState, type FormEvent } from 'react';
import { useTranslation } from 'next-i18next';
import { PlusIcon } from '@heroicons/react/24/outline';
import {
  buildDirectionAdd,
  type DirectionAdd,
} from '@/lib/test-plans/plan-model';

interface AddIdeaFormProps {
  disabled: boolean;
  busy: boolean;
  onAdd: (add: DirectionAdd) => Promise<boolean>;
}

/** "加一条我的想法": a title and, optionally, the scene. */
export function AddIdeaForm({ disabled, busy, onAdd }: AddIdeaFormProps) {
  const { t } = useTranslation('tests');
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [scene, setScene] = useState('');

  if (!open) {
    return (
      <button
        type="button"
        className="btn btn-outline btn-sm w-full border-dashed sm:w-auto"
        disabled={disabled}
        onClick={() => setOpen(true)}
      >
        <PlusIcon className="h-4 w-4" aria-hidden="true" />
        {t('add-idea-open')}
      </button>
    );
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const add = buildDirectionAdd({ title, sceneDescription: scene });
    if (!add) return;
    if (await onAdd(add)) {
      setTitle('');
      setScene('');
      setOpen(false);
    }
  }

  return (
    <form
      className="space-y-3 rounded-box border border-dashed border-base-content/30 p-5"
      onSubmit={submit}
      aria-labelledby="add-idea-title"
    >
      <h3 id="add-idea-title" className="font-semibold">
        {t('add-idea-title')}
      </h3>
      <label className="form-control">
        <span className="label-text mb-1 text-sm">
          {t('add-idea-field-title')}
        </span>
        <input
          className="input input-bordered input-sm"
          value={title}
          maxLength={120}
          placeholder={t('add-idea-field-title-placeholder')}
          onChange={(event) => setTitle(event.target.value)}
          autoFocus
        />
      </label>
      <label className="form-control">
        <span className="label-text mb-1 text-sm">
          {t('add-idea-field-scene')}
        </span>
        <textarea
          className="textarea textarea-bordered text-sm"
          rows={2}
          value={scene}
          placeholder={t('add-idea-field-scene-placeholder')}
          onChange={(event) => setScene(event.target.value)}
        />
      </label>
      <div className="flex justify-end gap-2">
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          onClick={() => setOpen(false)}
          disabled={busy}
        >
          {t('add-idea-cancel')}
        </button>
        <button
          type="submit"
          className="btn btn-primary btn-sm"
          disabled={busy || !title.trim()}
        >
          {t('add-idea-submit')}
        </button>
      </div>
    </form>
  );
}
