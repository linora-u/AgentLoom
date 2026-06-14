const fs = require("fs");
const path = require("path");

const workspace = process.env.AGENTLOOM_SKILL_WORKSPACE;
fs.writeFileSync(path.join(workspace, "node_probe.txt"), "node-ok\n", "utf8");
console.log("node-ok");
