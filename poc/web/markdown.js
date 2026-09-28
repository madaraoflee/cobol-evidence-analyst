'use strict';

// The bundled parser runs offline. Raw model HTML and image URLs are never loaded.
const answerMarkdown = (() => {
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));

  function body(value) {
    let text = String(value ?? '').replace(/\r\n?/g, '\n').trim();
    // Older saved replies may contain a plain transport envelope. Explicit JSON
    // code blocks remain examples, even when they happen to contain an answer key.
    for (let depth = 0; depth < 3; depth++) {
      if (text.startsWith('{') && text.endsWith('}')) {
        try {
          const envelope = JSON.parse(text);
          const answer = envelope.narrative?.text ?? envelope.answer ?? envelope.markdown;
          if (typeof answer === 'string' && answer.trim()) {
            text = answer.trim();
            continue;
          }
        } catch { /* Ordinary prose and incomplete replies remain readable. */ }
      }
      break;
    }
    // Some replies reuse the outer marker for language-labelled inner examples.
    // An outer closing fence before the last line still means separate examples.
    const lines = text.split('\n');
    const opening = lines[0].match(/^ {0,3}(`{3,}|~{3,})(?:markdown|md)[ \t]*$/i);
    if (opening && lines.length >= 3) {
      const closingFor = marker => new RegExp(`^ {0,3}${marker[0]}{${marker.length},}[ \\t]*$`);
      const closing = closingFor(opening[1]);
      let innerClosing = null;
      for (let index = 1; index < lines.length; index++) {
        const line = lines[index];
        if (innerClosing) {
          if (innerClosing.test(line)) innerClosing = null;
          continue;
        }
        const inner = line.match(/^ {0,3}(`{3,}|~{3,})[ \t]*([A-Za-z][^`~\r\n]*)$/);
        if (inner) {
          innerClosing = closingFor(inner[1]);
          continue;
        }
        if (closing.test(line)) return index === lines.length - 1 ? lines.slice(1, index).join('\n').trim() : text;
      }
    }
    return text;
  }

  function render(value, reference = () => null) {
    const text = body(value);
    if (!text) return '';
    try {
      const base = new marked.Renderer();
      const parser = new marked.Marked({
        gfm: true,
        breaks: true,
        tokenizer: {
          html() { return undefined; },
          tag() { return undefined; },
        },
        renderer: {
          html(token) { return escape(token.text); },
          heading(token) {
            const level = Math.min(6, token.depth + 1);
            return `<h${level}>${this.parser.parseInline(token.tokens)}</h${level}>\n`;
          },
          code(token) {
            const language = (token.lang || '').match(/^[a-z0-9_+-]{1,32}(?=\s|$)/i)?.[0] || '';
            return `<div class="markdown-code-block">${language ? `<div class="markdown-code-language">${escape(language)}</div>` : ''}<pre><code>${escape(token.text)}</code></pre></div>\n`;
          },
          link(token) {
            const label = this.parser.parseInline(token.tokens);
            // Explicit web links only; protocol-relative, local and executable links stay text.
            if (!/^https?:\/\//i.test(token.href) || /[\u0000-\u0020\u007f\\]/.test(token.href)) return label;
            return `<a href="${escape(token.href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
          },
          image(token) { return escape(token.text); },
          table(token) {
            return `<div class="markdown-table" tabindex="0">${base.table.call(this, token)}</div>\n`;
          },
        },
        extensions: [{
          name: 'answerReference',
          level: 'inline',
          start(source) { return source.indexOf('['); },
          tokenizer(source) {
            const match = source.match(/^\[([^\[\]\r\n]{1,256})\](?!\()/);
            if (!match) return;
            const html = reference(match[1]);
            if (html) return {type: 'answerReference', raw: match[0], html};
          },
          renderer(token) { return token.html; },
        }],
      });
      return parser.parse(text);
    } catch {
      // Formatting must never prevent a returned answer from being displayed.
      return `<p>${escape(text).replace(/\n/g, '<br>')}</p>`;
    }
  }
  return {render};
})();
