"use strict";

// The status poll is what the ui runs on: it decides which controls are live,
// what the run button says, and whether a stop condition is showing. None of
// that is visible to the backend test suite, so it is checked here.

const test = require("node:test");
const assert = require("node:assert");
const { loadFrontend } = require("./harness");

function machine(overrides) {
  // a status frame as the backend sends it
  return Object.assign(
    {
      server: true,
      serial: true,
      ready: true,
      paused: false,
      progress: 1.0,
      remaining: [0.0, 0.0],
      stops: {},
      info: {},
      stackclear: 999999,
    },
    overrides || {},
  );
}

function fresh() {
  const app = loadFrontend(["status.js"]);
  app.status_init();
  // a connected, idle machine, so tests start from the state the app settles in
  app.status_handle_message(machine());
  app.dom.reset_messages();
  return app;
}

// ---------------------------------------------------------------------------
// change detection, which gates every handler below
// ---------------------------------------------------------------------------

test("a handler only runs when its value changes", () => {
  const app = fresh();
  let calls = 0;
  app.status_handlers.progress = () => calls++;
  app.status_handle_message(machine({ progress: 0.5 }));
  app.status_handle_message(machine({ progress: 0.5 }));
  app.status_handle_message(machine({ progress: 0.6 }));
  assert.equal(calls, 2);
});

test("change detection compares arrays and maps by value", () => {
  const app = fresh();
  assert.equal(app.status_check_new([1, 2], [1, 2]), false);
  assert.equal(app.status_check_new([1, 2], [1, 3]), true);
  assert.equal(app.status_check_new([1, 2], [1, 2, 3]), true);
  assert.equal(app.status_check_new({ x1: true }, { x1: true }), false);
  assert.equal(app.status_check_new({ x1: true }, { x2: true }), true);
  assert.equal(app.status_check_new({}, { x1: true }), true);
  assert.equal(app.status_check_new(1.0, 1.0), false);
  assert.equal(app.status_check_new(false, true), true);
});

// ---------------------------------------------------------------------------
// idle and busy
// ---------------------------------------------------------------------------

test("an idle machine offers its controls", () => {
  const app = fresh();
  for (const btn of [
    "#pulse_btn",
    "#origin_btn",
    "#homing_btn",
    "#moveBy_btn",
    "#motion_btn",
    "#jog_btn",
  ]) {
    assert.equal(app.dom.disabled(btn), false, btn);
  }
  assert.equal(app.run_btn.state.spinning, false);
});

test("a busy machine takes its controls away", () => {
  const app = fresh();
  app.status_handle_message(machine({ ready: false }));
  for (const btn of [
    "#pulse_btn",
    "#origin_btn",
    "#homing_btn",
    "#moveBy_btn",
    "#motion_btn",
    "#jog_btn",
  ]) {
    assert.equal(app.dom.disabled(btn), true, btn);
  }
  assert.equal(app.run_btn.state.spinning, true);
});

test("polling slows down when idle and speeds up when busy", () => {
  const app = fresh();
  assert.equal(app.status_every, 4000);
  app.status_handle_message(machine({ ready: false }));
  assert.ok(app.status_every <= 2000, "busy should poll often");
});

// ---------------------------------------------------------------------------
// the wait between hitting run and the machine starting
// ---------------------------------------------------------------------------

test("waiting says so, without a stale progress bar", () => {
  const app = fresh();
  app.run_btn.setProgress(1.0);
  app.status_set_waiting(true);
  assert.equal(app.dom.el("#run_btn span.ladda-label").html, "Waiting");
  assert.equal(app.run_btn.state.spinning, true);
  assert.equal(app.run_btn.state.progress, 0);
});

test("waiting ignores the progress the machine is still reporting", () => {
  const app = fresh();
  app.status_set_waiting(true);
  // whatever the machine says here belongs to the job before this one
  app.status_handle_message(machine({ ready: true, progress: 0.77 }));
  assert.equal(app.dom.el("#run_btn span.ladda-label").html, "Waiting");
  assert.equal(app.run_btn.state.spinning, true, "the wait is not over yet");
  assert.equal(app.run_btn.state.progress, 0, "not the last job's progress");
});

test("waiting holds when the machine goes idle again under it", () => {
  // run can be pressed by keyboard while the machine is busy with something
  // else, since the shortcut does not go by the button's disabled class. That
  // something finishing is not this job starting.
  const app = fresh();
  app.status_handle_message(machine({ ready: false }));
  app.status_set_waiting(true);
  app.status_handle_message(machine({ ready: true }));
  assert.equal(app.job_waiting, true);
  assert.equal(app.dom.el("#run_btn span.ladda-label").html, "Waiting");
  assert.equal(app.run_btn.state.spinning, true);
  assert.equal(app.dom.disabled("#jog_btn"), true, "still not an idle machine");
});

test("waiting polls at the busy rate, so the start is not missed", () => {
  const app = fresh();
  assert.equal(app.status_every, 4000);
  app.status_set_waiting(true);
  assert.ok(app.status_every <= 2000);
});

test("the machine taking the job ends the wait", () => {
  const app = fresh();
  app.status_set_waiting(true);
  app.status_handle_message(machine({ ready: false, progress: 0.02 }));
  assert.equal(app.job_waiting, false);
  assert.equal(app.dom.el("#run_btn span.ladda-label").html, "Run");
  assert.equal(app.run_btn.state.progress, 0.02);
  assert.equal(app.run_btn.state.spinning, true);
});

