import assert from 'node:assert/strict';
import fs from 'node:fs';
import { test } from 'node:test';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('./static/app.js', import.meta.url), 'utf8');
const part = (start, end) => source.slice(source.indexOf(start), source.indexOf(end));

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ''; this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('records must be rendered as text'); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; this._text = ''; }
  addEventListener() {}
}

function renderer() {
  const nodes = Object.fromEntries(['pendingCount','historyCount','pending','history'].map(id => [id, new Element('div')]));
  const context = vm.createContext({
    document: {getElementById: id => nodes[id], createElement: tag => new Element(tag)},
    pendingStates: new Set(['pending','processing','sending']), labels: {sent:'已发送', pending:'待处理'},
    openCommandDetails: new Set(), commandCache: new Map(), busy:false, connected:true,
  });
  const snippets = [
    part('function display(', 'function setStatus('),
    part('function formatActions(', '// Keep a reader'),
    part('function updateList(', 'function renderIntake('),
    part('function renderComments(', 'function renderDirector('),
  ];
  vm.runInContext("const $ = id => document.getElementById(id);\n" + snippets.join('\n'), context);
  return {render: vm.runInContext('renderComments', context), nodes};
}

test('new and historic comments render without reading or observation fields', () => {
  const {render, nodes} = renderer();
  const historical = {id:'old', status:'sent', nickname:'旧观众', text:'旧问题', answer:'旧回答',
    segments_sent:2, actions:[{description:'点头'}], reading:{text:'旧短读'}, observation:'旧观察'};
  // Historical fields may be absent, malformed, or even throw: rendering no
  // longer depends on them and must still show the original reply and actions.
  Object.defineProperty(historical, 'reading', {get() { throw new Error('must not read'); }});
  Object.defineProperty(historical, 'observation', {get() { throw new Error('must not read'); }});
  const current = {id:'new', status:'pending', nickname:'观众', text:'<img src=x onerror=alert(1)>',
    answer:'新回答', segments_sent:0, actions:[]};
  render([historical, current], 'test-generation');
  assert.match(nodes.history.textContent, /已发送/);
  assert.match(nodes.history.textContent, /旧问题/);
  assert.match(nodes.history.textContent, /旧回答/);
  assert.match(nodes.history.textContent, /已发送片段：2/);
  assert.match(nodes.history.textContent, /点头/);
  assert.doesNotMatch(nodes.history.textContent, /方式：|整套阅读|旧短读|旧观察/);
  assert.match(nodes.pending.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(nodes.pending.textContent, /新回答/);
});

test('pure motion records keep actions and omit a speech-segment count', () => {
  const {render, nodes} = renderer();
  render([{id:'motion', status:'sent', input_kind:'operator', operator_mode:'motion', text:'点头', actions:['点头']}], 'test-generation');
  assert.match(nodes.history.textContent, /只做动作/);
  assert.match(nodes.history.textContent, /动作：点头/);
  assert.doesNotMatch(nodes.history.textContent, /已发送片段|台词：|方式：/);
});
