import {
  getCodec,
  isSchemaSupported,
  type ParsedDraft,
} from '@/lib/library/draft-codecs';
import {
  LIBRARY_ENTRY_TYPES,
  type LibraryEntryType,
} from '@/lib/library/navigation';

describe('draft-codecs', () => {
  describe('every type has a codec', () => {
    it.each(LIBRARY_ENTRY_TYPES.map((t) => [t]))(
      '%s has a registered codec',
      (entryType: string) => {
        const codec = getCodec(entryType as LibraryEntryType);
        expect(codec).toBeDefined();
        expect(codec.entryType).toBe(entryType);
        expect(codec.supportedSchemaVersion).toBe(1);
      }
    );
  });

  describe('schema version support', () => {
    it.each(LIBRARY_ENTRY_TYPES.map((t) => [t]))(
      '%s supports version 1 only',
      (entryType: string) => {
        expect(isSchemaSupported(entryType as LibraryEntryType, 1)).toBe(true);
        expect(isSchemaSupported(entryType as LibraryEntryType, 0)).toBe(false);
        expect(isSchemaSupported(entryType as LibraryEntryType, 2)).toBe(false);
        expect(isSchemaSupported(entryType as LibraryEntryType, 99)).toBe(
          false
        );
      }
    );
  });

  describe('unknown field preservation', () => {
    it.each(LIBRARY_ENTRY_TYPES.map((t) => [t]))(
      '%s preserves unknown fields through parse/serialize round-trip',
      (entryType: string) => {
        const codec = getCodec(entryType as LibraryEntryType);
        const raw = {
          display_name: 'Test',
          _future_field_alpha: 'preserve me',
          _future_field_beta: [1, 2, 3],
          _nested: { deep: { value: true } },
        };

        const parsed = codec.parse(raw);
        expect(parsed._unknown._future_field_alpha).toBe('preserve me');
        expect(parsed._unknown._future_field_beta).toEqual([1, 2, 3]);
        expect(parsed._unknown._nested).toEqual({ deep: { value: true } });

        const serialized = codec.serialize(parsed);
        expect(serialized._future_field_alpha).toBe('preserve me');
        expect(serialized._future_field_beta).toEqual([1, 2, 3]);
        expect(serialized._nested).toEqual({ deep: { value: true } });
        expect(serialized.display_name).toBe('Test');
      }
    );
  });

  describe('product codec', () => {
    const codec = getCodec('product');

    it('parses the fields `_validate_product` checks', () => {
      const raw = {
        name: 'Widget Pro',
        description: 'A widget',
        audience: 'developers',
        selling_points: ['fast', 'reliable'],
      };
      const parsed = codec.parse(raw);
      expect(parsed.fields).toEqual(raw);
      expect(Object.keys(parsed._unknown)).toHaveLength(0);
    });

    it('uses defaults for missing fields', () => {
      const parsed = codec.parse({});
      expect(parsed.fields.name).toBe('');
      expect(parsed.fields.selling_points).toEqual([]);
    });

    it('round-trips cleanly, keeping must_match and reference bindings', () => {
      const raw = {
        name: 'X',
        description: 'desc',
        audience: 'all',
        selling_points: ['a'],
        must_match: [{ key: 'logo', value: 'top-left' }],
        reference_bindings: [],
        _extension: { custom: true },
      };
      const serialized = codec.serialize(codec.parse(raw));
      expect(serialized).toEqual(raw);
    });
  });

  describe('person codec', () => {
    const codec = getCodec('person');

    it('parses known person fields', () => {
      const raw = {
        display_name: 'Jane Doe',
        person_type: 'licensed',
        age_expression: '25-34',
        prompt_fragments: ['friendly tone'],
        rights_regions: ['US', 'EU'],
      };
      const parsed = codec.parse(raw);
      expect(parsed.fields).toEqual(raw);
    });

    it('defaults person_type to fictional', () => {
      const parsed = codec.parse({});
      expect(parsed.fields.person_type).toBe('fictional');
    });
  });

  describe('dimension_template codec', () => {
    const codec = getCodec('dimension_template');

    it('parses the server shape: value objects, constraints and cardinality', () => {
      const raw = {
        dimensions: [
          {
            id: 'scene',
            name: '场景',
            values: [
              {
                id: '生活化',
                positive_fragments: ['daily life'],
                negative_fragments: [],
              },
              {
                id: '棚拍',
                positive_fragments: [],
                negative_fragments: ['outdoor'],
              },
            ],
          },
        ],
        constraints: [
          { type: 'max_occurrence', dimension_id: 'scene', max: 1 },
        ],
        combination_cardinality: 'one_per_dimension',
      };
      const parsed = codec.parse(raw);
      expect(parsed.fields.dimensions).toHaveLength(1);
      expect(parsed.fields.dimensions[0].values[0]).toEqual({
        id: '生活化',
        positive_fragments: ['daily life'],
        negative_fragments: [],
      });
      expect(parsed.fields.constraints).toEqual(raw.constraints);
      expect(Object.keys(parsed._unknown)).toHaveLength(0);
      expect(codec.serialize(parsed)).toEqual(raw);
    });

    it('defaults a new draft to the one cardinality the server accepts', () => {
      const parsed = codec.parse({});
      expect(parsed.fields.combination_cardinality).toBe('one_per_dimension');
      expect(parsed.fields.dimensions).toEqual([]);
      expect(parsed.fields.constraints).toEqual([]);
    });
  });

  describe('data_source codec', () => {
    const codec = getCodec('data_source');

    it('preserves config object', () => {
      const raw = {
        display_name: 'My Source',
        adapter_kind: 'postgres',
        execution_mode: 'real',
        config: { host: 'db.example.com', port: 5432 },
      };
      const parsed = codec.parse(raw);
      expect(parsed.fields.config).toEqual({
        host: 'db.example.com',
        port: 5432,
      });

      const serialized = codec.serialize(parsed);
      expect(serialized.config).toEqual({ host: 'db.example.com', port: 5432 });
    });
  });

  describe('complete replacement content', () => {
    it('serialize emits all known and unknown fields', () => {
      const codec = getCodec('prompt_policy');
      const raw = {
        display_name: 'Strict Policy',
        registered_tokens: ['brand_name'],
        locked_phrases: ['No refunds'],
        brand_tone: 'professional',
        _server_metadata: { internal: true },
      };

      const parsed = codec.parse(raw);
      parsed.fields.brand_tone = 'casual';

      const serialized = codec.serialize(parsed);
      expect(serialized.brand_tone).toBe('casual');
      expect(serialized._server_metadata).toEqual({ internal: true });
      expect(serialized.display_name).toBe('Strict Policy');
      expect(serialized.registered_tokens).toEqual(['brand_name']);
    });
  });

  describe('known fields take precedence over unknown on serialize', () => {
    it('known field value wins when _unknown has same key', () => {
      const codec = getCodec('product');
      const parsed: ParsedDraft = {
        fields: {
          display_name: 'Known',
          description: '',
          audience: '',
          selling_points: [],
        },
        _unknown: { display_name: 'Unknown' },
      };
      const serialized = codec.serialize(parsed);
      expect(serialized.display_name).toBe('Known');
    });
  });

  describe('empty content', () => {
    it.each(LIBRARY_ENTRY_TYPES.map((t) => [t]))(
      '%s handles empty object gracefully',
      (entryType: string) => {
        const codec = getCodec(entryType as LibraryEntryType);
        const parsed = codec.parse({});
        expect(parsed.fields).toBeDefined();
        expect(parsed._unknown).toEqual({});

        const serialized = codec.serialize(parsed);
        expect(typeof serialized).toBe('object');
        expect(serialized).not.toBeNull();
      }
    );
  });
});
