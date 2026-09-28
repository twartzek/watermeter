import React from "react";

// __APP_VERSION__ / __BUILD_TIME__ are injected at build time by vite.config.js
// (git short hash + build timestamp) -- see that file. No manual bumping needed:
// every build automatically carries the commit it was built from, so after a
// deploy you can compare this against `git rev-parse --short HEAD` to confirm
// the Pi is really running the latest code.
const VERSION = typeof __APP_VERSION__ !== "undefined" ? __APP_VERSION__ : "dev";
const BUILD_TIME = typeof __BUILD_TIME__ !== "undefined" ? __BUILD_TIME__ : null;

const buildTimeLabel = BUILD_TIME
  ? new Date(BUILD_TIME).toLocaleString(undefined, {
      dateStyle: "short",
      timeStyle: "short",
    })
  : null;

const VersionBadge = ({ className = "" }) => (
  <span
    className={`select-all font-mono text-xs text-bodydark2 ${className}`}
    title={BUILD_TIME ? `Built ${BUILD_TIME}` : undefined}
  >
    {VERSION}
    {buildTimeLabel ? ` · ${buildTimeLabel}` : ""}
  </span>
);

export default VersionBadge;
