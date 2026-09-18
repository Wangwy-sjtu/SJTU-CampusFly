// Run with node tests/test_map_runtime.js. No browser/GPU or provider requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('assets/map.html', 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
const frames = new Map(); let nextFrame = 0, created = 0, removed = 0;
const context2d = new Proxy({}, {get: () => () => {}});
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    style: {}, classList: {toggle() {}}, clientWidth: 800, clientHeight: 600,
    getBoundingClientRect: () => ({width: 800, height: 600, left: 0, top: 0}),
    getContext: () => context2d, listeners: {},
    addEventListener(event, handler) { this.listeners[event] = handler; },
    setPointerCapture() {}, releasePointerCapture() {},
  });
  return elements.get(id);
}
const sandbox = {
  document: {getElementById: element},
  qq: {maps: {
    LatLng: function(lat, lon) { this.lat = lat; this.lng = lon; },
    Polyline: function() { created++; this.setMap = () => removed++; },
    Circle: function() { created++; this.setMap = () => removed++; },
  }},
  requestAnimationFrame(fn) { const id = ++nextFrame; frames.set(id, fn); return id; },
  cancelAnimationFrame(id) { frames.delete(id); },
  addEventListener() {}, devicePixelRatio: 1,
};
sandbox.window = sandbox;
vm.runInNewContext(script.replace('      sizeCanvas();', `
  window.test = {state, graphPath, updateSnapPreview, render, renderRoadGraph,
    commitSnapSelection, nodeAtPixel};
      sizeCanvas();`), sandbox);
function flush() {
  let limit = 100;
  while (frames.size) {
    assert.ok(limit-- > 0, 'render must not schedule frames forever');
    const pending = [...frames.values()]; frames.clear(); pending.forEach(fn => fn());
  }
}
const nodes = ['a', 'b', 'c', 'z'].map((id, i) => ({id, latitude: 31, longitude: 121 + i / 1000}));
const edge = (id, start, end, one_way = false) => ({id, start, end, one_way, length_m: 100,
  geometry: [nodes.find(n => n.id === start), nodes.find(n => n.id === end)]});
const graph = {nodes, edges: [edge('ab', 'a', 'b'), edge('bc', 'b', 'c', true)]};
sandbox.setRoadGraph(graph); flush();
assert.deepEqual([...sandbox.test.graphPath('a', 'c').edgeIds], ['ab', 'bc']);
const tree = sandbox.test.state.pathTree;
sandbox.test.graphPath('a', 'b');
assert.equal(sandbox.test.state.pathTree, tree, 'same start reuses one search tree');
assert.equal(sandbox.test.graphPath('c', 'a'), null, 'one-way reverse is unreachable');
assert.equal(sandbox.test.graphPath('a', 'z'), null, 'disconnected nodes stay disconnected');
sandbox.setRoadGraph({...graph, edges: [edge('ac', 'a', 'c')]});
assert.deepEqual([...sandbox.test.graphPath('a', 'c').edgeIds], ['ac'], 'graph replacement invalidates cache');
sandbox.setRoadGraph(graph);
sandbox.test.state.tencentMap = {setOptions() {}};
let centerReads = 0, zoom = 0;
Object.assign(sandbox.test.state.tencentMap, {
  getCenter() { centerReads++; return {lat: 31, lng: 121}; },
  getZoom: () => zoom,
  getProjection: () => ({fromLatLngToPoint: p => ({getX: () => p.lng * 10000, getY: () => p.lat * 10000})}),
});
assert.equal(sandbox.test.nodeAtPixel(400, 300).id, 'a');
assert.equal(centerReads, 1, 'map center is projected once per hit test');
zoom = 2;
assert.equal(sandbox.test.nodeAtPixel(440, 300).id, 'b', 'projection refreshes after zoom');
assert.equal(centerReads, 2);
sandbox.test.renderRoadGraph(); flush();
const oldCreated = created;
for (let i = 0; i < 1000; i++) sandbox.test.render();
assert.equal(frames.size, 1, 'render calls coalesce into one frame'); flush();
assert.equal(created, oldCreated, 'unchanged overlays are never recreated');
delete sandbox.qq.maps.Circle;
sandbox.test.renderRoadGraph(); flush(); // Previously scheduled forever without Circle.
const initialRemoved = removed;
sandbox.test.renderRoadGraph(); sandbox.test.renderRoadGraph(); flush();
assert.ok(removed > initialRemoved);
assert.equal(sandbox.test.state.roadPolylines.length, 2, 'stale chunks do not duplicate edges');
sandbox.test.state.snapCurrentNode = 'a';
sandbox.test.updateSnapPreview(nodes[1]);
const preview = sandbox.test.state.snapPreview;
for (let i = 0; i < 1000; i++) sandbox.test.updateSnapPreview(nodes[1]);
assert.equal(sandbox.test.state.snapPreview, preview, 'same candidate reuses geometry');
sandbox.clearRoute();
sandbox.test.commitSnapSelection(nodes[0]);
sandbox.test.commitSnapSelection(nodes[1]);
assert.equal(sandbox.test.state.route.strokes.length, 1);
sandbox.undoStroke();
assert.equal(sandbox.test.state.route.strokes.length, 0, 'undo final segment clears its geometry');
// Queued pointer work must be cancelled when the route/mode is reset.
sandbox.setDrawingMode(true);
const move = element('drawCanvas').listeners.pointermove;
for (let i = 0; i < 1000; i++) move({clientX: 20, clientY: 20});
assert.ok(frames.size <= 2, 'pointer previews are bounded to one queued update');
sandbox.clearRoute(); flush();
assert.equal(sandbox.test.state.snapPreview, null);
assert.equal(sandbox.test.state.snapCandidateNode, null);
console.log('map runtime: caching, coalescing, overlays, cancellation, undo passed');
