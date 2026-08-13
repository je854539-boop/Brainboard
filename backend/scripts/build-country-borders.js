// Converts world-atlas's 110m-resolution TopoJSON country boundaries into
// a flat static JSON file of [lon, lat] border rings, served at
// /static/data/country-borders.json for the 3D globe's country-outline
// layer. Pre-processed at build time (not in-browser) so the runtime page
// only ever fetches a plain JSON file, no topojson-client needed client-side.
//
// Run after `npm install` if you want to regenerate it (e.g. to bump
// resolution to countries-50m.json for finer borders at the cost of a
// larger payload).
const fs = require("fs");
const path = require("path");
const topojson = require("topojson-client");

const ROOT = path.join(__dirname, "..");
const topology = require(path.join(ROOT, "node_modules/world-atlas/countries-110m.json"));

const geojson = topojson.feature(topology, topology.objects.countries);

const rings = [];
for (const feature of geojson.features) {
  const geom = feature.geometry;
  if (!geom) continue;
  const polygons = geom.type === "Polygon" ? [geom.coordinates] : geom.type === "MultiPolygon" ? geom.coordinates : [];
  for (const polygon of polygons) {
    for (const ring of polygon) {
      // simplify: keep every 2nd point to shave payload size further, borders
      // are already 110m-resolution so this loses negligible visual detail
      rings.push(ring.filter((_, i) => i % 2 === 0).map(([lon, lat]) => [Math.round(lon * 100) / 100, Math.round(lat * 100) / 100]));
    }
  }
}

const outPath = path.join(ROOT, "app/static/data/country-borders.json");
fs.mkdirSync(path.dirname(outPath), { recursive: true });
fs.writeFileSync(outPath, JSON.stringify(rings));
console.log(`wrote ${rings.length} border rings to ${path.relative(ROOT, outPath)} (${(fs.statSync(outPath).size / 1024).toFixed(0)} KB)`);
