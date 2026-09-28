'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const web=path.join(__dirname,'../web');

function harness(){
  const context=vm.createContext({});
  for(const asset of ['marked.umd.js','markdown.js'])vm.runInContext(fs.readFileSync(path.join(web,asset),'utf8'),context);
  return {run:code=>vm.runInContext(code,context),render:text=>vm.runInContext(`answerMarkdown.render(${JSON.stringify(text)})`,context)};
}

test('business Markdown renders emphasis, nested lists, tables, blockquotes and code without a network runtime',()=>{
  const h=harness();
  const text='# 业务结论\n\n保费使用 **有效费率**，`RATE` 为输入，*按月* 处理。\n\n1. 读取保单\n2. 计算保费\n   - 基本保费\n   - 附加保费\n\n| 输入 | 作用 |\n| :--- | ---: |\n| `RATE` | **有效费率** |\n\n> 保留业务条件。\n\n```cobol\nCOMPUTE AMOUNT = BASE * RATE\n```';
  const html=h.render(text);
  assert.match(html,/<h2>业务结论<\/h2>/);assert.match(html,/<strong>有效费率<\/strong>/);
  assert.match(html,/<code>RATE<\/code>/);assert.match(html,/<em>按月<\/em>/);
  assert.match(html,/<ol>[\s\S]*<li>计算保费[\s\S]*<ul>/);assert.match(html,/<blockquote>/);
  assert.match(html,/<div class="markdown-table" tabindex="0"><table>/);assert.match(html,/<th align="right">作用<\/th>/);
  assert.match(html,/<div class="markdown-code-language">cobol<\/div>/);assert.match(html,/<pre><code>COMPUTE AMOUNT = BASE \* RATE<\/code><\/pre>/);
  assert.doesNotMatch(html,/\*\*|```/);
});

test('whole-answer Markdown wrappers and known saved answer envelopes are readable while real JSON examples stay code',()=>{
  const h=harness();
  for(const text of ['```markdown\n## 计算方法\n\n**计算结果**\n```','~~~md\n## 计算方法\n\n**计算结果**\n~~~',JSON.stringify({answer:'## 计算方法\n\n**计算结果**'}),JSON.stringify({narrative:{text:'## 计算方法\n\n**计算结果**'}})]){
    const html=h.render(text);assert.match(html,/<h3>计算方法<\/h3>/);assert.match(html,/<strong>计算结果<\/strong>/);assert.doesNotMatch(html,/markdown-code-block|```|"answer"|"narrative"/);
  }
  const example=h.render('返回示例：\n\n```json\n{"amount": 125, "status": "accepted"}\n```');
  assert.match(example,/markdown-code-language">json/);assert.match(example,/&quot;amount&quot;: 125/);
});

test('separate Markdown examples and explicit JSON answer examples keep their original code boundaries',()=>{
  const h=harness();
  for(const marker of ['```','~~~~']){
    const html=h.render(`${marker}md\n**第一个例子**\n${marker}\n\n两个写法：\n\n${marker}markdown\n**第二个例子**\n${marker}`);
    assert.equal((html.match(/markdown-code-block/g)||[]).length,2);
    assert.match(html,/\*\*第一个例子\*\*/);assert.match(html,/\*\*第二个例子\*\*/);assert.doesNotMatch(html,/<strong>/);
  }
  for(const envelope of [{answer:'**示例值**'},{narrative:{text:'**示例值**'}}]){
    const html=h.render('```json\n'+JSON.stringify(envelope)+'\n```');
    assert.match(html,/markdown-code-language">json/);assert.match(html,/\*\*示例值\*\*/);assert.doesNotMatch(html,/<strong>/);
  }
  const nested=h.render('````markdown\n**业务结论**\n\n```cobol\nCOMPUTE TOTAL = BASE * RATE.\n```\n````');
  assert.match(nested,/<strong>业务结论<\/strong>/);assert.equal((nested.match(/markdown-code-block/g)||[]).length,1);assert.match(nested,/markdown-code-language">cobol/);
});

test('provider Markdown wrappers tolerate language-labelled inner code fences using the same marker',()=>{
  const h=harness();
  for(const marker of ['```','~~~~']){
    const html=h.render(`${marker}markdown\n**先检查保单状态。**\n\n${marker}cobol\nIF REQUEST-STATE = 'A'\n    COMPUTE TOTAL = BASE * RATE\nEND-IF\n${marker}\n\n再返回计算结果。\n${marker}`);
    assert.match(html,/<strong>先检查保单状态。<\/strong>/);assert.match(html,/<p>再返回计算结果。<\/p>/);
    assert.equal((html.match(/markdown-code-block/g)||[]).length,1);assert.match(html,/markdown-code-language">cobol/);
    assert.match(html,/    COMPUTE TOTAL = BASE \* RATE/);assert.doesNotMatch(html,/markdown-code-language">markdown|\*\*先检查/);
  }
  const mixed=h.render('```markdown\n**结论**\n\n~~~md\n**只是示例**\n~~~\n```');
  assert.match(mixed,/<strong>结论<\/strong>/);assert.match(mixed,/<pre><code>\*\*只是示例\*\*<\/code>/);
  const incomplete=h.render('```markdown\n**结论**\n\n```cobol\nCOMPUTE TOTAL = BASE * RATE\n```');
  assert.match(incomplete,/markdown-code-language">markdown/);assert.doesNotMatch(incomplete,/<strong>结论<\/strong>/);
});

test('HTML, scripts, images and executable or local links remain inert even when returned in nested Markdown',()=>{
  const h=harness();
  const unsafe=[
    '<script>alert(1)</script> **仍然可读**',
    '<img src=x onerror=alert(1)>',
    '<svg><a onmouseover="alert(1)">link</a></svg>',
    '[open](javascript:alert(1))',
    '[open](JaVaScRiPt:alert(1))',
    '[open](java&#x73;cript:alert(1))',
    '[open](data:text/html;base64,PHNjcmlwdD4=)',
    '[open](file:///local/source)',
    '[open](//remote.example/path)',
    '![load](https://remote.example/pixel)',
    '| Column |\n| --- |\n| <img src=x onerror=alert(1)> |',
    '```html\n<script>alert(1)</script>\n```',
  ];
  for(const text of unsafe){
    const html=h.render(text);assert.doesNotMatch(html,/<script|<img|<svg|<a(?:\s|>)|onerror="|onmouseover="/i,text);
  }
  assert.match(h.render(unsafe[0]),/<strong>仍然可读<\/strong>/);
  const safe=h.render('[Source **guide**](https://docs.example/path?q=1&other=2)');
  assert.match(safe,/href="https:\/\/docs.example\/path\?q=1&amp;other=2"/);assert.match(safe,/rel="noopener noreferrer"/);assert.match(safe,/<strong>guide<\/strong>/);
});

test('real citations survive Markdown nesting but references in code and unknown ids stay literal',()=>{
  const h=harness();
  const source='**Rule [known]**\n\n- Applies [known]\n\n| Rule | Source |\n| --- | --- |\n| Amount | [known] |\n\n`[known]` [unknown]\n\n```cobol\n[known] <img>\n```';
  const html=h.run(`answerMarkdown.render(${JSON.stringify(source)},id=>id==='known'?'<button data-evidence="known">1</button>':null)`);
  assert.equal((html.match(/data-evidence="known"/g)||[]).length,3);
  assert.match(html,/<code>\[known\]<\/code>/);assert.match(html,/\[unknown\]/);assert.match(html,/<pre><code>\[known\] &lt;img&gt;<\/code><\/pre>/);
});

test('incomplete output and a missing parser never hide a model answer',()=>{
  const h=harness();assert.match(h.render('已有结果：**保费\n\n```cobol\nCOMPUTE AMOUNT = BASE'),/已有结果/);
  h.run('marked=undefined');const fallback=h.render('**业务回答**\n<script>unsafe</script>');
  assert.match(fallback,/业务回答/);assert.match(fallback,/&lt;script&gt;/);assert.doesNotMatch(fallback,/<script>/);
});

test('local parser assets load before the application and preserve their redistribution license',()=>{
  const html=fs.readFileSync(path.join(web,'index.html'),'utf8');
  assert.ok(html.indexOf('src="marked.umd.js"')<html.indexOf('src="markdown.js"'));
  assert.ok(html.indexOf('src="markdown.js"')<html.indexOf('src="app.js"'));
  assert.match(fs.readFileSync(path.join(web,'marked.LICENSE.md'),'utf8'),/Permission is hereby granted/);
  assert.doesNotMatch(html,/<script[^>]+src="https?:/);
});
