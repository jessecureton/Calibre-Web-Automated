// Generates XPointer reference data for the position bridge's tests.
//
// This is our own script; it copies nothing from Readest. It runs inside a
// Readest clone so it can use Readest's CFI -> XPointer converter, which
// Readest pins against real CREngine (KOReader's engine). Only the JSON it
// writes is committed, to tests/fixtures/position_bridge/.
//
//   cp lab/readest-oracle/generate.test.ts <readest>/apps/readest-app/src/__tests__/
//   cd <readest>/apps/readest-app
//   ORACLE_EPUB=/abs/anna-karenina.epub ORACLE_OUT=/abs/anna-karenina-xpointers.json \
//     pnpm vitest run src/__tests__/generate.test.ts
//   rm src/__tests__/generate.test.ts
import { it } from 'vitest';
import { readFileSync, writeFileSync } from 'fs';
import { basename } from 'path';
import * as CFI from 'foliate-js/epubcfi.js';
import { DocumentLoader } from '@/libs/document';
import { XCFI } from '@/utils/xcfi';

const EVERY = 40;

it('writes XPointers for a sample of words', async () => {
  const epub = process.env['ORACLE_EPUB']!;
  const out = process.env['ORACLE_OUT']!;
  const file = new File([readFileSync(epub)], basename(epub), { type: 'application/epub+zip' });
  const { book } = await new DocumentLoader(file).open();

  const fragments = [];
  let seen = 0;
  for (const [index, section] of book.sections!.entries()) {
    const doc = await section.createDocument();
    const converter = new XCFI(doc, index);
    const base = section.cfi ?? CFI.fake.fromIndex(index);
    const words = [];
    const walker = doc.createTreeWalker(doc.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const parent = node.parentElement?.tagName.toLowerCase();
      if (parent === 'script' || parent === 'style') continue;
      for (const m of (node as Text).data.matchAll(/\S+/g)) {
        if (seen++ % EVERY !== 0) continue;
        const range = doc.createRange();
        range.setStart(node, m.index!);
        range.setEnd(node, m.index! + m[0].length);
        const back = converter.cfiToXPointer(CFI.joinIndir(base, CFI.fromRange(range)));
        words.push({ text: m[0], xp: back.pos0, xp_end: back.pos1 });
      }
    }
    fragments.push({ index, docfragment: index + 1, word_count: words.length, words });
  }
  const data = {
    epub: basename(epub),
    generator: 'lab/readest-oracle/generate.test.ts with Readest master 44c90152f',
    every: EVERY,
    fragments,
  };
  writeFileSync(out, JSON.stringify(data, null, 1));
}, 600000);
