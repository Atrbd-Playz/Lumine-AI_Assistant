// Builds the React fixture bundle once, so the certification run itself needs
// no Node.js. Run from the repository root:
//
//   node agent/tests/harness/fixtures/src/build_react.cjs
//
// The output (fixtures/react_bundle.js) is committed. Rebuild it only when the
// fixture source or the project's React version changes.
const path = require("path");
const fs = require("fs");

const repoRoot = path.resolve(__dirname, "..", "..", "..", "..", "..");
const uiRoot = path.join(repoRoot, "lumine-ui");
const esbuild = require(path.join(uiRoot, "node_modules", "esbuild"));

const outfile = path.resolve(__dirname, "..", "react_bundle.js");

esbuild.buildSync({
  entryPoints: [path.resolve(__dirname, "react_app.jsx")],
  bundle: true,
  format: "iife",
  outfile,
  loader: { ".jsx": "jsx" },
  define: { "process.env.NODE_ENV": '"production"' },
  minify: true,
  legalComments: "none",
  nodePaths: [path.join(uiRoot, "node_modules")],
});

const bytes = fs.statSync(outfile).size;
console.log(`Wrote ${outfile} (${bytes} bytes)`);
