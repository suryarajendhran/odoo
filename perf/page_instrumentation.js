/**
 * Injected in every page before any other script runs (Playwright
 * `add_init_script`). Exposes `window.__perf`, used by perf/journeys.py to
 * time journeys and collect client-side counters.
 *
 * Counters are reset by `__perf.start()` so that a journey only accounts for
 * what happens between its start and the moment the page is settled.
 */
(() => {
    const perf = {
        t0: 0,
        owlRenders: 0,
        pendingRequests: 0,
        lastMutation: 0,
        lastNetworkEnd: 0,
        longTasks: [],
        startedRequests: 0,
    };
    window.__perf = perf;

    // ---------------------------------------------------------------------
    // OWL template renders: `owl` is declared as a global `var` by the
    // web.assets_web bundle; trap the assignment to patch App.getTemplate so
    // that every template function (component render or t-call) is counted.
    // ---------------------------------------------------------------------
    function patchOwl(owl) {
        if (!owl || !owl.App || owl.App.prototype.__perfPatched) {
            return;
        }
        const getTemplate = owl.App.prototype.getTemplate;
        const wrapped = new WeakMap();
        owl.App.prototype.getTemplate = function (name) {
            const template = getTemplate.call(this, name);
            if (typeof template !== "function") {
                return template;
            }
            let counted = wrapped.get(template);
            if (!counted) {
                counted = function () {
                    perf.owlRenders++;
                    return template.apply(this, arguments);
                };
                wrapped.set(template, counted);
            }
            return counted;
        };
        owl.App.prototype.__perfPatched = true;
    }
    let owlValue;
    Object.defineProperty(window, "owl", {
        configurable: true,
        enumerable: true,
        get() {
            return owlValue;
        },
        set(value) {
            owlValue = value;
            patchOwl(value);
        },
    });

    // ---------------------------------------------------------------------
    // In-flight XHR / fetch tracking (the web client RPCs use XHR).
    // ---------------------------------------------------------------------
    function requestEnded() {
        perf.pendingRequests--;
        perf.lastNetworkEnd = performance.now();
    }
    const XHR = window.XMLHttpRequest;
    const send = XHR.prototype.send;
    XHR.prototype.send = function () {
        perf.pendingRequests++;
        perf.startedRequests++;
        this.addEventListener("loadend", requestEnded, { once: true });
        return send.apply(this, arguments);
    };
    const fetch = window.fetch;
    window.fetch = function () {
        perf.pendingRequests++;
        perf.startedRequests++;
        return fetch.apply(this, arguments).finally(requestEnded);
    };

    // ---------------------------------------------------------------------
    // DOM activity: the journey is "settled" once the DOM stopped changing.
    // ---------------------------------------------------------------------
    new MutationObserver(() => {
        perf.lastMutation = performance.now();
    }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });

    // ---------------------------------------------------------------------
    // Long tasks (> 50ms main thread blocking).
    // ---------------------------------------------------------------------
    try {
        new PerformanceObserver((list) => {
            for (const entry of list.getEntries()) {
                perf.longTasks.push({ start: entry.startTime, duration: entry.duration });
            }
        }).observe({ type: "longtask", buffered: true });
    } catch {
        // longtask not supported
    }

    /**
     * Reset counters and mark the start of a journey. `t0` defaults to now,
     * page load journeys pass 0 (navigation start).
     */
    perf.start = (t0) => {
        perf.t0 = t0 === undefined ? performance.now() : t0;
        perf.owlRenders = 0;
        perf.startedRequests = 0;
        perf.lastMutation = perf.t0;
        perf.lastNetworkEnd = perf.t0;
    };

    /**
     * Resolve once `selector` matches, no request is in flight and the DOM
     * has not changed for `quietMs`. Times are relative to `perf.t0`.
     */
    perf.waitSettled = (selector, quietMs = 300, timeoutMs = 30000) =>
        new Promise((resolve, reject) => {
            let readyAt = null;
            const deadline = performance.now() + timeoutMs;
            function check() {
                const now = performance.now();
                if (readyAt === null && document.querySelector(selector)) {
                    // the element may have been inserted by the last mutation
                    readyAt = Math.max(perf.lastMutation, perf.t0);
                    if (readyAt > now) {
                        readyAt = now;
                    }
                }
                const lastActivity = Math.max(perf.lastMutation, perf.lastNetworkEnd);
                if (readyAt !== null && perf.pendingRequests <= 0 && now - lastActivity >= quietMs) {
                    const settledAt = Math.max(readyAt, lastActivity);
                    const blocking = perf.longTasks.filter((t) => t.start >= perf.t0 && t.start <= settledAt);
                    resolve({
                        ready_ms: readyAt - perf.t0,
                        settled_ms: settledAt - perf.t0,
                        owl_renders: perf.owlRenders,
                        xhr_fetch_requests: perf.startedRequests,
                        long_tasks: blocking.length,
                        total_blocking_ms: blocking.reduce((sum, t) => sum + Math.max(0, t.duration - 50), 0),
                        js_heap_bytes: performance.memory ? performance.memory.usedJSHeapSize : null,
                    });
                } else if (now > deadline) {
                    reject(
                        new Error(
                            `Journey did not settle within ${timeoutMs}ms (selector ${selector} ` +
                                `${readyAt === null ? "never matched" : "matched"}, ` +
                                `${perf.pendingRequests} pending requests)`
                        )
                    );
                } else {
                    setTimeout(check, 20);
                }
            }
            check();
        });
})();
