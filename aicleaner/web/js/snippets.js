// Feature 116: code snippets are fenced blocks in the prompt, with what they are in the fence's info:
//   ```powershell snippet-3 file=installer/Deploy-AiCleaner.ps1 lines=16-40
//   ```text snippet-4 image=shot.png
// so what is seen is what is sent, and the transcript can number and label them. Pure helpers.

// The fence languages offered for a new snippet, and a file's extension to its fence language.
export const LANGUAGES = [
  ['text', 'Plain text'], ['powershell', 'PowerShell'], ['python', 'Python'], ['javascript', 'JavaScript'],
  ['typescript', 'TypeScript'], ['json', 'JSON'], ['yaml', 'YAML'], ['hcl', 'Terraform / HCL'],
  ['bash', 'Bash'], ['sql', 'SQL'], ['csharp', 'C#'], ['html', 'HTML'], ['css', 'CSS'], ['xml', 'XML'],
];
const BY_EXTENSION = {
  ps1: 'powershell', psm1: 'powershell', psd1: 'powershell', py: 'python', js: 'javascript', mjs: 'javascript',
  jsx: 'javascript', ts: 'typescript', tsx: 'typescript', json: 'json', jsonc: 'json', yaml: 'yaml', yml: 'yaml',
  tf: 'hcl', tfvars: 'hcl', hcl: 'hcl', sh: 'bash', bash: 'bash', sql: 'sql', kql: 'kusto', cs: 'csharp',
  html: 'html', htm: 'html', css: 'css', scss: 'scss', xml: 'xml', csproj: 'xml', md: 'markdown', bicep: 'bicep',
  bat: 'bat', cmd: 'bat', go: 'go', rs: 'rust', java: 'java', rb: 'ruby', php: 'php', ini: 'ini', toml: 'toml',
};
const LABELS = Object.fromEntries(LANGUAGES);

export const MAX_LINES = 5000;

export function languageForFile(name) {
  const match = /\.([A-Za-z0-9]+)$/.exec(String(name || ''));
  return (match && BY_EXTENSION[match[1].toLowerCase()]) || 'text';
}

export function languageLabel(lang) {
  return LABELS[lang] || (lang ? lang.charAt(0).toUpperCase() + lang.slice(1) : 'Plain text');
}

// The next snippet number after every one found in these texts (the transcript's prompts and the box).
export function nextSnippetNumber(texts) {
  let highest = 0;
  for (const text of texts || []) {
    for (const found of String(text || '').matchAll(/^ {0,3}`{3,}[^\n`]*\bsnippet-(\d+)\b/gm)) {
      highest = Math.max(highest, Number(found[1]));
    }
  }
  return highest + 1;
}

// A fence long enough for the code: one more backtick than the longest run at a line's start.
function fenceFor(code) {
  let longest = 2;
  for (const found of String(code).matchAll(/^ {0,3}(`{3,})/gm)) longest = Math.max(longest, found[1].length);
  return '`'.repeat(longest + 1);
}

// The snippet as prompt text. { code, lang, number, file, lines: [first, last], image }.
export function makeSnippet({ code, lang = 'text', number, file = '', lines = null, image = '' }) {
  const body = String(code || '').replace(/\r\n?/g, '\n').replace(/\n+$/, '');
  const fence = fenceFor(body);
  const info = [lang || 'text', `snippet-${number}`];
  if (file) info.push(`file=${file.replace(/\s/g, '%20')}`);
  if (lines) info.push(`lines=${lines[0]}-${lines[1]}`);
  if (image) info.push(`image=${image.replace(/\s/g, '%20')}`);
  const head = file && lines ? `From ${file}, lines ${lines[0]}-${lines[1]}:\n` : '';
  return `${head}${fence}${info.join(' ')}\n${body}\n${fence}`;
}

function infoOf(info) {
  const words = String(info || '').trim().split(/\s+/).filter(Boolean);
  const meta = { lang: 'text', number: 0, file: '', lines: null, image: '' };
  if (words.length && !words[0].includes('=') && !/^snippet-\d+$/.test(words[0])) meta.lang = words.shift();
  for (const word of words) {
    const numbered = /^snippet-(\d+)$/.exec(word);
    if (numbered) { meta.number = Number(numbered[1]); continue; }
    const [key, ...rest] = word.split('=');
    const value = rest.join('=').replace(/%20/g, ' ');
    if (key === 'file') meta.file = value;
    else if (key === 'image') meta.image = value;
    else if (key === 'lines') {
      const range = /^(\d+)-(\d+)$/.exec(value);
      if (range) meta.lines = [Number(range[1]), Number(range[2])];
    }
  }
  return meta;
}

// Every fenced block in a text: { start, end, codeStart, codeEnd, fence, ...meta, code }. A block
// with no closing fence runs to the end, as Markdown has it. `start` includes a "From ..., lines" line
// written just above a file snippet, so replacing a block replaces its heading with it.
export function findSnippets(text) {
  const value = String(text || '');
  const found = [];
  const opener = /^ {0,3}(`{3,})([^\n`]*)\n/gm;
  let match;
  while ((match = opener.exec(value)) !== null) {
    const fence = match[1];
    const codeStart = match.index + match[0].length;
    const closer = new RegExp(`^ {0,3}${fence}\`*[ \\t]*$`, 'gm');
    closer.lastIndex = codeStart;
    const close = closer.exec(value);
    const codeEnd = close ? Math.max(codeStart, close.index - 1) : value.length;
    const end = close ? close.index + close[0].length : value.length;
    let start = match.index;
    const before = value.slice(0, start);
    const heading = /(^|\n)(From [^\n]+, lines \d+-\d+:)\n$/.exec(before);
    const meta = infoOf(match[2]);
    if (heading && meta.file) start -= heading[2].length + 1;
    found.push({ start, end, codeStart, codeEnd, fence, ...meta, code: value.slice(codeStart, codeEnd) });
    opener.lastIndex = end;
  }
  return found;
}

// The snippet the caret is in (anywhere from its heading to its closing fence), or null.
export function snippetAt(text, caret) {
  return findSnippets(text).find((s) => caret >= s.start && caret <= s.end) || null;
}

// A prompt as text and snippet parts, for the transcript: [{ text } | { snippet }].
export function splitPrompt(text) {
  const value = String(text || '');
  const parts = [];
  let at = 0;
  for (const snippet of findSnippets(value)) {
    if (snippet.start > at) parts.push({ text: value.slice(at, snippet.start).replace(at ? /^\n/ : /$^/, '').replace(/\n$/, '') });
    parts.push({ snippet });
    at = snippet.end;
  }
  if (at < value.length) parts.push({ text: value.slice(at).replace(/^\n/, '') });
  return parts.filter((part) => part.snippet || part.text);
}

// The heading the transcript shows over a snippet.
export function snippetTitle(snippet) {
  const n = snippet.number ? `Snippet ${snippet.number}` : 'Code';
  if (snippet.file) {
    const base = snippet.file.split(/[\\/]/).pop();
    return snippet.lines ? `${n} - ${base}, lines ${snippet.lines[0]}-${snippet.lines[1]}` : `${n} - ${base}`;
  }
  if (snippet.image) return `${n} - text read from ${snippet.image}`;
  return `${n} (${languageLabel(snippet.lang)})`;
}
