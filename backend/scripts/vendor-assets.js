// Copies the vendored frontend deps (htmx, Alpine, JetBrains Mono) from
// node_modules into app/static, where FastAPI serves them locally. Run
// after `npm install` and whenever a vendored version bumps. The dashboard
// intentionally never loads these from a runtime CDN -- see base.html.
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const copy = (from, to) => {
  fs.mkdirSync(path.dirname(to), { recursive: true });
  fs.copyFileSync(from, to);
  console.log(`vendored ${path.relative(ROOT, to)}`);
};

copy(
  path.join(ROOT, "node_modules/htmx.org/dist/htmx.min.js"),
  path.join(ROOT, "app/static/vendor/htmx/htmx.min.js")
);
copy(
  path.join(ROOT, "node_modules/htmx.org/dist/ext/json-enc.js"),
  path.join(ROOT, "app/static/vendor/htmx/json-enc.js")
);
copy(
  path.join(ROOT, "node_modules/alpinejs/dist/cdn.min.js"),
  path.join(ROOT, "app/static/vendor/alpine/alpine.min.js")
);
copy(
  path.join(ROOT, "node_modules/three/build/three.module.min.js"),
  path.join(ROOT, "app/static/vendor/three/three.module.min.js")
);

// CSS2DRenderer (three.js addon, used for the globe's zoom-gated city/port
// name labels) imports from the bare specifier "three", which only
// resolves via an import map in a bundler-less setup. Patch it to a
// relative import against the vendored copy above instead.
{
  const dest = path.join(ROOT, "app/static/vendor/three/CSS2DRenderer.js");
  const src = fs.readFileSync(path.join(ROOT, "node_modules/three/examples/jsm/renderers/CSS2DRenderer.js"), "utf8");
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  fs.writeFileSync(dest, src.replace("from 'three'", "from './three.module.min.js'"));
  console.log(`vendored ${path.relative(ROOT, dest)} (patched 'three' import)`);
}

const fontWeights = ["400", "500", "700"];
for (const weight of fontWeights) {
  copy(
    path.join(ROOT, `node_modules/@fontsource/jetbrains-mono/files/jetbrains-mono-latin-${weight}-normal.woff2`),
    path.join(ROOT, `app/static/fonts/jetbrains-mono/jetbrains-mono-latin-${weight}-normal.woff2`)
  );
}
