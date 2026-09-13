// Polls the run's progress every 2 seconds and redraws the stages, clips and log in place.
// Everything is written with textContent: clip reasons and log lines come from the model.
(function () {
  const root = document.getElementById("progress");
  if (!root) return;
  const bound = (name) => root.querySelector('[data-bind="' + name + '"]');
  const make = (tag, className, text) => {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  };

  function draw(p) {
    bound("headline").textContent = p.headline;
    const error = bound("error");
    error.textContent = p.error || "";
    error.hidden = !p.error;

    bound("stages").replaceChildren(...p.stages.map((s) => {
      const li = make("li", "stage is-" + s.state);
      const text = make("span", "stage-text");
      text.append(make("span", "stage-label", s.label), make("span", "stage-detail", s.detail));
      li.append(make("span", "stage-mark"), text);
      return li;
    }));

    const clips = bound("clips");
    if (p.clips.length) {
      clips.replaceChildren(...p.clips.map((c) => {
        const li = make("li");
        const when = make("span", "found-when mono");
        when.append(c.span, make("br"), c.length);
        li.append(when, make("span", "", c.reason));
        return li;
      }));
    }

    const log = bound("log");
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
    log.textContent = p.log.join("\n");
    if (atBottom) log.scrollTop = log.scrollHeight;

    bound("done").hidden = !p.terminal;
    if (p.terminal) document.getElementById("run-actions").replaceChildren();
  }

  const log = bound("log");
  log.scrollTop = log.scrollHeight;

  async function tick() {
    try {
      const response = await fetch(root.dataset.url, { headers: { Accept: "application/json" } });
      if (response.status === 401) return window.location.assign("/signin");
      if (response.ok) {
        const p = await response.json();
        if (p.status === "needs_review") return window.location.reload();  // the page becomes the review
        draw(p);
        if (p.terminal) return;
      }
    } catch (e) { /* a dropped poll is retried on the next tick */ }
    setTimeout(tick, 2000);
  }
  if (!bound("done").hidden) return;  // already finished when the page loaded
  setTimeout(tick, 2000);
})();
