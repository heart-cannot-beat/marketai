import type { ComponentType } from 'react';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { LibraryEntryType } from '@/lib/library/navigation';
import type { FormEntryType } from '@/lib/library/draft-codecs';
import { ProductDraftEditor } from './product/ProductDraftEditor';
import { DimensionTemplateDraftEditor } from './dimension-template/DimensionTemplateDraftEditor';
import { DataSourceDraftEditor } from './data-source/DataSourceDraftEditor';

export interface DraftFieldsEditorProps {
  session: UseDraftSessionResult;
}

/**
 * Form editors whose fields match the server's content validator for their
 * entry type (`app/library/contracts.py`, see the codec notes in
 * `lib/library/draft-codecs.ts`); the data source form offers exactly the
 * server's source types and execution modes (#396). An entry type absent
 * here is edited through `RawJsonDraftEditor` only, and its codec passes the
 * content through untouched (#21). The map is keyed by the codec's
 * `FormEntryType`, so a form and the codec fields it edits come and go
 * together; `__tests__/components/library/entry-types/entry-type-files.spec.ts`
 * keeps unmounted editors from accumulating under `entry-types/` again.
 */
const FORM_EDITORS: Record<
  FormEntryType,
  ComponentType<DraftFieldsEditorProps>
> = {
  product: ProductDraftEditor,
  dimension_template: DimensionTemplateDraftEditor,
  data_source: DataSourceDraftEditor,
};

export function getDraftFieldsEditor(
  entryType: LibraryEntryType
): ComponentType<DraftFieldsEditorProps> | undefined {
  return (
    FORM_EDITORS as Partial<
      Record<LibraryEntryType, ComponentType<DraftFieldsEditorProps>>
    >
  )[entryType];
}

export function hasDraftFieldsEditor(entryType: LibraryEntryType): boolean {
  return getDraftFieldsEditor(entryType) !== undefined;
}
