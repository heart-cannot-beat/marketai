import fs from 'node:fs';
import path from 'node:path';
import type { Locator, Page } from '@playwright/test';

/** Where `shot` saves screenshots; unset, it saves none. */
const SHOTS = process.env.C31_SCREENSHOT_DIR;

/**
 * A 390×844 screenshot with `target` just below the page's sticky header
 * (the workspace bar and the round's focus bar).
 */
export async function shot(page: Page, name: string, target: Locator) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await target.evaluate((node) => {
    node.scrollIntoView({ block: 'start' });
    // Whatever the page pins over the top (workspace bar, round focus bar)
    // covers the target's first rows; scroll it back out from under them.
    const x = node.getBoundingClientRect().left + 4;
    let covered = 0;
    while (
      covered < window.innerHeight / 2 &&
      !node.contains(document.elementFromPoint(x, covered + 1))
    )
      covered += 4;
    let scroller = node.parentElement;
    while (
      scroller &&
      !(
        scroller.scrollHeight > scroller.clientHeight &&
        ['auto', 'scroll'].includes(getComputedStyle(scroller).overflowY)
      )
    )
      scroller = scroller.parentElement;
    (scroller ?? document.scrollingElement!).scrollBy(0, -covered - 8);
  });
  await page.screenshot({ path: path.join(SHOTS, `${name}.png`) });
}
