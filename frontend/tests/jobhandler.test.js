"use strict";

// The job model behind the job view: what counts as a runnable job, which
// items belong together, and which pass entry represents them. Selecting and
// assigning passes is built on these, so getting them wrong assigns burn
// settings to the wrong geometry.

const test = require("node:test");
const assert = require("node:assert");
const { loadFrontend } = require("./harness");

function withJob(defs, items, passes) {
  const app = loadFrontend(["jobhandler.js"]);
  app.jobhandler.defs = defs;
  app.jobhandler.items = items;
  app.jobhandler.passes = passes || [];
  app.jobhandler.groupIdenticalImages();
  return app.jobhandler;
}

function path(color) {
  return { def: 0, color: color };
}

// ---------------------------------------------------------------------------
// what counts as runnable
// ---------------------------------------------------------------------------

test("a job with no defs or no items is empty", () => {
  assert.equal(withJob([], []).isEmpty(), true);
  assert.equal(withJob([{ kind: "path", data: [] }], []).isEmpty(), true);
  assert.equal(
    withJob([{ kind: "path", data: [] }], [{ def: 0 }]).isEmpty(),
    false,
  );
});

test("passes are what makes a job runnable, and there may be none", () => {
  const job = withJob([{ kind: "path", data: [] }], [{ def: 0 }]);
  assert.equal(job.hasPasses(), false);
  job.passes = [{ items: [0], feedrate: 2000, intensity: 50 }];
  assert.equal(job.hasPasses(), true);
});

// ---------------------------------------------------------------------------
// walking the job
// ---------------------------------------------------------------------------

test("looping by kind only visits that kind", () => {
  const job = withJob(
    [
      { kind: "path", data: [] },
      { kind: "image", data: "a", source: "a" },
      { kind: "fill", data: [] },
    ],
    [{ def: 0 }, { def: 1 }, { def: 2 }],
  );
  const seen = [];
  job.loopItems(function (item, i) {
    seen.push(i);
  }, "image");
  assert.deepEqual(seen, [1]);

  const both = [];
  job.loopItems(function (item, i) {
    both.push(i);
  }, "path fill");
  assert.deepEqual(both, [0, 2]);

  const all = [];
  job.loopItems(function (item, i) {
    all.push(i);
  });
  assert.deepEqual(all, [0, 1, 2]);
});

test("looping passes hands over the item indices of each", () => {
  const job = withJob(
    [{ kind: "path", data: [] }],
    [{ def: 0 }, { def: 0 }],
    [
      { items: [0], feedrate: 2000 },
      { items: [1, 0], feedrate: 1000 },
    ],
  );
  const seen = [];
  job.loopPasses(function (pass, idxs) {
    seen.push([pass.feedrate, idxs]);
  });
  assert.deepEqual(seen, [
    [2000, [0]],
    [1000, [1, 0]],
  ]);
});

// ---------------------------------------------------------------------------
// what selecting one item selects
// ---------------------------------------------------------------------------

test("selecting a path takes every path of that color", () => {
  const job = withJob(
    [{ kind: "path", data: [] }],
    [path("#ff0000"), path("#0000ff"), path("#ff0000")],
  );
  assert.deepEqual(job.matchingItems(0), [0, 2]);
  assert.deepEqual(job.matchingItems(1), [1]);
});

test("a fill of the same color as a path stays separate", () => {
  const job = withJob(
    [
      { kind: "path", data: [] },
      { kind: "fill", data: [] },
    ],
    [
      { def: 0, color: "#ff0000" },
      { def: 1, color: "#ff0000" },
    ],
  );
  assert.deepEqual(job.matchingItems(0), [0], "the path alone");
  assert.deepEqual(job.matchingItems(1), [1], "the fill alone");
});

test("copies of one image are selected together", () => {
  const job = withJob(
    [
      { kind: "image", data: "PNGDATA", source: "hash1" },
      { kind: "image", data: "PNGDATA", source: "hash1" },
      { kind: "image", data: "OTHER", source: "hash2" },
    ],
    [{ def: 0 }, { def: 1 }, { def: 2 }],
  );
  assert.deepEqual(job.matchingItems(0), [0, 1]);
  assert.deepEqual(job.matchingItems(1), [0, 1]);
  assert.deepEqual(job.matchingItems(2), [2]);
});

// ---------------------------------------------------------------------------
// which pass entry stands for an item
// ---------------------------------------------------------------------------

test("copies of one image share the first copy's pass entry", () => {
  const job = withJob(
    [
      { kind: "image", data: "PNGDATA", source: "hash1" },
      { kind: "image", data: "PNGDATA", source: "hash1" },
    ],
    [{ def: 0 }, { def: 1 }],
  );
  assert.equal(job.groupRep(0), 0);
  assert.equal(job.groupRep(1), 0, "the copy points at the first");
  assert.deepEqual(job.groupMembers(0), [0, 1]);
});

test("images group on their pixels, not on where they sit", () => {
  const job = withJob(
    [
      { kind: "image", data: "PNGDATA", source: "hash1", pos: [0, 0] },
      { kind: "image", data: "PNGDATA", source: "hash1", pos: [50, 50] },
    ],
    [{ def: 0 }, { def: 1 }],
  );
  assert.deepEqual(job.groupMembers(job.groupRep(1)), [0, 1]);
});

test("an image with no source groups on its data instead", () => {
  const job = withJob(
    [
      { kind: "image", data: "PNGDATA" },
      { kind: "image", data: "PNGDATA" },
      { kind: "image", data: "OTHER" },
    ],
    [{ def: 0 }, { def: 1 }, { def: 2 }],
  );
  assert.deepEqual(job.groupMembers(job.groupRep(1)), [0, 1]);
  assert.deepEqual(job.groupMembers(job.groupRep(2)), [2]);
});

test("a path is its own pass entry", () => {
  const job = withJob([{ kind: "path", data: [] }], [path("#ff0000")]);
  assert.equal(job.groupRep(0), 0);
  assert.deepEqual(job.groupMembers(0), [0]);
});

// ---------------------------------------------------------------------------
// colors
// ---------------------------------------------------------------------------

test("an uncolored first path becomes black, later ones get their own", () => {
  const job = withJob(
    [{ kind: "path", data: [] }],
    [{ def: 0 }, { def: 0 }, { def: 0 }],
  );
  job.normalizeColors();
  assert.equal(job.items[0].color, "#000000");
  assert.match(job.items[1].color, /^#[0-9a-f]+$/);
  assert.match(job.items[2].color, /^#[0-9a-f]+$/);
});

test("colors already set are left alone", () => {
  const job = withJob(
    [{ kind: "path", data: [] }],
    [path("#123456"), path("#abcdef")],
  );
  job.normalizeColors();
  assert.equal(job.items[0].color, "#123456");
  assert.equal(job.items[1].color, "#abcdef");
});

test("the color list covers every color in use", () => {
  // it is asked whether a color is taken, so it lists them per item rather
  // than uniquely, and an item without a color is not one
  const job = withJob(
    [{ kind: "path", data: [] }],
    [path("#ff0000"), path("#0000ff"), path("#ff0000"), { def: 0 }],
  );
  const colors = job.getAllColors();
  assert.ok(colors.indexOf("#ff0000") !== -1);
  assert.ok(colors.indexOf("#0000ff") !== -1);
  assert.equal(colors.indexOf("#00ff00"), -1, "a color not in the job");
  assert.equal(colors.length, 3, "one per colored item");
});
