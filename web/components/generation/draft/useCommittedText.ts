import { useEffect, useState, type KeyboardEvent } from 'react';

/**
 * A text field that edits a local copy and commits on blur or Enter.
 *
 * Every `onUpdate` on the draft page is a CAS write of the whole draft;
 * binding an input straight to the saved value made each keystroke a save
 * whose response re-rendered the field mid-typing, so characters typed
 * while a save was in flight were lost ("1024x1024" became "10"). The saved
 * value still wins whenever the field is not being edited.
 */
export function useCommittedText(
  saved: string,
  commit: (value: string) => void
) {
  const [text, setText] = useState(saved);
  const [editing, setEditing] = useState(false);
  useEffect(() => {
    if (!editing) setText(saved);
  }, [saved, editing]);
  const finish = () => {
    setEditing(false);
    if (text !== saved) commit(text);
  };
  return {
    value: text,
    onFocus: () => setEditing(true),
    onChange: (value: string) => {
      setEditing(true);
      setText(value);
    },
    onBlur: finish,
    onKeyDown: (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        finish();
      }
    },
  };
}