test("calling off the wait hands control back to the idle machine", () => {
  const app = fresh();
  app.status_set_waiting(true);
  app.status_set_waiting(false); // what a rejected run does
  app.run_btn.stop();
  app.status_handle_message(machine({ ready: false }));
  app.status_handle_message(machine({ ready: true }));
  assert.equal(app.run_btn.state.spinning, false);
  assert.equal(app.dom.disabled("#jog_btn"), false);
});

// ---------------------------------------------------------------------------
// stop conditions and warnings
// ---------------------------------------------------------------------------

test("a limit stop shows on that limit and locks the controls", () => {
  const app = fresh();
  app.status_handle_message(machine({ stops: { y2: true } }));
  assert.ok(app.dom.$("#status_limit_y2").hasClass("label-danger"));
  assert.ok(app.dom.$("#status_limit_x1").hasClass("label-success"));
  for (const btn of ["#run_btn", "#jog_btn", "#pulse_btn", "#motion_btn"]) {
    assert.equal(app.dom.disabled(btn), true, btn);
  }
  assert.ok(app.dom.$("#status_btn").hasClass("btn-danger"));
});

test("clearing the stop gives the controls back", () => {
  const app = fresh();
  app.status_handle_message(machine({ stops: { y2: true } }));
  app.status_handle_message(machine({ stops: {} }));
  assert.ok(app.dom.$("#status_limit_y2").hasClass("label-success"));
  assert.equal(app.dom.disabled("#run_btn"), false);
  assert.ok(app.dom.$("#status_btn").hasClass("btn-success"));
});

test("a transmission stop shows as itself, not as a limit", () => {
  const app = fresh();
  app.status_handle_message(machine({ stops: { transmission: true } }));
  assert.ok(app.dom.$("#status_transmission").hasClass("label-danger"));
  assert.ok(app.dom.$("#status_limit_x1").hasClass("label-success"));
});

test("an open door warns and holds back the pulse button", () => {
  const app = fresh();
  app.status_handle_message(machine({ info: { door: true } }));
  assert.ok(app.dom.$("#status_door").hasClass("label-warning"));
  assert.equal(app.dom.disabled("#pulse_btn"), true);
  assert.ok(app.dom.$("#status_btn").hasClass("btn-warning"));
});

test("the chiller reports separately from the door", () => {
  const app = fresh();
  app.status_handle_message(machine({ info: { chiller: true } }));
  assert.ok(app.dom.$("#status_chiller").hasClass("label-warning"));
  assert.ok(app.dom.$("#status_door").hasClass("label-success"));
  assert.equal(app.dom.disabled("#pulse_btn"), true);
});

test("the controller running low on memory presses stop", () => {
  const app = fresh();
  app.status_handle_message(machine({ stackclear: 64 }));
  assert.deepEqual(app.dom.triggered("#stop_btn"), ["click"]);
  assert.ok(
    app.dom.messages.some((m) => m.level === "error"),
    "and says why",
  );
});

test("a low memory warning alone does not press stop", () => {
  const app = fresh();
  app.status_handle_message(machine({ stackclear: 150 }));
  assert.deepEqual(app.dom.triggered("#stop_btn"), []);
  assert.ok(app.dom.messages.some((m) => m.level === "warn"));
});

// ---------------------------------------------------------------------------
// pause and the info line
// ---------------------------------------------------------------------------

test("pausing swaps the button to play, and back again", () => {
  const app = fresh();
  app.status_handle_message(machine({ paused: true }));
  assert.ok(app.dom.$("#pause_btn").hasClass("btn-primary"));
  assert.equal(app.dom.el("#play_glyph").visible, true);
  assert.equal(app.dom.el("#pause_glyph").visible, false);
  app.status_handle_message(machine({ paused: false }));
  assert.ok(app.dom.$("#pause_btn").hasClass("btn-default"));
  assert.equal(app.dom.el("#play_glyph").visible, false);
  assert.equal(app.dom.el("#pause_glyph").visible, true);
});

test("a running job shows what is left of it instead of its duration", () => {
  const app = fresh();
  app.status_handle_message(machine({ remaining: [12.0, 300.0] }));
  assert.match(app.dom.el("#job_info_remaining").html, /remaining/);
  assert.equal(app.dom.el("#job_info_duration").visible, false);
});

test("the duration comes back when the run is over", () => {
  const app = fresh();
  app.status_handle_message(machine({ remaining: [12.0, 300.0] }));
  app.status_handle_message(machine({ remaining: [0.0, 0.0] }));
  assert.equal(app.dom.el("#job_info_remaining").html, "");
  assert.equal(app.dom.el("#job_info_duration").visible, true);
});

// ---------------------------------------------------------------------------
// losing the machine, and losing the server
// ---------------------------------------------------------------------------

test("serial going down grays the hardware indicators", () => {
  const app = fresh();
  app.status_handle_message(machine({ serial: false }));
  assert.ok(app.dom.$(".status_hw").hasClass("label-default"));
  assert.ok(app.dom.$("#status_serial").hasClass("label-danger"));
});

test("losing the server raises the connect dialog", () => {
  const app = fresh();
  app.status_handle_message({ server: false, serial: false });
  assert.ok(app.dom.el("#connect_modal").modals.includes("show"));
  assert.ok(app.dom.$("#status_btn").hasClass("btn-danger"));
  assert.ok(app.dom.messages.some((m) => m.level === "warning"));
});
