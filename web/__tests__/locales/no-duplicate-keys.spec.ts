import fs from 'node:fs';
import path from 'node:path';

/**
 * `JSON.parse` keeps the *last* of two identical keys silently. The library
 * namespace once declared `"tabs.draft": "Draft"` and, further down, a
 * `"tabs.draft": { … }` key group for the panel's own strings: the label
 * vanished, and every grouped tab rendered as
 * `key 'tabs.draft (en)' returned an object instead of string.`
 *
 * A minimal JSON scanner that only tracks object nesting and member names
 * reports such duplicates with their path; it does not validate anything
 * else (the `JSON.parse` below does).
 */
function findDuplicateKeys(source: string): string[] {
  const duplicates: string[] = [];
  const stack: Array<{ kind: 'object' | 'array'; seen: Set<string> }> = [];
  const pathStack: string[] = [];
  let index = 0;

  const readString = (): string => {
    // `source[index]` is the opening quote.
    let out = '';
    index += 1;
    while (index < source.length) {
      const ch = source[index];
      if (ch === '\\') {
        out += source[index + 1];
        index += 2;
        continue;
      }
      if (ch === '"') {
        index += 1;
        return out;
      }
      out += ch;
      index += 1;
    }
    throw new Error('unterminated string');
  };

  let expectingKey = false;
  while (index < source.length) {
    const ch = source[index];
    if (ch === '{') {
      stack.push({ kind: 'object', seen: new Set() });
      expectingKey = true;
      index += 1;
    } else if (ch === '[') {
      stack.push({ kind: 'array', seen: new Set() });
      expectingKey = false;
      index += 1;
    } else if (ch === '}' || ch === ']') {
      stack.pop();
      if (ch === '}') pathStack.pop();
      expectingKey = false;
      index += 1;
    } else if (ch === ',') {
      expectingKey = stack[stack.length - 1]?.kind === 'object';
      if (expectingKey) pathStack.pop();
      index += 1;
    } else if (ch === '"') {
      const text = readString();
      if (expectingKey) {
        const frame = stack[stack.length - 1];
        const fullPath = [...pathStack, text].join('.');
        if (frame.seen.has(text)) duplicates.push(fullPath);
        frame.seen.add(text);
        pathStack.push(text);
        expectingKey = false;
      }
    } else {
      index += 1;
    }
  }
  return duplicates;
}

describe('locale namespaces', () => {
  const localesDir = path.join(__dirname, '..', '..', 'locales');
  const files: string[] = [];
  for (const language of fs.readdirSync(localesDir)) {
    const dir = path.join(localesDir, language);
    if (!fs.statSync(dir).isDirectory()) continue;
    for (const file of fs.readdirSync(dir)) {
      if (file.endsWith('.json')) files.push(path.join(dir, file));
    }
  }

  it('has at least one namespace to check', () => {
    expect(files.length).toBeGreaterThan(0);
  });

  it.each(files)('%s declares every key once', (file) => {
    const source = fs.readFileSync(file, 'utf8');
    JSON.parse(source);
    expect(findDuplicateKeys(source)).toEqual([]);
  });

  it('the scanner reports a duplicated member with its path', () => {
    expect(
      findDuplicateKeys(
        '{"tabs": {"draft": "Draft", "x": 1, "draft": {"a": 1}}}'
      )
    ).toEqual(['tabs.draft']);
    expect(findDuplicateKeys('{"a": {"b": 1}, "c": {"b": 2}}')).toEqual([]);
  });
});
