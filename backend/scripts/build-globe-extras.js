// Generates two static reference layers for the 3D globe:
//
//   app/static/data/major-ports.json  -- ~40 of the world's highest-throughput
//     container ports (name + real-world lat/lon).
//
//   app/static/data/sea-routes.json   -- major global trade corridors, drawn
//     as multi-hop great-circle paths through real chokepoints (Malacca,
//     Suez, Bab-el-Mandeb, Hormuz, Gibraltar, Panama, Cape of Good Hope).
//
// IMPORTANT: sea-routes.json is a stylized reference layer, not routed or
// live shipping data. Each route is a straight great-circle arc between
// named waypoints -- it approximates the real corridor a route follows
// (e.g. via Suez rather than over Siberia) by chaining through the actual
// chokepoint it passes, but it is not sourced from AIS traffic and won't
// hug coastlines. Live vessel-position data is what GFW 4Wings (the
// amber signal dots) already provides; this layer exists purely to show
// *which* corridors matter, not to track *what* is currently on them.
const fs = require("fs");
const path = require("path");

const ROOT = path.join(__dirname, "..");

// [lon, lat] -- lon first to match the country-borders.json convention.
const PORTS = [
  ["Shanghai", 121.5, 31.2],
  ["Singapore", 103.85, 1.29],
  ["Ningbo-Zhoushan", 121.55, 29.87],
  ["Shenzhen", 114.05, 22.54],
  ["Guangzhou", 113.3, 23.1],
  ["Qingdao", 120.38, 36.07],
  ["Busan", 129.04, 35.1],
  ["Tianjin", 117.7, 39.0],
  ["Hong Kong", 114.17, 22.3],
  ["Rotterdam", 4.14, 51.95],
  ["Jebel Ali (Dubai)", 55.06, 25.0],
  ["Port Klang", 101.4, 3.0],
  ["Antwerp", 4.4, 51.29],
  ["Xiamen", 118.08, 24.48],
  ["Kaohsiung", 120.28, 22.6],
  ["Los Angeles", -118.26, 33.73],
  ["Long Beach", -118.19, 33.75],
  ["Hamburg", 9.99, 53.55],
  ["Laem Chabang", 100.88, 13.08],
  ["New York / New Jersey", -74.1, 40.67],
  ["Colombo", 79.85, 6.95],
  ["Tanjung Pelepas", 103.55, 1.36],
  ["Cai Mep (Ho Chi Minh City)", 107.0, 10.5],
  ["Tanjung Priok (Jakarta)", 106.88, -6.1],
  ["Piraeus", 23.63, 37.94],
  ["Valencia", -0.32, 39.44],
  ["Algeciras", -5.44, 36.13],
  ["Santos", -46.33, -23.96],
  ["Manila", 120.9, 14.58],
  ["Savannah", -81.09, 32.08],
  ["Nhava Sheva / JNPT (Mumbai)", 72.95, 18.95],
  ["Salalah", 54.09, 17.0],
  ["Vancouver", -123.11, 49.29],
  ["Genoa", 8.93, 44.4],
  ["Felixstowe", 1.35, 51.96],
  ["Bremerhaven", 8.58, 53.55],
  ["Durban", 31.03, -29.87],
  ["Alexandria", 29.9, 31.2],
  ["Colon", -79.9, 9.36],
  ["Callao (Lima)", -77.15, -12.05],
  ["Tokyo", 139.8, 35.6],
  ["Karachi", 66.98, 24.85],
  ["Apapa (Lagos)", 3.38, 6.45],
];

// Named chokepoints used as intermediate waypoints so corridors follow the
// real strait/canal a route passes through instead of a raw point-to-point
// great circle that would cut across land.
const CHOKEPOINTS = {
  malacca: [98.3, 3.0],
  bab_el_mandeb: [43.3, 12.5],
  suez_south: [32.55, 29.9],
  suez_north: [32.35, 31.25],
  hormuz: [56.3, 26.5],
  gibraltar: [-5.6, 35.9],
  panama_pacific: [-79.9, 8.9],
  panama_atlantic: [-79.9, 9.4],
  good_hope: [18.4, -34.5],
  dover: [1.5, 51.0],
};

const PORT_COORD = Object.fromEntries(PORTS.map(([name, lon, lat]) => [name, [lon, lat]]));
function pt(nameOrChoke) {
  if (CHOKEPOINTS[nameOrChoke]) return CHOKEPOINTS[nameOrChoke];
  if (PORT_COORD[nameOrChoke]) return PORT_COORD[nameOrChoke];
  throw new Error(`unknown waypoint: ${nameOrChoke}`);
}

