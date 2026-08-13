// Fetches Natural Earth's 50m admin-1 (state/province) boundary LINES
// dataset and flattens it into app/static/data/admin1-borders.json, in
// the same [[lon,lat], ...] line-array format country-borders.json uses.
// This is the "inland" layer: state/province lines *inside* countries,
// layered on top of the national outlines from build-country-borders.js.
// It naturally includes full US state coverage (all 50 states + DC) as
// part of the global dataset, along with first-level administrative
// divisions for every other country Natural Earth tracks them for.
//
// Requires network access to raw.githubusercontent.com at build time
// (same class of one-time build-time network dependency as `npm install`
// fetching world-atlas/world-countries/all-the-cities from the npm
// registry) -- the fetched+flattened *output* is committed, so deploys
// never need network for this at runtime. If this specific host is
// unreachable from your build environment, download the URL below by
// hand and point SOURCE_FILE at the local copy instead.
const fs = require("fs");
const path = require("path");
const https = require("https");

const ROOT = path.join(__dirname, "..");
const SOURCE_URL =
  "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_1_states_provinces_lines.geojson";

function fetchText(url) {
  return new Promise((resolve, reject) => {
    https
      .get(url, (res) => {
        if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {
          resolve(fetchText(res.headers.location));
          return;
        }
        if (res.statusCode !== 200) {
          reject(new Error(`${url} -> ${res.statusCode}`));
          return;
        }
        let body = "";
        res.on("data", (chunk) => (body += chunk));
        res.on("end", () => resolve(body));
      })
      .on("error", reject);
  });
}

async function main() {
  const sourceFileOverride = process.env.ADMIN1_SOURCE_FILE;
  const raw = sourceFileOverride ? fs.readFileSync(sourceFileOverride, "utf8") : await fetchText(SOURCE_URL);
  const geojson = JSON.parse(raw);

  const lines = [];
  for (const feature of geojson.features) {
    const geom = feature.geometry;
    if (!geom) continue;
    const paths = geom.type === "LineString" ? [geom.coordinates] : geom.type === "MultiLineString" ? geom.coordinates : [];
    for (const coords of paths) {
      lines.push(coords.map(([lon, lat]) => [Math.round(lon * 100) / 100, Math.round(lat * 100) / 100]));
    }
  }

  const outPath = path.join(ROOT, "app/static/data/admin1-borders.json");
  fs.mkdirSync(path.dirname(outPath), { recursive: true });
  fs.writeFileSync(outPath, JSON.stringify(lines));
  console.log(`wrote ${lines.length} admin-1 border lines to app/static/data/admin1-borders.json (${(fs.statSync(outPath).size / 1024).toFixed(0)} KB)`);
}

main().catch((err) => {
  console.error(err.message);
  console.error(`If raw.githubusercontent.com is unreachable from this environment, download`);
  console.error(`${SOURCE_URL}`);
  console.error(`by hand and re-run with ADMIN1_SOURCE_FILE=/path/to/file.geojson node scripts/build-admin1-borders.js`);
  process.exit(1);
});
