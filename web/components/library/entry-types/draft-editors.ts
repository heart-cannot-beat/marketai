import type { ComponentType } from 'react';
import type { UseDraftSessionResult } from '@/hooks/library/useDraftSession';
import type { LibraryEntryType } from '@/lib/library/navigation';
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
 * here is edited through `RawJsonDraftEditor`; the remaining editors under
 * `entry-types/` are sketches still carrying field names the server does not
 * read, so mounting them would let people "edit" content the validator
 * ignores.
 */
const FORM_EDITORS: Partial<
  Record<LibraryEntryType, ComponentType<DraftFieldsEditorProps>>
> = {
  product: ProductDraftEditor,
  dimension_template: DimensionTemplateDraftEditor,
  data_source: DataSourceDraftEditor,
};

export function getDraftFieldsEditor(
  entryType: LibraryEntryType
): ComponentType<DraftFieldsEditorProps> | undefined {
  return FORM_EDITORS[entryType];
}

export function hasDraftFieldsEditor(entryType: LibraryEntryType): boolean {
  return FORM_EDITORS[entryType] !== undefined;
}
