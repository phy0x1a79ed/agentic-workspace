// Naked-component bundle for the awm static-registration smoke test.
// Mounted by the auto-generated ESM shell against <div id="app">.

const root = document.getElementById("app");
if (root) {
  const heading = document.createElement("h1");
  heading.textContent = "hello from awm static demo";
  heading.className = "awm-static-demo-heading";

  const meta = document.createElement("p");
  meta.textContent = `served at ${location.pathname} · ${new Date().toISOString()}`;
  meta.className = "awm-static-demo-meta";

  root.append(heading, meta);
}

console.log("[awm-static-demo] mounted at", location.pathname);
