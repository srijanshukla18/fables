"use strict";

importScripts("/fables-core.js");

self.addEventListener("message", (event) => {
  const { raw, format, source } = event.data || {};
  try {
    const parsed = self.FablesCore.parseSession(raw, format, source);
    self.postMessage({ parsed });
  } catch (error) {
    self.postMessage({
      error: error && error.message ? error.message : String(error),
    });
  }
});