// Multi-hop corridors: each entry is a chain of waypoint names/chokepoint
// keys. Real-world major trade lanes, picked for coverage rather than
// completeness.
const ROUTES = [
  ["Shanghai", "Busan", "Los Angeles"], // trans-Pacific
  ["Shenzhen", "malacca", "Colombo", "bab_el_mandeb", "suez_south", "suez_north", "gibraltar", "Rotterdam"], // Asia -> Europe via Suez
  ["New York / New Jersey", "Rotterdam"], // trans-Atlantic
  ["Rotterdam", "gibraltar", "good_hope", "Singapore"], // Cape route (Suez alt / oversized vessels)
  ["Shanghai", "Hong Kong", "Singapore", "Port Klang"], // intra-Asia / Malacca corridor
  ["Jebel Ali (Dubai)", "hormuz", "Nhava Sheva / JNPT (Mumbai)", "Colombo", "Singapore"], // Middle East -> South/SE Asia
  ["New York / New Jersey", "panama_atlantic", "panama_pacific", "Los Angeles"], // Panama transit, Atlantic -> Pacific
  ["Santos", "panama_atlantic", "panama_pacific", "Long Beach"], // South America -> US West Coast
  ["Rotterdam", "Apapa (Lagos)", "Durban"], // Europe -> West/Southern Africa
  ["Singapore", "Tanjung Priok (Jakarta)", "malacca", "Colombo"], // SE Asia hub spokes
  ["Busan", "Tokyo", "Vancouver"], // North Pacific
  ["Piraeus", "suez_north", "suez_south", "bab_el_mandeb", "Salalah", "Singapore"], // Mediterranean -> Asia via Suez
];

// Spherical linear interpolation between two [lon,lat] points (degrees),
// returning `steps+1` points along the great-circle arc between them.
function slerpArc([lon1, lat1], [lon2, lat2], steps) {
  const toRad = (d) => (d * Math.PI) / 180;
  const toDeg = (r) => (r * 180) / Math.PI;
  const p1 = [Math.cos(toRad(lat1)) * Math.cos(toRad(lon1)), Math.cos(toRad(lat1)) * Math.sin(toRad(lon1)), Math.sin(toRad(lat1))];
  const p2 = [Math.cos(toRad(lat2)) * Math.cos(toRad(lon2)), Math.cos(toRad(lat2)) * Math.sin(toRad(lon2)), Math.sin(toRad(lat2))];
  const dot = Math.max(-1, Math.min(1, p1[0] * p2[0] + p1[1] * p2[1] + p1[2] * p2[2]));
  const omega = Math.acos(dot);
  const points = [];
  if (omega < 1e-6) {
    points.push([lon1, lat1]);
    return points;
  }
  const sinOmega = Math.sin(omega);
  for (let i = 0; i <= steps; i++) {
    const t = i / steps;
    const a = Math.sin((1 - t) * omega) / sinOmega;
    const b = Math.sin(t * omega) / sinOmega;
    const x = a * p1[0] + b * p2[0];
    const y = a * p1[1] + b * p2[1];
    const z = a * p1[2] + b * p2[2];
    const lat = toDeg(Math.asin(z));
    const lon = toDeg(Math.atan2(y, x));
    points.push([Math.round(lon * 100) / 100, Math.round(lat * 100) / 100]);
  }
  return points;
}

const routeRings = ROUTES.map((chain) => {
  const path_ = [];
  for (let i = 0; i < chain.length - 1; i++) {
    const arc = slerpArc(pt(chain[i]), pt(chain[i + 1]), 24);
    // avoid duplicating the shared waypoint between consecutive hops
    path_.push(...(i === 0 ? arc : arc.slice(1)));
  }
  return path_;
});

const portsOut = PORTS.map(([name, lon, lat]) => ({ name, lon, lat }));

const dataDir = path.join(ROOT, "app/static/data");
fs.mkdirSync(dataDir, { recursive: true });

fs.writeFileSync(path.join(dataDir, "major-ports.json"), JSON.stringify(portsOut));
fs.writeFileSync(path.join(dataDir, "sea-routes.json"), JSON.stringify(routeRings));

console.log(`wrote ${portsOut.length} ports to app/static/data/major-ports.json`);
console.log(`wrote ${routeRings.length} sea route corridors to app/static/data/sea-routes.json`);
