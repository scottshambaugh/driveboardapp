"use strict";

// Loads a frontend source file with stand-ins for the things a browser would
// provide, so its logic can be tested without one.
//
// The stand-in for jQuery records what was asked of each selector instead of
// touching a document: which classes an element carries, what its html says,
// whether it is shown, and what was triggered on it. That is enough to assert
// on the state the ui puts its controls in, which is what these files are
// mostly deciding.

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const FRONTEND_DIR = path.join(__dirname, "..");

function makeElement(selector, dom) {
  const state = {
    selector: selector,
    classes: new Set(),
    props: {},
    attrs: {},
    html: "",
    value: "",
    visible: true,
    triggered: [],
    modals: [],
    handlers: {},
  };

  const api = {
    addClass: function (names) {
      String(names)
        .split(/\s+/)
        .filter(Boolean)
        .forEach((n) => state.classes.add(n));
      return proxy;
    },
    removeClass: function (names) {
      String(names)
        .split(/\s+/)
        .filter(Boolean)
        .forEach((n) => state.classes.delete(n));
      return proxy;
    },
    hasClass: function (name) {
      return state.classes.has(name);
    },
    html: function (value) {
      if (value === undefined) return state.html;
      state.html = value;
      return proxy;
    },
    text: function (value) {
      if (value === undefined) return state.html;
      state.html = value;
      return proxy;
    },
    val: function (value) {
      if (value === undefined) return state.value;
      state.value = value;
      return proxy;
    },
    show: function () {
      state.visible = true;
      return proxy;
    },
    hide: function () {
      state.visible = false;
      return proxy;
    },
    prop: function (name, value) {
      if (value === undefined) return state.props[name];
      state.props[name] = value;
      return proxy;
    },
    attr: function (name, value) {
      if (value === undefined) return state.attrs[name];
      state.attrs[name] = value;
      return proxy;
    },
    on: function (event, handler) {
      state.handlers[event] = handler;
      return proxy;
    },
    click: function (handler) {
      if (typeof handler === "function") state.handlers.click = handler;
      else api.trigger("click");
      return proxy;
    },
    trigger: function (event) {
      state.triggered.push(event);
      if (state.handlers[event]) state.handlers[event]({});
      return proxy;
    },
    modal: function (action) {
      state.modals.push(action);
      return proxy;
    },
    find: function (sub) {
      return dom.$(selector + " " + sub);
    },
  };

  // anything else a browser would do (tooltip, modal, animate, css, ...) is
  // not what these tests are about, so it is accepted and ignored
  const proxy = new Proxy(api, {
    get(target, name) {
      if (name in target) return target[name];
      if (name === "state") return state;
      if (name === "length") return 1;
      return function () {
        return proxy;
      };
    },
  });
  return proxy;
}

function makeDom() {
  const elements = new Map();
  const messages = [];

  const dom = {
    messages: messages,
    $: function (selector) {
      if (selector === undefined || selector === "") {
        // $().uxmessage(...) is how the app talks to the log pane
        return {
          uxmessage: function (level, text) {
            messages.push({ level: level, text: text });
          },
        };
      }
      if (typeof selector === "function") {
        return selector; // $(function(){}) document-ready, never fired here
      }
      if (!elements.has(selector)) {
        elements.set(selector, makeElement(selector, dom));
      }
      return elements.get(selector);
    },
    // what a test asks about an element
    el: function (selector) {
      return dom.$(selector).state;
    },
    disabled: function (selector) {
      return dom.$(selector).hasClass("disabled");
    },
    triggered: function (selector) {
      return dom.$(selector).state.triggered;
    },
    reset_messages: function () {
      messages.length = 0;
    },
  };

  dom.$.isEmptyObject = function (obj) {
    return !obj || Object.keys(obj).length === 0;
  };
  dom.$.each = function (obj, func) {
    Object.keys(obj || {}).forEach((k) => func(k, obj[k]));
  };
  dom.$.extend = Object.assign;
  return dom;
}

function makeLadda() {
  const state = { spinning: false, progress: null };
  return {
    state: state,
    start: function () {
      state.spinning = true;
      return this;
    },
    stop: function () {
      state.spinning = false;
      return this;
    },
    toggle: function () {
      state.spinning = !state.spinning;
      return this;
    },
    setProgress: function (value) {
      state.progress = value;
      return this;
    },
    isLoading: function () {
      return state.spinning;
    },
  };
}

/**
 * Load frontend files into one scope.
 *
 * @param {string[]} files - paths relative to the frontend directory
 * @param {object} extra - further globals the files expect
 * @returns the scope, with `dom` and `run_btn` for asserting against
 */
function loadFrontend(files, extra) {
  const dom = makeDom();
  const run_btn = makeLadda();
  const context = Object.assign(
    {
      $: dom.$,
      jQuery: dom.$,
      dom: dom,
      run_btn: run_btn,
      app_run_btn: run_btn,
      app_visibility: true,
      console: console,
      Math: Math,
      Date: Date,
      setTimeout: function () {},
      clearTimeout: function () {},
      window: {},
      // the drawing surface and the job model, which the status handlers
      // reach into but do not decide anything about
      jobview_mm2px: 1,
      jobview_offsetLayer: { visible: false, position: null },
      jobview_boundsLayer: { position: null },
      jobview_seekLayer: { position: null },
      jobview_feedLayer: { position: null },
      paper: {
        Point: function (x, y) {
          return { x: x, y: y };
        },
        view: { draw: function () {} },
      },
      posText: { content: "" },
      time_format: function (seconds) {
        return String(seconds);
      },
      jobhandler: { duration: 0, showDuration: function () {} },
      request_get: function () {},
      request_post: function () {},
    },
    extra || {},
  );
  context.globalThis = context;
  vm.createContext(context);
  for (const file of files) {
    const source = fs.readFileSync(path.join(FRONTEND_DIR, file), "utf8");
    vm.runInContext(source, context, { filename: file });
  }
  return context;
}

module.exports = { loadFrontend };
