"use strict";

importScripts("/fables-core.js");

self.addEventListener("message", (event) => {
  const { id, raw, format, source } = event.data || {};
  try {
    const parsed = self.FablesCore.parseSession(raw, format, source);
    self.postMessage({ id, parsed });
  } catch (error) {
    self.postMessage({
      id,
      error: error && error.message ? error.message : String(error),
    });
  }
});
