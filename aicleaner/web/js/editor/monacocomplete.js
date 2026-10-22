// Feature 139: aicleaner's own suggestions in Monaco - the snippets and keywords feature 80 offered, and
// PowerShell's commands and parameters from this machine - beside Monaco's own word completion.
import { loadMachineCommands, machineNames, parametersOf } from './complete.js';
import { SNIPPETS, WORDS } from './languages.js';

// aicleaner's language names -> Monaco's.
const MONACO_ID = { powershell: 'powershell', python: 'python', terraform: 'hcl', json: 'json', yaml: 'yaml', markdown: 'markdown' };
let registered = false;

export function registerCompletions(monaco) {
  if (registered) return;
  registered = true;
  const Kind = monaco.languages.CompletionItemKind;
  const asSnippet = monaco.languages.CompletionItemInsertTextRule.InsertAsSnippet;
  for (const [ours, id] of Object.entries(MONACO_ID)) {
    const words = WORDS[ours] || {};
    const snippets = SNIPPETS[ours] || [];
    monaco.languages.registerCompletionItemProvider(id, {
      // PowerShell's parameters come after a space and a dash, and its $variables after a $.
      triggerCharacters: ours === 'powershell' ? ['-', '$'] : [],
      async provideCompletionItems(model, position) {
        const word = model.getWordUntilPosition(position);
        const line = model.getLineContent(position.lineNumber).slice(0, position.column - 1);
        // PowerShell words include their - and $: Verb-Noun, -Parameter, $variable.
        const lead = ours === 'powershell' ? (line.match(/[\w$:-]*$/) || [''])[0] : word.word;
        const range = new monaco.Range(position.lineNumber, position.column - lead.length, position.lineNumber, position.column);
        const items = [];
        const add = (label, kind, detail, sort, insertText = label, rules) => items.push({
          label, kind, detail, sortText: `${sort}${label.toLowerCase()}`, insertText, range,
          ...(rules ? { insertTextRules: rules } : {}),
        });
        for (const s of snippets) add(s.label, Kind.Snippet, 'snippet', '0', s.body, asSnippet);
        for (const w of words.keywords || []) add(w, Kind.Keyword, 'keyword', '1');
        for (const w of words.operators || []) add(w, Kind.Operator, 'operator', '1');
        for (const w of words.builtins || []) add(w, Kind.Function, 'built-in', '2');
        for (const w of words.variables || []) add(w, Kind.Variable, 'variable', '2');
        if (ours === 'powershell') {
          await loadMachineCommands();
          for (const name of machineNames() || []) add(name, Kind.Function, 'this machine', '2');
          // After a command and a dash: that command's parameters (asked of this machine once).
          const command = (line.match(/([A-Za-z]+-[A-Za-z]+)\s+(?:[^|;]*\s)?-[\w]*$/) || [])[1];
          if (command && lead.startsWith('-')) {
            for (const p of await parametersOf(command)) add(p, Kind.Property, `${command} parameter`, '0');
          }
        }
        return { suggestions: items };
      },
    });
  }
}
