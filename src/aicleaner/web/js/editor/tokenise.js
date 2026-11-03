// Colouring the editor's text (feature 80): each language as an ordered list of rules, run as one
// regular expression so the earliest, first-listed match wins - a "#" inside a string is part of the
// string, a "$x" inside a comment part of the comment. Whole text at a time, so constructs that span
// lines (here-strings, block comments, triple quotes, heredocs, code fences) need no line state.

import { WORDS } from './languages.js';

const escape = (word) => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
const wordList = (list) => `\\b(?:${list.map(escape).join('|')})\\b`;

// [class, pattern] in priority order. Class names are tok-<class> in editor.css.
const RULES = {
  powershell: () => [
    ['comment', String.raw`<#[\s\S]*?(?:#>|$(?![\s\S]))`],
    ['comment', String.raw`#[^\n]*`],
    ['string', String.raw`@"[\s\S]*?\n"@|@'[\s\S]*?\n'@`],
    ['string', String.raw`"(?:[^"\x60]|\x60[\s\S])*"?|'(?:[^']|'')*'?`],
    ['variable', String.raw`\$(?:\{[^}\n]*\}|[A-Za-z_][\w]*(?::[\w]+)?|[$?^_])`],
    ['type', String.raw`\[[A-Za-z_][\w.]*(?:\[\])?\]`],
    ['builtin', String.raw`\b[A-Za-z]+-[A-Za-z][A-Za-z0-9]*\b`],
    ['operator', String.raw`(?<![\w$])-[A-Za-z][\w]*`],
    ['keyword', wordList(WORDS.powershell.keywords)],
    ['number', String.raw`\b(?:0x[0-9a-f]+|\d+(?:\.\d+)?(?:[kmgtp]b)?)\b`],
  ],
  terraform: () => [
    ['comment', String.raw`#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$(?![\s\S]))`],
    ['string', String.raw`<<-?(?<heredoc>[A-Za-z_]\w*)\n[\s\S]*?\n[ \t]*\k<heredoc>\b`],
    ['string', String.raw`"(?:[^"\\\n]|\\.)*"?`],
    ['variable', String.raw`\b(?:var|local|module|data|each|count|path|self)\.[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*)*`],
    ['keyword', wordList(['resource', 'data', 'module', 'variable', 'output', 'locals', 'provider', 'terraform',
      'dynamic', 'moved', 'import', 'check', 'required_providers', 'backend', 'lifecycle', 'content',
      'provisioner', 'connection', 'true', 'false', 'null', 'for', 'in', 'if'])],
    ['builtin', String.raw`\b[a-z_][a-z0-9_]*(?=\()`],
    ['property', String.raw`\b[A-Za-z_][\w-]*(?=[ \t]*=(?!=))`],
    ['number', String.raw`\b\d+(?:\.\d+)?\b`],
  ],
  python: () => [
    ['string', String.raw`(?:\b[rRbBfFuU]{1,2})?(?:"""[\s\S]*?(?:"""|$(?![\s\S]))|'''[\s\S]*?(?:'''|$(?![\s\S])))`],
    ['string', String.raw`(?:\b[rRbBfFuU]{1,2})?(?:"(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?)`],
    ['comment', String.raw`#[^\n]*`],
    ['type', String.raw`^[ \t]*@[\w.]+`],
    ['variable', String.raw`\b(?:self|cls)\b`],
    ['keyword', wordList(WORDS.python.keywords.filter((w) => w !== 'self' && w !== 'cls'))],
    ['builtin', wordList(WORDS.python.builtins)],
    ['number', String.raw`\b(?:0[xob][0-9a-f_]+|\d[\d_]*(?:\.\d+)?(?:e[+-]?\d+)?j?)\b`],
  ],
  json: () => [
    ['comment', String.raw`\/\/[^\n]*|\/\*[\s\S]*?(?:\*\/|$(?![\s\S]))`],
    ['property', String.raw`"(?:[^"\\\n]|\\.)*"(?=[ \t]*:)`],
    ['string', String.raw`"(?:[^"\\\n]|\\.)*"?`],
    ['keyword', String.raw`\b(?:true|false|null)\b`],
    ['number', String.raw`-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b`],
  ],
  yaml: () => [
    ['comment', String.raw`(?:^|(?<=[ \t]))#[^\n]*`],
    ['keyword', String.raw`^(?:---|\.\.\.)[ \t]*$`],
    ['string', String.raw`"(?:[^"\\\n]|\\.)*"?|'(?:[^'\n]|'')*'?`],
    ['property', String.raw`[A-Za-z_$][\w$.\/-]*(?=:(?:[ \t]|$))`],
    ['variable', String.raw`[&*][\w-]+|\$\([\w.]+\)|\$\{\{[^}\n]*\}\}`],
    ['keyword', String.raw`\b(?:true|false|yes|no|null|on|off)\b`],
    ['number', String.raw`\b\d+(?:\.\d+)?\b`],
    ['punct', String.raw`^[ \t]*-(?=[ \t])`],
  ],
  markdown: () => [
    ['string', String.raw`^[ \t]*\x60\x60\x60[\s\S]*?(?:^[ \t]*\x60\x60\x60[ \t]*$|$(?![\s\S]))`],
    ['heading', String.raw`^#{1,6}[ \t][^\n]*`],
    ['comment', String.raw`^>[^\n]*`],
    ['string', String.raw`\x60[^\x60\n]+\x60`],
    ['variable', String.raw`!?\[[^\]\n]*\]\([^)\n]*\)`],
    ['keyword', String.raw`\*\*[^*\n]+\*\*|__[^_\n]+__|(?<![\w*])\*[^*\n]+\*(?!\*)|(?<![\w_])_[^_\n]+_(?![\w_])`],
    ['punct', String.raw`^[ \t]*(?:[-*+]|\d+\.)(?=[ \t])`],
  ],
};

const CASELESS = new Set(['powershell']);
const compiled = new Map();

function matcherFor(language) {
  if (!compiled.has(language)) {
    const rules = RULES[language]();
    const source = rules.map(([, pattern], i) => `(?<g${i}>${pattern})`).join('|');
    compiled.set(language, { rules, re: new RegExp(source, CASELESS.has(language) ? 'gmi' : 'gm') });
  }
  return compiled.get(language);
}

// [[class or null, text], ...] covering the whole text, in order.
export function tokenise(language, text) {
  if (!RULES[language]) return [[null, text]];
  const { rules, re } = matcherFor(language);
  const out = [];
  let at = 0;
  re.lastIndex = 0;
  for (let found = re.exec(text); found; found = re.exec(text)) {
    if (!found[0]) { re.lastIndex += 1; continue; }
    if (found.index > at) out.push([null, text.slice(at, found.index)]);
    const index = rules.findIndex((_, i) => found.groups[`g${i}`] !== undefined);
    out.push([rules[index][0], found[0]]);
    at = found.index + found[0].length;
  }
  if (at < text.length) out.push([null, text.slice(at)]);
  return out;
}
