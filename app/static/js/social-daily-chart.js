(() => {
  const choices = [
    ["reach", "Reach", "#117f79"], ["impressions", "Impression", "#df6c45"],
    ["total_engagement", "Total Engagement", "#7767b8"],
    ["accounts_engaged", "Accounts Engaged", "#3382b8"],
    ["profile_visits", "Profile Visit", "#a56828"],
    ["website_clicks", "Click Website", "#a8466d"],
  ];
  document.querySelectorAll("[data-daily-chart]").forEach(chart => {
  const canvas = chart.querySelector("canvas");
  const source = document.getElementById(chart.dataset.source);
  if (!canvas || !source) return;
  const rows = JSON.parse(source.textContent);
  const active = new Set(["reach"]);
  const options = chart.querySelector("[data-chart-options]");
  const tooltip = chart.querySelector("[data-chart-tooltip]");
  choices.forEach(([key, label, color]) => {
    const item = document.createElement("label");
    item.innerHTML = `<input type="checkbox" value="${key}" ${active.has(key) ? "checked" : ""}><i style="--series:${color}"></i>${label}`;
    item.querySelector("input").addEventListener("change", event => {
      if (event.target.checked && active.size >= 3) event.target.checked = false;
      event.target.checked ? active.add(key) : active.delete(key);
      draw();
    });
    options.appendChild(item);
  });
  const context = canvas.getContext("2d");
  let points = [];
  function draw() {
    const ratio = window.devicePixelRatio || 1;
    const width = canvas.parentElement.clientWidth;
    canvas.width = width * ratio; canvas.height = 260 * ratio;
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, 260);
    const selected = choices.filter(([key]) => active.has(key));
    const values = rows.flatMap(row => selected.map(([key]) => row[key]).filter(value => value !== null));
    const max = Math.max(...values, 1); const left = 48; const right = 12; const top = 12; const bottom = 34;
    const chartWidth = width - left - right; const chartHeight = 260 - top - bottom;
    context.strokeStyle = "#dfe5e3"; context.fillStyle = "#68736f"; context.font = "11px sans-serif";
    for (let step = 0; step <= 4; step++) {
      const y = top + chartHeight * step / 4; context.beginPath(); context.moveTo(left, y); context.lineTo(width - right, y); context.stroke();
      context.fillText(Math.round(max * (4 - step) / 4).toLocaleString("id-ID"), 2, y + 4);
    }
    points = [];
    selected.forEach(([key, label, color]) => {
      context.strokeStyle = color; context.lineWidth = 2; context.beginPath(); let open = false;
      rows.forEach((row, index) => {
        const value = row[key]; if (value === null) { open = false; return; }
        const x = left + chartWidth * (rows.length === 1 ? .5 : index / (rows.length - 1));
        const y = top + chartHeight * (1 - value / max);
        open ? context.lineTo(x, y) : context.moveTo(x, y); open = true;
        points.push({x, y, index, key, label, color, value});
      }); context.stroke();
    });
    if (rows.length) { context.fillStyle = "#68736f"; context.fillText(rows[0].date.slice(5), left, 250); context.textAlign = "right"; context.fillText(rows.at(-1).date.slice(5), width - right, 250); context.textAlign = "left"; }
  }
  canvas.addEventListener("mousemove", event => {
    if (!points.length) return;
    const rect = canvas.getBoundingClientRect(); const x = event.clientX - rect.left;
    const nearest = points.reduce((best, point) => Math.abs(point.x - x) < Math.abs(best.x - x) ? point : best);
    const sameDay = points.filter(point => point.index === nearest.index);
    tooltip.innerHTML = `<strong>${rows[nearest.index].date}</strong>${sameDay.map(point => `<span><i style="--series:${point.color}"></i>${point.label}: ${point.value.toLocaleString("id-ID")}</span>`).join("")}`;
    tooltip.hidden = false; tooltip.style.left = `${Math.min(nearest.x + 8, rect.width - 180)}px`; tooltip.style.top = "12px";
  });
  canvas.addEventListener("mouseleave", () => { tooltip.hidden = true; });
  new ResizeObserver(draw).observe(canvas.parentElement); draw();
  });
})();
