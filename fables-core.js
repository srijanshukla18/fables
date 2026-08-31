(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.FablesCore = api;
})(typeof self !== "undefined" ? self : globalThis, function () {
  "use strict";

  const CODEX_BARE_ITEMS = new Set([
    "message", "reasoning", "function_call", "function_call_output",
    "custom_tool_call", "custom_tool_call_output", "local_shell_call",
    "web_search_call",
  ]);
  const CODEX_CONTEXT_PREFIXES = [
    "<permissions", "<environment_context", "<user_instructions",
    "<turn_aborted", "# AGENTS.md", "<AGENTS", "<app_context",
    "<system_status",
  ];

  function diagnostics() {
    return {
      malformedLines: 0,
      ignoredRecords: 0,
      orphanCalls: 0,
      orphanResults: 0,
      unsupported: {},
      warnings: [],
    };
  }

  function noteUnsupported(diag, type) {
    const key = String(type || "unknown");
    diag.unsupported[key] = (diag.unsupported[key] || 0) + 1;
  }

  function baseMeta(source, format) {
    return {
      source,
      format: format || source,
      title: "",
      cwd: "",
      branch: "",
      models: new Set(),
      efforts: new Set(),
      tokens: {
        input: 0,
        output: 0,
        reasoning: 0,
        cacheRead: 0,
        cacheWrite: 0,
      },
      start: "",
      end: "",
      diagnostics: diagnostics(),
    };
  }

  function touchTime(meta, ts) {
    if (ts === undefined || ts === null || ts === "") return;
    if (!meta.start) meta.start = ts;
    meta.end = ts;
  }

  function parseJsonLines(rawText) {
    const objects = [];
    const diag = diagnostics();
    for (const line of String(rawText || "").split("\n")) {
      const text = line.trim();
      if (!text) continue;
      try {
        const value = JSON.parse(text);
        if (value && typeof value === "object") objects.push(value);
        else {
          diag.malformedLines++;
          diag.warnings.push("A JSONL line was not an object.");
        }
      } catch (error) {
        diag.malformedLines++;
      }
    }
    return { objects, diagnostics: diag };
  }

  function mergeDiagnostics(target, source) {
    target.malformedLines += source.malformedLines || 0;
    target.ignoredRecords += source.ignoredRecords || 0;
    target.orphanCalls += source.orphanCalls || 0;
    target.orphanResults += source.orphanResults || 0;
    for (const [key, count] of Object.entries(source.unsupported || {})) {
      target.unsupported[key] = (target.unsupported[key] || 0) + count;
    }
    target.warnings.push(...(source.warnings || []));
  }

  function joinText(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";
    return content.map((part) => {
      if (typeof part === "string") return part;
      if (!part || typeof part !== "object") return "";
      return part.text || part.content || part.message || "";
    }).join("");
  }

  function firstText(content) {
    if (typeof content === "string") return content;
    if (!Array.isArray(content)) return "";
    return content.map((part) => {
      if (!part || typeof part !== "object") return "";
      return part.type === "text" || part.text !== undefined ? part.text || "" : "";
    }).join(" ");
  }

  function outputText(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") {
      if (value.length < 200000 && /^[\[{]/.test(value.trim())) {
        try {
          const parsed = JSON.parse(value);
          if (parsed && typeof parsed.output === "string") return parsed.output;
        } catch (error) {
          // The value is ordinary text that happens to begin with JSON punctuation.
        }
      }
      return value;
    }
    if (Array.isArray(value)) return value.map(outputText).filter(Boolean).join("\n");
    if (typeof value === "object") {
      if (value.output !== undefined) return outputText(value.output);
      if (value.content !== undefined) return outputText(value.content);
      if (value.text !== undefined) return outputText(value.text);
      if (value.value !== undefined) return outputText(value.value);
      if (value.stdout !== undefined || value.stderr !== undefined) {
        let result = outputText(value.stdout);
        if (value.stderr) result += (result ? "\n" : "") + "[stderr] " + outputText(value.stderr);
        return result || "(no output)";
      }
      try {
        return JSON.stringify(value, null, 2);
      } catch (error) {
        return String(value);
      }
    }
    return String(value);
  }

  function finalize(meta, items, pending) {
    if (pending) {
      for (const item of pending.values()) {
        if (item.output === null) meta.diagnostics.orphanCalls++;
      }
    }
    for (const item of items) item.search = itemSearchText(item);
    return { meta, items };
  }

  function claudeUserText(text, items, obj, run) {
    const value = String(text || "");
    if (!value.trim()) return;
    if (value.includes("<local-command-caveat>") && !value.includes("<command-name>")) return;
    if (value.startsWith("Caveat:")) return;
    const command = value.match(/<command-name>([^<]*)<\/command-name>/);
    if (command) {
      const args = value.match(/<command-args>([^<]*)<\/command-args>/);
      items.push({
        kind: "command",
        ts: obj.timestamp,
        text: (command[1] + " " + (args ? args[1] : "")).trim(),
        run,
        raw: [obj],
      });
      return;
    }
    const stdout = value.match(/<local-command-stdout>([\s\S]*?)<\/local-command-stdout>/);
    if (stdout) {
      if (stdout[1].trim()) {
        items.push({
          kind: "info",
          label: "command output",
          ts: obj.timestamp,
          text: stdout[1].trim(),
          run,
          raw: [obj],
        });
      }
      return;
    }
    if (value.startsWith("[Request interrupted")) {
      items.push({
        kind: "info",
        label: "interrupted",
        ts: obj.timestamp,
        text: "session interrupted by user",
        run,
        raw: [obj],
      });
      return;
    }
    if (value.startsWith("<system-reminder>")) {
      items.push({
        kind: "info",
        label: "system reminder",
        ts: obj.timestamp,
        text: value.replace(/<\/?system-reminder>/g, "").trim(),
        run,
        raw: [obj],
      });
      return;
    }
    items.push({
      kind: "user",
      ts: obj.timestamp,
      text: value,
      side: !!obj.isSidechain,
      run,
      raw: [obj],
    });
  }

  function claudeEffortEvent(text) {
    const value = String(text || "");
    let match = value.match(/Set effort level to\s+([a-z0-9_-]+)/i);
    if (match) return { effort: match[1].toLowerCase(), backfill: false };
    match = value.match(/Kept effort level as\s+([a-z0-9_-]+)/i);
    if (match) return { effort: match[1].toLowerCase(), backfill: true };
    return null;
  }

  function claudeResultText(block, toolUseResult) {
    if (toolUseResult) {
      if (typeof toolUseResult === "string") return toolUseResult;
      if (toolUseResult.stdout !== undefined || toolUseResult.stderr !== undefined) {
        return outputText(toolUseResult);
      }
      if (toolUseResult.file && toolUseResult.file.content !== undefined) {
        return outputText(toolUseResult.file.content);
      }
      if (Array.isArray(toolUseResult.structuredPatch) && toolUseResult.structuredPatch.length) {
        return toolUseResult.structuredPatch.map((hunk) =>
          "@@ -" + hunk.oldStart + "," + hunk.oldLines +
          " +" + hunk.newStart + "," + hunk.newLines + " @@\n" +
          (hunk.lines || []).join("\n")
        ).join("\n");
      }
      if (typeof toolUseResult.content === "string") return toolUseResult.content;
    }
    return outputText(block.content);
  }

  function parseClaudeObjects(objects, source, lineDiagnostics) {
    const meta = baseMeta(source || "claude", "claude");
    mergeDiagnostics(meta.diagnostics, lineDiagnostics || diagnostics());
    const items = [];
    const pending = new Map();
    let currentRun = {};
    let firstEffort = null;

    for (const obj of objects) {
      touchTime(meta, obj.timestamp);
      if (obj.cwd && !meta.cwd) meta.cwd = obj.cwd;
      if (obj.gitBranch && !meta.branch) meta.branch = obj.gitBranch;

      switch (obj.type) {
        case "summary":
          if (obj.summary && !meta.title) meta.title = obj.summary;
          break;
        case "ai-title":
          if (obj.aiTitle) meta.title = obj.aiTitle;
          break;
        case "user": {
          const content = obj.message && obj.message.content;
          if (typeof content === "string") {
            if (!obj.isMeta) {
              const effort = claudeEffortEvent(content);
              if (effort) {
                currentRun = { ...currentRun, effort: effort.effort };
                meta.efforts.add(currentRun.effort);
                if (!firstEffort) firstEffort = { ...effort, ts: obj.timestamp };
                meta.diagnostics.ignoredRecords++;
              } else {
                claudeUserText(content, items, obj, { ...currentRun });
              }
            } else {
              meta.diagnostics.ignoredRecords++;
            }
          } else if (Array.isArray(content)) {
            for (const block of content) {
              if (!block || typeof block !== "object") continue;
              if (block.type === "text") {
                if (!obj.isMeta) {
                  const effort = claudeEffortEvent(block.text);
                  if (effort) {
                    currentRun = { ...currentRun, effort: effort.effort };
                    meta.efforts.add(currentRun.effort);
                    if (!firstEffort) firstEffort = { ...effort, ts: obj.timestamp };
                  } else {
                    claudeUserText(block.text, items, obj, { ...currentRun });
                  }
                }
              } else if (block.type === "tool_result") {
                const tool = pending.get(block.tool_use_id);
                if (tool) {
                  tool.output = claudeResultText(block, obj.toolUseResult);
                  tool.isError = !!block.is_error;
                  tool.raw.push(obj);
                  pending.delete(block.tool_use_id);
                } else {
                  meta.diagnostics.orphanResults++;
                }
              } else if (block.type === "image") {
                items.push({
                  kind: "user",
                  ts: obj.timestamp,
                  text: "[image attached]",
                  side: !!obj.isSidechain,
                  run: { ...currentRun },
                  raw: [obj],
                });
              } else {
                noteUnsupported(meta.diagnostics, "claude.user." + block.type);
              }
            }
          }
          break;
        }
        case "assistant": {
          const message = obj.message || {};
          const realModel = message.model && message.model !== "<synthetic>" ? message.model : "";
          if (realModel) meta.models.add(realModel);
          const run = { ...currentRun };
          if (realModel) run.model = realModel;
          if (message.usage) {
            meta.tokens.input += message.usage.input_tokens || 0;
            meta.tokens.output += message.usage.output_tokens || 0;
            meta.tokens.reasoning += message.usage.reasoning_output_tokens || 0;
            meta.tokens.cacheRead += message.usage.cache_read_input_tokens || 0;
            meta.tokens.cacheWrite += message.usage.cache_creation_input_tokens || 0;
          }
          for (const block of Array.isArray(message.content) ? message.content : []) {
            if (!block || typeof block !== "object") continue;
            if (block.type === "thinking" && String(block.thinking || "").trim()) {
              items.push({
                kind: "thinking",
                ts: obj.timestamp,
                text: block.thinking,
                side: !!obj.isSidechain,
                run,
                raw: [obj],
              });
            } else if (block.type === "text" && String(block.text || "").trim()) {
              items.push({
                kind: "assistant",
                ts: obj.timestamp,
                text: block.text,
                side: !!obj.isSidechain,
                run,
                raw: [obj],
              });
            } else if (block.type === "tool_use") {
              const tool = {
                kind: "tool",
                ts: obj.timestamp,
                name: block.name,
                input: block.input,
                output: null,
                isError: false,
                side: !!obj.isSidechain,
                run,
                raw: [obj],
              };
              if (block.id) pending.set(block.id, tool);
              items.push(tool);
            } else if (block.type !== "thinking" && block.type !== "text") {
              noteUnsupported(meta.diagnostics, "claude.assistant." + block.type);
            }
          }
          break;
        }
        case "system":
          if (typeof obj.content === "string" && obj.content.trim() && obj.level !== "suggestion") {
            items.push({
              kind: "info",
              label: obj.subtype || "system",
              ts: obj.timestamp,
              text: obj.content,
              raw: [obj],
            });
          } else {
            meta.diagnostics.ignoredRecords++;
          }
          break;
        case "progress":
        case "file-history-snapshot":
        case "queue-operation":
          meta.diagnostics.ignoredRecords++;
          break;
        default:
          noteUnsupported(meta.diagnostics, "claude." + obj.type);
      }
    }

    if (firstEffort && firstEffort.backfill) {
      for (const item of items) {
        if (item.ts && firstEffort.ts && item.ts > firstEffort.ts) break;
        item.run = {
          ...(item.run || {}),
          effort: (item.run && item.run.effort) || firstEffort.effort,
        };
      }
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user" && !item.side);
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, pending);
  }

  function parseClaude(rawText, source) {
    const parsed = parseJsonLines(rawText);
    return parseClaudeObjects(parsed.objects, source || "claude", parsed.diagnostics);
  }

  function parsePiObjects(objects, lineDiagnostics) {
    const meta = baseMeta("pi", "pi");
    mergeDiagnostics(meta.diagnostics, lineDiagnostics || diagnostics());
    const items = [];
    const pending = new Map();
    for (const obj of objects) {
      touchTime(meta, obj.timestamp);
      switch (obj.type) {
        case "session":
          if (obj.cwd && !meta.cwd) meta.cwd = obj.cwd;
          break;
        case "model_change":
          if (obj.modelId) meta.models.add(obj.modelId);
          break;
        case "thinking_level_change":
          if (obj.thinkingLevel) meta.efforts.add(String(obj.thinkingLevel));
          break;
        case "message": {
          const message = obj.message || {};
          const content = message.content;
          if (message.role === "user") {
            if (typeof content === "string") {
              if (!obj.isMeta && String(content).trim()) {
                items.push({ kind: "user", ts: obj.timestamp, text: content, raw: [obj] });
              }
            } else if (Array.isArray(content)) {
              for (const block of content) {
                if (!block || typeof block !== "object") continue;
                if (block.type === "text" && String(block.text || "").trim()) {
                  items.push({ kind: "user", ts: obj.timestamp, text: block.text, raw: [obj] });
                } else if (block.type === "image") {
                  items.push({ kind: "user", ts: obj.timestamp, text: "[image attached]", raw: [obj] });
                } else {
                  noteUnsupported(meta.diagnostics, "pi.user." + block.type);
                }
              }
            }
          } else if (message.role === "assistant") {
            for (const block of Array.isArray(content) ? content : []) {
              if (!block || typeof block !== "object") continue;
              if (block.type === "thinking" && String(block.thinking || "").trim()) {
                items.push({ kind: "thinking", ts: obj.timestamp, text: block.thinking, raw: [obj] });
              } else if (block.type === "text" && String(block.text || "").trim()) {
                items.push({ kind: "assistant", ts: obj.timestamp, text: block.text, raw: [obj] });
              } else if (block.type === "toolCall") {
                const tool = {
                  kind: "tool",
                  ts: obj.timestamp,
                  name: block.name,
                  input: block.arguments,
                  output: null,
                  isError: false,
                  raw: [obj],
                };
                if (block.id) pending.set(block.id, tool);
                items.push(tool);
              } else {
                noteUnsupported(meta.diagnostics, "pi.assistant." + block.type);
              }
            }
          } else if (message.role === "toolResult") {
            const tool = pending.get(message.toolCallId);
            const text = outputText(message.content);
            if (tool) {
              tool.output = text;
              tool.isError = !!message.isError;
              tool.raw.push(obj);
              pending.delete(message.toolCallId);
            } else {
              meta.diagnostics.orphanResults++;
              if (text.trim()) {
                items.push({ kind: "info", label: "tool result", ts: obj.timestamp, text, raw: [obj] });
              }
            }
          } else if (message.role === "bashExecution") {
            items.push({
              kind: "tool",
              ts: obj.timestamp,
              name: "bash",
              input: message.command || "",
              output: message.output || "",
              isError: message.exitCode != null && message.exitCode !== 0,
              raw: [obj],
            });
          } else {
            noteUnsupported(meta.diagnostics, "pi.message." + message.role);
          }
          break;
        }
        case "custom":
          noteUnsupported(meta.diagnostics, "pi.custom");
          break;
        default:
          noteUnsupported(meta.diagnostics, "pi." + obj.type);
      }
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user");
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, pending);
  }

  function parsePi(rawText) {
    const parsed = parseJsonLines(rawText);
    return parsePiObjects(parsed.objects, parsed.diagnostics);
  }

  function parseCodexObjects(objects, lineDiagnostics) {
    const meta = baseMeta("codex", "codex");
    mergeDiagnostics(meta.diagnostics, lineDiagnostics || diagnostics());
    const items = [];
    const pending = new Map();
    let currentRun = null;
    let firstUser = "";

    for (const obj of objects) {
      let payload = obj.payload || {};
      let kind = obj.type;
      if (obj.payload === undefined) {
        if (obj.record_type !== undefined) {
          meta.diagnostics.ignoredRecords++;
          continue;
        }
        if (CODEX_BARE_ITEMS.has(obj.type)) {
          payload = obj;
          kind = "response_item";
        } else if (obj.git !== undefined) {
          if (obj.git && obj.git.branch) meta.branch = obj.git.branch;
          meta.diagnostics.ignoredRecords++;
          continue;
        }
      }
      const ts = obj.timestamp || payload.timestamp;
      touchTime(meta, ts);

      switch (kind) {
        case "session_meta":
          meta.cwd = payload.cwd || meta.cwd;
          if (payload.git && payload.git.branch) meta.branch = payload.git.branch;
          break;
        case "turn_context":
          if (payload.model) meta.models.add(payload.model);
          if (payload.effort) meta.efforts.add(payload.effort);
          currentRun = {
            model: payload.model || (currentRun && currentRun.model) || "",
            effort: payload.effort || (currentRun && currentRun.effort) || "",
          };
          if (payload.cwd && !meta.cwd) meta.cwd = payload.cwd;
          break;
        case "response_item": {
          const type = payload.type;
          if (type === "message") {
            const text = joinText(payload.content);
            if (!text.trim()) break;
            if (payload.role === "assistant") {
              items.push({ kind: "assistant", ts, text, run: currentRun, raw: [obj] });
            } else if (payload.role === "user") {
              const trimmed = text.trim();
              if (CODEX_CONTEXT_PREFIXES.some((prefix) => trimmed.startsWith(prefix))) {
                items.push({
                  kind: "info",
                  label: "injected context",
                  ts,
                  text: trimmed,
                  run: currentRun,
                  raw: [obj],
                });
              } else {
                if (!firstUser) firstUser = trimmed;
                items.push({ kind: "user", ts, text, run: currentRun, raw: [obj] });
              }
            } else {
              items.push({
                kind: "info",
                label: (payload.role || "system") + " message",
                ts,
                text,
                run: currentRun,
                raw: [obj],
              });
            }
          } else if (type === "reasoning") {
            const text = (payload.summary || []).map((part) => part.text || "").join("\n\n") ||
              joinText(payload.content);
            if (text.trim()) items.push({ kind: "thinking", ts, text, run: currentRun, raw: [obj] });
          } else if (type && type.endsWith("_call_output")) {
            const tool = pending.get(payload.call_id);
            if (tool) {
              tool.output = outputText(payload.output);
              tool.isError = /^exec_command failed|^error/i.test(tool.output.slice(0, 80));
              tool.raw.push(obj);
              pending.delete(payload.call_id);
            } else {
              meta.diagnostics.orphanResults++;
            }
          } else if (type && (type.endsWith("_call") || type === "local_shell_call")) {
            let input = payload.arguments;
            if (typeof input === "string") {
              try {
                input = JSON.parse(input);
              } catch (error) {
                // Preserve non-JSON arguments verbatim.
              }
            }
            if (input === undefined) input = payload.action || payload.input || {};
            const tool = {
              kind: "tool",
              ts,
              name: payload.name || type.replace(/_call$/, ""),
              input,
              output: null,
              isError: false,
              run: currentRun,
              raw: [obj],
            };
            if (payload.call_id) pending.set(payload.call_id, tool);
            items.push(tool);
          } else {
            noteUnsupported(meta.diagnostics, "codex.response." + type);
          }
          break;
        }
        case "event_msg": {
          const type = payload.type;
          if (type === "thread_name_updated" && payload.thread_name) {
            meta.title = meta.title || payload.thread_name;
          } else if (type === "token_count" && payload.info && payload.info.total_token_usage) {
            const usage = payload.info.total_token_usage;
            meta.tokens.cacheRead = usage.cached_input_tokens || 0;
            meta.tokens.input = Math.max(0, (usage.input_tokens || 0) - meta.tokens.cacheRead);
            meta.tokens.output = usage.output_tokens || 0;
            meta.tokens.reasoning = usage.reasoning_output_tokens || 0;
          } else if ([
            "user_message", "agent_message", "agent_reasoning",
            "task_started", "task_complete", "turn_started", "turn_complete",
          ].includes(type)) {
            meta.diagnostics.ignoredRecords++;
          } else {
            noteUnsupported(meta.diagnostics, "codex.event." + type);
          }
          break;
        }
        case "compacted":
          items.push({
            kind: "info",
            label: "compacted",
            ts,
            text: "context was compacted here",
            raw: [obj],
          });
          break;
        default:
          noteUnsupported(meta.diagnostics, "codex." + kind);
      }
    }
    if (!meta.title) meta.title = firstUser;
    return finalize(meta, items, pending);
  }

  function parseCodex(rawText) {
    const parsed = parseJsonLines(rawText);
    return parseCodexObjects(parsed.objects, parsed.diagnostics);
  }

  function parseCopilotObjects(objects, lineDiagnostics) {
    const meta = baseMeta("copilot", "copilot");
    mergeDiagnostics(meta.diagnostics, lineDiagnostics || diagnostics());
    const items = [];
    const pending = new Map();
    let currentRun = {};
    let firstUser = "";

    for (const event of objects) {
      const data = event.data || {};
      const type = event.type || "";
      const ts = event.timestamp || data.timestamp;
      touchTime(meta, ts);

      switch (type) {
        case "session.start":
        case "session.resume": {
          const context = data.context || {};
          meta.cwd = context.cwd || context.gitRoot || meta.cwd;
          meta.branch = context.branch || meta.branch;
          if (data.selectedModel) {
            currentRun = { ...currentRun, model: data.selectedModel };
            meta.models.add(data.selectedModel);
          }
          if (data.reasoningEffort) {
            currentRun = { ...currentRun, effort: data.reasoningEffort };
            meta.efforts.add(data.reasoningEffort);
          }
          break;
        }
        case "session.context_changed":
          meta.cwd = data.cwd || data.gitRoot || meta.cwd;
          meta.branch = data.branch || meta.branch;
          break;
        case "session.model_change":
          currentRun = {
            ...currentRun,
            model: data.newModel || currentRun.model || "",
            effort: data.reasoningEffort || currentRun.effort || "",
          };
          if (currentRun.model) meta.models.add(currentRun.model);
          if (currentRun.effort) meta.efforts.add(currentRun.effort);
          break;
        case "user.message": {
          const text = data.content || data.transformedContent || "";
          if (String(text).trim()) {
            if (!firstUser) firstUser = String(text).trim();
            items.push({ kind: "user", ts, text: String(text), run: { ...currentRun }, raw: [event] });
          }
          break;
        }
        case "assistant.message": {
          const run = {
            ...currentRun,
            model: data.model || currentRun.model || "",
          };
          if (run.model) meta.models.add(run.model);
          if (data.outputTokens) meta.tokens.output += data.outputTokens;
          if (String(data.reasoningText || "").trim()) {
            items.push({
              kind: "thinking",
              ts,
              text: data.reasoningText,
              run,
              raw: [event],
            });
          }
          if (String(data.content || "").trim()) {
            items.push({
              kind: data.phase === "analysis" ? "thinking" : "assistant",
              ts,
              text: data.content,
              run,
              raw: [event],
            });
          }
          break;
        }
        case "tool.execution_start": {
          const run = { ...currentRun, model: data.model || currentRun.model || "" };
          const tool = {
            kind: "tool",
            ts,
            name: data.toolName || data.mcpToolName || "tool",
            input: data.arguments || {},
            output: null,
            isError: false,
            run,
            raw: [event],
          };
          if (data.toolCallId) pending.set(data.toolCallId, tool);
          items.push(tool);
          break;
        }
        case "tool.execution_complete": {
          const tool = pending.get(data.toolCallId);
          if (tool) {
            tool.output = outputText(data.result !== undefined ? data.result : data.error);
            tool.isError = data.success === false || !!data.error;
            tool.raw.push(event);
            pending.delete(data.toolCallId);
          } else {
            meta.diagnostics.orphanResults++;
          }
          break;
        }
        case "system.message":
          if (String(data.content || "").trim()) {
            items.push({
              kind: "info",
              label: data.role || "system",
              ts,
              text: data.content,
              raw: [event],
            });
          }
          break;
        case "permission.requested": {
          const request = data.permissionRequest || data.promptRequest || {};
          items.push({
            kind: "info",
            label: "permission requested",
            ts,
            text: request.description || request.message || request.kind || "tool permission",
            raw: [event],
          });
          break;
        }
        case "session.compaction_start":
          items.push({
            kind: "info",
            label: "compaction",
            ts,
            text: "context compaction started",
            raw: [event],
          });
          break;
        case "session.compaction_complete":
          items.push({
            kind: "info",
            label: "compacted",
            ts,
            text: data.success === false ? "context compaction failed" : "context was compacted here",
            raw: [event],
          });
          break;
        case "subagent.started":
        case "subagent.completed":
          items.push({
            kind: "info",
            label: type === "subagent.started" ? "subagent started" : "subagent completed",
            ts,
            text: data.agentDisplayName || data.agentName || data.agentDescription || "subagent",
            raw: [event],
          });
          break;
        case "skill.invoked":
          items.push({
            kind: "command",
            ts,
            text: "/" + (data.name || "skill"),
            run: { ...currentRun, model: data.model || currentRun.model || "" },
            raw: [event],
          });
          break;
        case "session.warning":
        case "session.info":
        case "system.notification":
        case "abort":
          items.push({
            kind: "info",
            label: type.replace(/^session\./, ""),
            ts,
            text: data.message || data.content || data.reason || data.infoType || type,
            raw: [event],
          });
          break;
        case "session.shutdown": {
          const tokenDetails = data.tokenDetails || {};
          const count = (name) => (tokenDetails[name] && tokenDetails[name].tokenCount) || 0;
          meta.tokens.input = count("input") || meta.tokens.input;
          meta.tokens.output = count("output") || meta.tokens.output;
          meta.tokens.cacheRead = count("cache_read") || meta.tokens.cacheRead;
          meta.tokens.cacheWrite = count("cache_write") || meta.tokens.cacheWrite;
          if (data.currentModel) meta.models.add(data.currentModel);
          break;
        }
        case "assistant.turn_start":
        case "assistant.turn_end":
        case "permission.completed":
        case "session.binary_asset":
        case "session.workspace_file_changed":
        case "session.permissions_changed":
          meta.diagnostics.ignoredRecords++;
          break;
        default:
          noteUnsupported(meta.diagnostics, "copilot." + type);
      }
    }
    meta.title = firstUser;
    return finalize(meta, items, pending);
  }

  function parseCopilot(rawText) {
    const parsed = parseJsonLines(rawText);
    return parseCopilotObjects(parsed.objects, parsed.diagnostics);
  }

  function parseGemini(rawText) {
    const meta = baseMeta("gemini", "gemini");
    const items = [];
    let data;
    try {
      data = JSON.parse(String(rawText || ""));
    } catch (error) {
      meta.diagnostics.malformedLines++;
      meta.diagnostics.warnings.push("The Gemini session is not valid JSON.");
      return finalize(meta, items);
    }
    meta.start = data.startTime || "";
    meta.end = data.lastUpdated || meta.start;
    for (const message of Array.isArray(data.messages) ? data.messages : []) {
      const ts = message.timestamp;
      touchTime(meta, ts);
      const raw = [message];
      if (message.type === "user") {
        const text = firstText(message.content) || joinText(message.content);
        if (text.trim()) {
          if (!meta.title) meta.title = text.trim();
          items.push({ kind: "user", ts, text, raw });
        }
      } else if (message.type === "gemini") {
        const run = { model: message.model || "" };
        if (run.model) meta.models.add(run.model);
        const tokens = message.tokens || {};
        meta.tokens.input += tokens.input || tokens.inputTokens || tokens.prompt || 0;
        meta.tokens.output += tokens.output || tokens.outputTokens || tokens.candidates || 0;
        meta.tokens.reasoning += tokens.thoughts || tokens.reasoning || 0;
        meta.tokens.cacheRead += tokens.cached || tokens.cachedContent || 0;
        for (const thought of Array.isArray(message.thoughts) ? message.thoughts : []) {
          const text = typeof thought === "string" ? thought :
            thought.description || thought.text || thought.subject || "";
          if (String(text).trim()) items.push({ kind: "thinking", ts, text, run, raw });
        }
        const content = joinText(message.content);
        if (content.trim()) items.push({ kind: "assistant", ts, text: content, run, raw });
        for (const call of Array.isArray(message.toolCalls) ? message.toolCalls : []) {
          const name = call.name || call.toolName ||
            (call.functionCall && call.functionCall.name) || "tool";
          const input = call.args || call.arguments ||
            (call.functionCall && call.functionCall.args) || {};
          const result = call.result !== undefined ? call.result :
            call.response !== undefined ? call.response : call.functionResponse;
          items.push({
            kind: "tool",
            ts,
            name,
            input,
            output: result === undefined ? null : outputText(result),
            isError: call.status === "error" || !!call.error,
            run,
            raw,
          });
        }
      } else if (message.type === "info" || message.type === "error") {
        items.push({
          kind: "info",
          label: message.type,
          ts,
          text: joinText(message.content),
          raw,
        });
      } else {
        noteUnsupported(meta.diagnostics, "gemini." + message.type);
      }
    }
    return finalize(meta, items);
  }

  function openCodePartTime(part, message) {
    return (part.time && (part.time.start || part.time.created || part.time.end)) ||
      (message.time && (message.time.created || message.time.completed)) || "";
  }

  function parseCommandCode(rawText) {
    const meta = baseMeta("commandcode", "commandcode");
    const items = [];
    const pending = new Map();
    const parsed = parseJsonLines(rawText);
    mergeDiagnostics(meta.diagnostics, parsed.diagnostics);
    for (const obj of parsed.objects) {
      touchTime(meta, obj.timestamp);
      const role = obj.role || "";
      if (role === "user") {
        for (const block of Array.isArray(obj.content) ? obj.content : []) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "text" && String(block.text || "").trim()) {
            items.push({ kind: "user", ts: obj.timestamp, text: block.text, raw: [obj] });
          } else if (block.type === "tool_result") {
            const tool = pending.get(block.tool_use_id);
            const text = outputText(block.content);
            if (tool) {
              tool.output = text;
              tool.isError = !!block.is_error;
              tool.raw.push(obj);
              pending.delete(block.tool_use_id);
            } else if (text.trim()) {
              meta.diagnostics.orphanResults++;
              items.push({ kind: "info", label: "tool result", ts: obj.timestamp, text, raw: [obj] });
            }
          }
        }
      } else if (role === "assistant") {
        for (const block of Array.isArray(obj.content) ? obj.content : []) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "reasoning" && String(block.text || "").trim()) {
            items.push({ kind: "thinking", ts: obj.timestamp, text: block.text, raw: [obj] });
          } else if (block.type === "text" && String(block.text || "").trim()) {
            items.push({ kind: "assistant", ts: obj.timestamp, text: block.text, raw: [obj] });
          } else if (block.type === "tool_use") {
            const tool = {
              kind: "tool",
              ts: obj.timestamp,
              name: block.name,
              input: block.input,
              output: null,
              isError: false,
              raw: [obj],
            };
            if (block.id) pending.set(block.id, tool);
            items.push(tool);
          } else {
            noteUnsupported(meta.diagnostics, "commandcode.assistant." + block.type);
          }
        }
      }
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user");
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, pending);
  }

  function parseKimi(rawText) {
    const meta = baseMeta("kimi", "kimi");
    const items = [];
    const pending = new Map();
    const data = parseDocument(rawText, meta, "Kimi");
    if (!data) return finalize(meta, items, pending);
    if (!Array.isArray(data.messages)) return finalize(meta, items, pending);
    for (const entry of data.messages) {
      const message = entry && typeof entry === "object" &&
        entry.message && typeof entry.message === "object" ? entry.message : entry;
      const role = message.role || "";
      const content = message.content;
      if (role === "user") {
        if (typeof content === "string") {
          if (String(content).trim()) items.push({ kind: "user", text: content, raw: [entry] });
        } else if (Array.isArray(content)) {
          for (const block of content) {
            if (!block || typeof block !== "object") continue;
            if (block.type === "text" && String(block.text || "").trim()) {
              items.push({ kind: "user", text: block.text, raw: [entry] });
            }
          }
        }
      } else if (role === "assistant") {
        for (const block of Array.isArray(content) ? content : []) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "thinking" && String(block.thinking || "").trim()) {
            items.push({ kind: "thinking", text: block.thinking, raw: [entry] });
          } else if (block.type === "text" && String(block.text || "").trim()) {
            items.push({ kind: "assistant", text: block.text, raw: [entry] });
          }
        }
      } else if (role === "tool") {
        const tool = {
          kind: "tool",
          name: message.toolName || "tool",
          input: message.arguments,
          output: outputText(message.content),
          isError: false,
          raw: [entry],
        };
        if (message.tool_call_id) pending.set(message.tool_call_id, tool);
        items.push(tool);
      }
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user");
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, pending);
  }

  function parseCursorCli(rawText) {
    const meta = baseMeta("cursor-cli", "cursor-cli");
    const items = [];
    const pending = new Map();
    const parsed = parseJsonLines(rawText);
    mergeDiagnostics(meta.diagnostics, parsed.diagnostics);
    for (const obj of parsed.objects) {
      touchTime(meta, obj.timestamp);
      const message = obj.message || {};
      const role = obj.role || message.role || "";
      if (role === "user") {
        for (const block of Array.isArray(message.content) ? message.content : []) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "text" && String(block.text || "").trim()) {
            items.push({ kind: "user", ts: obj.timestamp, text: block.text, raw: [obj] });
          }
        }
      } else if (role === "assistant") {
        for (const block of Array.isArray(message.content) ? message.content : []) {
          if (!block || typeof block !== "object") continue;
          if (block.type === "text" && String(block.text || "").trim()) {
            items.push({ kind: "assistant", ts: obj.timestamp, text: block.text, raw: [obj] });
          } else if (block.type === "tool_use") {
            items.push({
              kind: "tool",
              ts: obj.timestamp,
              name: block.name || "tool",
              input: block.input,
              output: null,
              isError: false,
              raw: [obj],
            });
          }
        }
      }
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user");
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, pending);
  }

  function parseMessagesArchive(rawText, source) {
    // Generic {messages: [{message: {role, content}}]} archive used by the
    // amp, trae, and zed loaders.
    const meta = baseMeta(source, source);
    const items = [];
    const pending = new Map();
    const data = parseDocument(rawText, meta, source);
    if (!data) return finalize(meta, items, pending);
    for (const entry of Array.isArray(data.messages) ? data.messages : []) {
      const message = entry && typeof entry === "object" &&
        entry.message && typeof entry.message === "object" ? entry.message : entry;
      const role = message.role || "";
      const content = message.content;
      if (role === "user") {
        const text = outputText(content);
        if (text.trim()) items.push({ kind: "user", text, raw: [entry] });
      } else if (role === "assistant") {
        const text = outputText(content);
        if (text.trim()) items.push({ kind: "assistant", text, raw: [entry] });
      } else if (role === "tool") {
        items.push({
          kind: "tool", name: message.toolName || "tool",
          input: message.arguments, output: outputText(content),
          isError: false, raw: [entry],
        });
      }
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user");
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, pending);
  }

  function parseKiro(rawText) {
    // Kiro CLI ACP session logs: defensive generic event walker.
    const meta = baseMeta("kiro", "kiro");
    const items = [];
    const parsed = parseJsonLines(rawText);
    mergeDiagnostics(meta.diagnostics, parsed.diagnostics);
    for (const obj of parsed.objects) {
      touchTime(meta, obj.timestamp);
      let role = obj.role || "";
      let text = outputText(obj.content !== undefined ? obj.content : obj.message);
      if (!role) {
        const type = String(obj.type || "");
        if (type.includes("user")) role = "user";
        else if (type.includes("agent") || type.includes("assistant")) role = "assistant";
        else continue;
        if (!text) text = outputText(obj.text || obj.payload);
      }
      if (!text.trim()) continue;
      items.push({ kind: role, ts: obj.timestamp, text, raw: [obj] });
    }
    if (!meta.title) {
      const firstUser = items.find((item) => item.kind === "user");
      if (firstUser) meta.title = firstUser.text;
    }
    return finalize(meta, items, new Map());
  }

  function parseAider(rawText) {
    const meta = baseMeta("aider", "aider");
    const items = [];
    const text = String(rawText || "").trim();
    if (text) {
      items.push({ kind: "info", label: "aider history", text });
    }
    return finalize(meta, items, new Map());
  }

  function parseOpenCode(rawText) {
    const meta = baseMeta("opencode", "opencode");
    const items = [];
    let data;
    try {
      data = JSON.parse(String(rawText || ""));
    } catch (error) {
      meta.diagnostics.malformedLines++;
      meta.diagnostics.warnings.push("The OpenCode archive is not valid JSON.");
      return finalize(meta, items);
    }
    const session = data.session || {};
    meta.title = session.title || "";
    meta.cwd = session.directory || "";
    meta.start = session.time && session.time.created || "";
    meta.end = session.time && session.time.updated || meta.start;

    for (const entry of Array.isArray(data.messages) ? data.messages : []) {
      const message = entry.message || entry;
      const parts = Array.isArray(entry.parts) ? entry.parts : [];
      const role = message.role || "";
      const run = {
        model: message.modelID || "",
      };
      if (run.model) meta.models.add(run.model);
      if (role === "assistant" && message.tokens) {
        meta.tokens.input += message.tokens.input || 0;
        meta.tokens.output += message.tokens.output || 0;
        meta.tokens.reasoning += message.tokens.reasoning || 0;
        const cache = message.tokens.cache || {};
        meta.tokens.cacheRead += cache.read || 0;
        meta.tokens.cacheWrite += cache.write || 0;
      }
      for (const part of parts) {
        const ts = openCodePartTime(part, message);
        touchTime(meta, ts);
        const raw = [message, part];
        switch (part.type) {
          case "text":
            if (String(part.text || "").trim()) {
              const kind = role === "user" ? "user" : "assistant";
              if (!meta.title && kind === "user") meta.title = part.text.trim();
              items.push({ kind, ts, text: part.text, run, raw });
            }
            break;
          case "reasoning":
            if (String(part.text || "").trim()) {
              items.push({ kind: "thinking", ts, text: part.text, run, raw });
            }
            break;
          case "tool": {
            const state = part.state || {};
            const status = state.status || "";
            items.push({
              kind: "tool",
              ts,
              name: part.tool || part.name || "tool",
              input: state.input !== undefined ? state.input : part.input || {},
              output: state.output !== undefined ? outputText(state.output) :
                state.error !== undefined ? outputText(state.error) : null,
              isError: status === "error" || !!state.error,
              run,
              raw,
            });
            break;
          }
          case "step-start":
          case "step-finish":
          case "snapshot":
          case "patch":
            meta.diagnostics.ignoredRecords++;
            break;
          default:
            noteUnsupported(meta.diagnostics, "opencode." + part.type);
        }
      }
    }
    return finalize(meta, items);
  }

  function cursorModel(bubble, composer) {
    const info = bubble.modelInfo || composer.modelInfo || composer.modelConfig || {};
    return info.modelName || info.modelId || info.model || info.name ||
      composer.model || composer.modelName || "";
  }

  function cursorThinkingText(value) {
    if (!value) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(cursorThinkingText).filter(Boolean).join("\n\n");
    if (typeof value === "object") {
      return value.text || value.content || value.thought || value.description ||
        value.summary || "";
    }
    return "";
  }

  function cursorTool(bubble, run, ts) {
    const data = bubble.toolFormerData || {};
    const name = data.name || data.toolName || data.tool ||
      bubble.toolName || bubble.capabilityName || "";
    if (!name) return null;
    const input = data.arguments !== undefined ? data.arguments :
      data.args !== undefined ? data.args :
      data.input !== undefined ? data.input : {};
    const result = data.result !== undefined ? data.result :
      data.output !== undefined ? data.output :
      bubble.toolResults && bubble.toolResults.length ? bubble.toolResults : undefined;
    return {
      kind: "tool",
      ts,
      name,
      input,
      output: result === undefined ? null : outputText(result),
      isError: !!data.error || !!bubble.errorDetails,
      run,
      raw: [bubble],
    };
  }

  function parseCursor(rawText) {
    const meta = baseMeta("cursor", "cursor");
    const items = [];
    let data;
    try {
      data = JSON.parse(String(rawText || ""));
    } catch (error) {
      meta.diagnostics.malformedLines++;
      meta.diagnostics.warnings.push("The Cursor archive is not valid JSON.");
      return finalize(meta, items);
    }
    const composer = data.composer || {};
    meta.title = composer.name || "";
    meta.cwd = composer.workspaceProjectDir || composer.cwd || "";
    meta.start = composer.createdAt || "";
    meta.end = composer.lastUpdatedAt || meta.start;
    for (const bubble of Array.isArray(data.bubbles) ? data.bubbles : []) {
      const timing = bubble.timingInfo || {};
      const ts = bubble.createdAt || timing.clientStartTime || timing.clientRpcSendTime || "";
      touchTime(meta, ts);
      const run = { model: cursorModel(bubble, composer) };
      if (run.model) meta.models.add(run.model);
      const tokens = bubble.tokenCount || {};
      meta.tokens.input += tokens.inputTokens || tokens.input || 0;
      meta.tokens.output += tokens.outputTokens || tokens.output || 0;

      if (bubble.type === 1 || bubble.type === "user") {
        const text = bubble.text || "";
        if (String(text).trim()) {
          if (!meta.title) meta.title = text.trim();
          items.push({ kind: "user", ts, text, run, raw: [bubble] });
        }
        continue;
      }
      if (bubble.type !== 2 && bubble.type !== "assistant") {
        noteUnsupported(meta.diagnostics, "cursor.bubble." + bubble.type);
        continue;
      }
      const tool = cursorTool(bubble, run, ts);
      if (tool) {
        items.push(tool);
        continue;
      }
      const thinking = cursorThinkingText(bubble.thinking) ||
        cursorThinkingText(bubble.allThinkingBlocks);
      if (thinking.trim()) {
        items.push({ kind: "thinking", ts, text: thinking, run, raw: [bubble] });
      }
      const text = bubble.text || "";
      if (String(text).trim()) {
        items.push({
          kind: bubble.isThought ? "thinking" : "assistant",
          ts,
          text,
          run,
          raw: [bubble],
        });
      } else if (!thinking) {
        meta.diagnostics.ignoredRecords++;
      }
    }
    return finalize(meta, items);
  }

  function parseDocument(rawText, meta, label) {
    try {
      const value = JSON.parse(String(rawText || ""));
      if (value && typeof value === "object") return value;
    } catch (error) {
      // The caller records one format-specific parse error below.
    }
    meta.diagnostics.malformedLines++;
    meta.diagnostics.warnings.push("The " + label + " session is not valid JSON.");
    return null;
  }

  function modelString(value) {
    if (!value) return "";
    if (typeof value === "string") return value;
    if (typeof value === "object") {
      return value.identifier || value.modelId || value.modelID || value.model ||
        value.name || value.id || "";
    }
    return String(value);
  }

  function metadataTokens(meta, value) {
    if (!value || typeof value !== "object") return;
    const usage = Array.isArray(value.usage) ? value.usage[0] :
      value.usage || value.tokenUsage || value.tokens || value;
    if (!usage || typeof usage !== "object") return;
    meta.tokens.input += tokenNumber(usage, ["inputTokens", "promptTokens", "input", "input_tokens"]);
    meta.tokens.output += tokenNumber(usage, ["outputTokens", "completionTokens", "output", "output_tokens"]);
    meta.tokens.cacheRead += tokenNumber(usage, ["cacheReadTokens", "cachedInputTokens", "cache_read_input_tokens"]);
    meta.tokens.reasoning += tokenNumber(usage, ["reasoningTokens", "reasoning", "reasoning_output_tokens"]);
  }

  function tokenNumber(value, keys) {
    if (!value || typeof value !== "object") return 0;
    for (const key of keys) {
      if (typeof value[key] === "number") return value[key];
    }
    return 0;
  }

  function parseExtensionTask(rawText, source) {
    const meta = baseMeta(source, source);
    const items = [];
    const pending = new Map();
    const data = parseDocument(rawText, meta, source === "roo" ? "Roo Code" : "Cline");
    if (!data) return finalize(meta, items, pending);
    const metadata = data.metadata || {};
    meta.title = metadata.title || metadata.task || metadata.name || "";
    const workspace = metadata.workspaceDirectory || metadata.cwd ||
      metadata.workspacePath || metadata.projectPath || metadata.rootPath ||
      metadata.workspace || "";
    meta.cwd = typeof workspace === "string" ? workspace :
      workspace.path || workspace.cwd || workspace.workspaceDirectory || "";
    meta.start = metadata.createdAt || metadata.ts || "";
    meta.end = metadata.updatedAt || metadata.lastUpdated ||
      metadata.lastActivityAt || meta.start;
    metadataTokens(meta, metadata);

    const apiMessages = Array.isArray(data.apiMessages) ? data.apiMessages : [];
    for (const message of apiMessages) {
      const role = message.role || "";
      const ts = message.ts || message.timestamp || "";
      touchTime(meta, ts);
      const run = { model: modelString(message.model || metadata.model) };
      if (run.model) meta.models.add(run.model);
      const blocks = Array.isArray(message.content) ?
        message.content :
        [{ type: "text", text: message.content || message.text || "" }];
      for (const block of blocks) {
        if (!block || typeof block !== "object") continue;
        if (block.type === "text") {
          const text = block.text || "";
          if (!String(text).trim()) continue;
          if (role === "user") {
            if (!meta.title) meta.title = text.trim();
            items.push({ kind: "user", ts, text, run, raw: [message] });
          } else if (role === "assistant") {
            items.push({ kind: "assistant", ts, text, run, raw: [message] });
          } else {
            items.push({ kind: "info", label: role || "message", ts, text, raw: [message] });
          }
        } else if (block.type === "thinking" || block.type === "reasoning" ||
          block.type === "redacted_thinking") {
          const text = block.thinking || block.text || block.reasoning || "[redacted thinking]";
          items.push({ kind: "thinking", ts, text, run, raw: [message] });
        } else if (block.type === "tool_use") {
          const tool = {
            kind: "tool",
            ts,
            name: block.name || "tool",
            input: block.input || {},
            output: null,
            isError: false,
            run,
            raw: [message],
          };
          if (block.id) pending.set(block.id, tool);
          items.push(tool);
        } else if (block.type === "tool_result") {
          const tool = pending.get(block.tool_use_id || block.toolCallId);
          if (tool) {
            tool.output = outputText(block.content);
            tool.isError = !!block.is_error;
            tool.raw.push(message);
            pending.delete(block.tool_use_id || block.toolCallId);
          } else {
            meta.diagnostics.orphanResults++;
          }
        } else if (block.type !== "image") {
          noteUnsupported(meta.diagnostics, source + "." + block.type);
        }
      }
    }

    if (!apiMessages.length) {
      for (const message of Array.isArray(data.uiMessages) ? data.uiMessages : []) {
        const ts = message.ts || message.timestamp || "";
        touchTime(meta, ts);
        const reasoning = joinText(message.reasoning);
        if (reasoning.trim()) {
          items.push({ kind: "thinking", ts, text: reasoning, raw: [message] });
        }
        const text = joinText(message.text || message.content);
        if (!text.trim()) continue;
        if (message.type === "ask") {
          if (!meta.title) meta.title = text.trim();
          items.push({ kind: "user", ts, text, raw: [message] });
        } else if (message.say === "reasoning") {
          items.push({ kind: "thinking", ts, text, raw: [message] });
        } else {
          items.push({ kind: "assistant", ts, text, raw: [message] });
        }
      }
    }
    return finalize(meta, items, pending);
  }

  function gooseContent(value) {
    if (typeof value === "string") {
      try {
        return JSON.parse(value);
      } catch (error) {
        return value;
      }
    }
    return value;
  }

  function parseGoose(rawText) {
    const meta = baseMeta("goose", "goose");
    const items = [];
    const pending = new Map();
    const data = parseDocument(rawText, meta, "Goose");
    if (!data) return finalize(meta, items, pending);
    const session = data.session || {};
    meta.title = session.name || session.title || "";
    meta.cwd = session.working_dir || session.cwd || "";
    meta.start = session.created_at || "";
    meta.end = session.updated_at || meta.start;
    const config = session.model_config_json || {};
    const sessionModel = modelString(config.model || config.model_name || session.model);
    if (sessionModel) meta.models.add(sessionModel);
    metadataTokens(meta, session);
    if (!meta.tokens.input && typeof session.input_tokens === "number") {
      meta.tokens.input = session.input_tokens;
    }
    if (!meta.tokens.output && typeof session.output_tokens === "number") {
      meta.tokens.output = session.output_tokens;
    }

    for (const message of Array.isArray(data.messages) ? data.messages : []) {
      const role = message.role || "";
      const ts = message.created_timestamp || message.created_at || "";
      touchTime(meta, ts);
      const run = { model: sessionModel };
      const content = gooseContent(message.content_json !== undefined ?
        message.content_json : message.content);
      const blocks = Array.isArray(content) ? content :
        [typeof content === "object" ? content : { type: "text", text: content || "" }];
      for (const block of blocks) {
        if (!block || typeof block !== "object") continue;
        const type = block.type || (block.text !== undefined ? "text" : "");
        if (type === "text" || type === "message") {
          const text = block.text || block.content || "";
          if (!String(text).trim()) continue;
          if (role === "user") {
            if (!meta.title) meta.title = text.trim();
            items.push({ kind: "user", ts, text, run, raw: [message] });
          } else if (role === "assistant") {
            items.push({ kind: "assistant", ts, text, run, raw: [message] });
          } else if (role === "system") {
            items.push({ kind: "info", label: "system", ts, text, raw: [message] });
          } else {
            items.push({ kind: "info", label: role || "message", ts, text, raw: [message] });
          }
        } else if (["thinking", "reasoning"].includes(type)) {
          const text = block.text || block.content || block.thinking || "";
          if (String(text).trim()) items.push({ kind: "thinking", ts, text, run, raw: [message] });
        } else if (["tool_request", "tool_use", "tool_call"].includes(type)) {
          const tool = {
            kind: "tool",
            ts,
            name: block.name || block.tool || block.tool_name || "tool",
            input: block.arguments || block.args || block.input || {},
            output: null,
            isError: false,
            run,
            raw: [message],
          };
          const id = block.id || block.tool_call_id || block.toolCallId;
          if (id) pending.set(id, tool);
          items.push(tool);
        } else if (["tool_response", "tool_result"].includes(type)) {
          const id = block.tool_call_id || block.tool_use_id || block.toolCallId || block.id;
          const tool = pending.get(id);
          if (tool) {
            tool.output = outputText(block.content !== undefined ? block.content : block.output);
            tool.isError = !!block.error || block.status === "error";
            tool.raw.push(message);
            pending.delete(id);
          } else {
            meta.diagnostics.orphanResults++;
          }
        } else {
          noteUnsupported(meta.diagnostics, "goose." + type);
        }
      }
    }
    return finalize(meta, items, pending);
  }

  function vscodeMessageText(value) {
    if (typeof value === "string") return value;
    if (!value || typeof value !== "object") return "";
    if (typeof value.text === "string") return value.text;
    if (Array.isArray(value.parts)) return value.parts.map((part) => part.text || "").join("");
    return joinText(value.content);
  }

  function vscodeMarkdownText(value) {
    if (typeof value === "string") return value;
    if (value && typeof value === "object") return value.value || value.text || "";
    return "";
  }

  function parseVSCode(rawText) {
    const meta = baseMeta("vscode", "vscode");
    const items = [];
    const data = parseDocument(rawText, meta, "VS Code");
    if (!data) return finalize(meta, items);
    const session = data.session || data;
    meta.title = session.customTitle || "";
    meta.cwd = typeof session.workingDirectory === "string" ?
      session.workingDirectory :
      session.workingDirectory && (
        session.workingDirectory.fsPath || session.workingDirectory.path ||
        session.workingDirectory.external
      ) || "";
    meta.start = session.creationDate || "";
    const selectedModel = modelString(
      session.inputState && session.inputState.selectedModel
    );
    if (selectedModel) meta.models.add(selectedModel);
    const backendDiagnostics = data.diagnostics || {};
    if (backendDiagnostics.ignoredTornLines) {
      meta.diagnostics.malformedLines += backendDiagnostics.ignoredTornLines;
      meta.diagnostics.warnings.push(
        backendDiagnostics.ignoredTornLines + " torn final VS Code log line was ignored."
      );
    }

    for (const request of Array.isArray(session.requests) ? session.requests : []) {
      const ts = request.timestamp || "";
      touchTime(meta, ts);
      const model = modelString(request.modelId || selectedModel);
      const run = { model };
      if (model) meta.models.add(model);
      const prompt = vscodeMessageText(request.message);
      if (prompt.trim()) {
        if (!meta.title) meta.title = prompt.trim();
        items.push({ kind: "user", ts, text: prompt, run, raw: [request] });
      }
      const resultMetadata = request.result && request.result.metadata || {};
      const toolResults = resultMetadata.toolCallResults || {};
      const seenTools = new Set();
      for (const round of Array.isArray(resultMetadata.toolCallRounds) ?
        resultMetadata.toolCallRounds : []) {
        const roundTs = round.timestamp || ts;
        const thinking = round.thinking && round.thinking.text || "";
        if (thinking.trim()) {
          items.push({ kind: "thinking", ts: roundTs, text: thinking, run, raw: [request] });
          meta.tokens.reasoning += round.thinking.tokens || 0;
        }
        for (const call of Array.isArray(round.toolCalls) ? round.toolCalls : []) {
          const id = call.id || call.toolCallId;
          const result = id && toolResults[id];
          items.push({
            kind: "tool",
            ts: roundTs,
            name: call.name || "tool",
            input: call.arguments || call.args || {},
            output: result === undefined ? null : outputText(result),
            isError: !!(result && result.error),
            run,
            raw: [request],
          });
          if (id) seenTools.add(id);
        }
      }

      const response = Array.isArray(request.response) ? request.response :
        request.response && Array.isArray(request.response.value) ?
          request.response.value : [];
      for (const part of response) {
        if (typeof part === "string") {
          if (part.trim()) items.push({ kind: "assistant", ts, text: part, run, raw: [request] });
          continue;
        }
        if (!part || typeof part !== "object") continue;
        const kind = part.kind || "markdown";
        if (kind === "markdown") {
          const text = vscodeMarkdownText(part.value);
          if (text.trim()) items.push({ kind: "assistant", ts, text, run, raw: [request] });
        } else if (kind === "thinking") {
          const text = vscodeMarkdownText(part.value);
          if (text.trim()) items.push({ kind: "thinking", ts, text, run, raw: [request] });
        } else if (kind === "toolInvocationSerialized") {
          if (part.toolCallId && seenTools.has(part.toolCallId)) continue;
          items.push({
            kind: "tool",
            ts,
            name: part.toolId || part.generatedTitle || "tool",
            input: {},
            output: part.isComplete ? vscodeMarkdownText(part.pastTenseMessage) : null,
            isError: false,
            run,
            raw: [request],
          });
        } else if (kind === "confirmation") {
          items.push({
            kind: "info",
            label: "confirmation",
            ts,
            text: [part.title, part.message].filter(Boolean).join("\n"),
            raw: [request],
          });
        } else if ([
          "inlineReference", "codeblockUri", "prepareToolInvocation",
          "mcpServersStarting", "progressTaskSerialized",
        ].includes(kind)) {
          meta.diagnostics.ignoredRecords++;
        } else {
          noteUnsupported(meta.diagnostics, "vscode." + kind);
        }
      }
      const usage = request.usage || resultMetadata.usage || {};
      meta.tokens.input += tokenNumber(usage, ["inputTokens", "promptTokens", "input"]);
      meta.tokens.output += tokenNumber(usage, ["outputTokens", "completionTokens", "output"]);
      meta.tokens.cacheRead += tokenNumber(usage, ["cachedInputTokens", "cacheReadTokens"]);
    }
    return finalize(meta, items);
  }

  function reviveMeta(rawMeta) {
    const meta = {
      ...baseMeta(rawMeta.source || "archive", rawMeta.format || rawMeta.source || "archive"),
      ...rawMeta,
      models: new Set(rawMeta.models || []),
      efforts: new Set(rawMeta.efforts || []),
      tokens: {
        ...baseMeta("", "").tokens,
        ...(rawMeta.tokens || {}),
      },
      diagnostics: {
        ...diagnostics(),
        ...(rawMeta.diagnostics || {}),
        unsupported: { ...((rawMeta.diagnostics && rawMeta.diagnostics.unsupported) || {}) },
        warnings: [...((rawMeta.diagnostics && rawMeta.diagnostics.warnings) || [])],
      },
    };
    return meta;
  }

  function parseArchive(rawText) {
    let data;
    try {
      data = typeof rawText === "string" ? JSON.parse(rawText) : rawText;
    } catch (error) {
      const meta = baseMeta("archive", "archive");
      meta.diagnostics.malformedLines++;
      meta.diagnostics.warnings.push("The shared Fables archive is not valid JSON.");
      return finalize(meta, []);
    }
    if (!data || !data.fablesVersion || !Array.isArray(data.items)) {
      const meta = baseMeta("archive", "archive");
      meta.diagnostics.malformedLines++;
      meta.diagnostics.warnings.push("The shared file does not contain a Fables archive.");
      return finalize(meta, []);
    }
    const meta = reviveMeta(data.meta || {});
    const items = data.items.map((item) => ({ ...item }));
    return finalize(meta, items);
  }

  function detectFormat(rawText, hint) {
    if (hint) {
      if (hint === "cowork") return "claude";
      return hint;
    }
    const text = String(rawText || "").trimStart();
    if (text.startsWith("{")) {
      try {
        const value = JSON.parse(text);
        if (value && value.fablesVersion) return "archive";
        if (value && Array.isArray(value.messages) && value.sessionId) return "gemini";
        if (value && value.kimiArchive && Array.isArray(value.messages)) return "kimi";
        if (value && value.session && Array.isArray(value.messages)) return "opencode";
        if (value && value.composer && Array.isArray(value.bubbles)) return "cursor";
        if (value && value.metadata && Array.isArray(value.apiMessages)) return "cline";
        if (value && value.session && Array.isArray(value.messages) &&
          Array.isArray(value.usage)) return "goose";
        if (value && value.session && Array.isArray(value.session.requests)) return "vscode";
      } catch (error) {
        // JSONL commonly begins with an object but is not one JSON document.
      }
    }
    const parsed = parseJsonLines(text);
    const sample = parsed.objects.slice(0, 5);
    if (sample.some((obj) => String(obj.type || "").includes(".") && obj.data !== undefined)) {
      return "copilot";
    }
    if (sample.some((obj) => typeof obj.role === "string" && obj.sessionId !== undefined &&
      Array.isArray(obj.content))) {
      return "commandcode";
    }
    if (sample.some((obj) => obj.payload !== undefined || obj.type === "session_meta" ||
      obj.record_type !== undefined || (obj.git !== undefined && obj.id !== undefined))) {
      return "codex";
    }
    if (sample.some((obj) => obj.type === "message" && obj.message &&
      typeof obj.message === "object")) {
      return "pi";
    }
    if (sample.some((obj) => (obj.role === "user" || obj.role === "assistant") &&
      obj.message && typeof obj.message === "object" && obj.type === undefined)) {
      return "cursor-cli";
    }
    return "claude";
  }

  function parseSession(rawText, hint, source) {
    const format = detectFormat(rawText, hint);
    let parsed;
    if (format === "archive") parsed = parseArchive(rawText);
    else if (format === "codex") parsed = parseCodex(rawText);
    else if (format === "copilot") parsed = parseCopilot(rawText);
    else if (format === "commandcode") parsed = parseCommandCode(rawText);
    else if (format === "pi" || format === "prime") parsed = parsePi(rawText);
    else if (format === "kimi") parsed = parseKimi(rawText);
    else if (format === "amp" || format === "zed" || format === "trae") parsed = parseMessagesArchive(rawText, format);
    else if (format === "qwen") parsed = parseCommandCode(rawText);
    else if (format === "kilo") parsed = parseExtensionTask(rawText, "kilo");
    else if (format === "kiro") parsed = parseKiro(rawText);
    else if (format === "aider") parsed = parseAider(rawText);
    else if (format === "cursor-cli") parsed = parseCursorCli(rawText);
    else if (format === "gemini") parsed = parseGemini(rawText);
    else if (format === "opencode") parsed = parseOpenCode(rawText);
    else if (format === "cursor") parsed = parseCursor(rawText);
    else if (format === "cline" || format === "roo") parsed = parseExtensionTask(rawText, format);
    else if (format === "goose") parsed = parseGoose(rawText);
    else if (format === "vscode") parsed = parseVSCode(rawText);
    else parsed = parseClaude(rawText, source || (hint === "cowork" ? "cowork" : "claude"));
    if (source && format !== "archive") parsed.meta.source = source;
    parsed.meta.format = format;
    return parsed;
  }

  function stableString(value) {
    if (value === undefined || value === null) return "";
    if (typeof value === "string") return value;
    try {
      return JSON.stringify(value);
    } catch (error) {
      return String(value);
    }
  }

  function itemSearchText(item) {
    return [
      item.text,
      item.label,
      item.name,
      stableString(item.input),
      stableString(item.output),
      item.run && item.run.model,
    ].filter(Boolean).join(" ").toLowerCase();
  }

  const SECRET_PATTERNS = [
    /-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----/g,
    /\b(?:sk-(?:proj-)?|sk-ant-|github_pat_|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b/g,
    /\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b/g,
    /\bBearer\s+[A-Za-z0-9._~+\/=-]{12,}\b/gi,
    /\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|secret)\b(\s*[:=]\s*["']?)[^\s"',;]{6,}/gi,
  ];
  const PATH_PATTERNS = [
    /\/Users\/[^/\s"'`]+(?=\/)/g,
    /\/home\/[^/\s"'`]+(?=\/)/g,
    /\b[A-Za-z]:\\Users\\[^\\\s"'`]+(?=\\)/g,
  ];
  const EMAIL_PATTERN = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi;

  function countMatches(text, patterns) {
    let count = 0;
    for (const pattern of patterns) {
      const matches = String(text || "").match(new RegExp(pattern.source, pattern.flags));
      count += matches ? matches.length : 0;
    }
    return count;
  }

  function inspectSensitive(parsed) {
    const text = (parsed.items || []).map((item) => [
      item.text,
      stableString(item.input),
      stableString(item.output),
    ].filter(Boolean).join("\n")).join("\n");
    return {
      secrets: countMatches(text, SECRET_PATTERNS),
      paths: countMatches(text, PATH_PATTERNS),
      emails: countMatches(text, [EMAIL_PATTERN]),
    };
  }

  function redactText(text, options) {
    let value = String(text || "");
    const opts = options || {};
    if (opts.redactSecrets !== false) {
      for (const pattern of SECRET_PATTERNS) {
        value = value.replace(new RegExp(pattern.source, pattern.flags), "[redacted secret]");
      }
    }
    if (opts.redactPaths) {
      value = value
        .replace(PATH_PATTERNS[0], "~")
        .replace(PATH_PATTERNS[1], "~")
        .replace(PATH_PATTERNS[2], "~");
    }
    if (opts.redactEmails) value = value.replace(EMAIL_PATTERN, "[redacted email]");
    return value;
  }

  function sanitizeValue(value, options, depth) {
    if ((depth || 0) > 20) return "[nested value omitted]";
    if (typeof value === "string") return redactText(value, options);
    if (Array.isArray(value)) return value.map((item) => sanitizeValue(item, options, (depth || 0) + 1));
    if (value && typeof value === "object") {
      const result = {};
      for (const [key, child] of Object.entries(value)) {
        result[key] = sanitizeValue(child, options, (depth || 0) + 1);
      }
      return result;
    }
    return value;
  }

  function plainMeta(meta, options) {
    return sanitizeValue({
      source: meta.source,
      format: meta.format,
      title: meta.title,
      cwd: meta.cwd,
      branch: meta.branch,
      models: [...(meta.models || [])],
      efforts: [...(meta.efforts || [])],
      tokens: { ...(meta.tokens || {}) },
      start: meta.start,
      end: meta.end,
      diagnostics: {
        ...(meta.diagnostics || diagnostics()),
        unsupported: { ...((meta.diagnostics && meta.diagnostics.unsupported) || {}) },
        warnings: [...((meta.diagnostics && meta.diagnostics.warnings) || [])],
      },
    }, options, 0);
  }

  function makeShareArchive(parsed, options) {
    const opts = {
      includeThinking: false,
      includeTools: true,
      includeInfo: false,
      includeRaw: false,
      redactSecrets: true,
      redactPaths: true,
      redactEmails: false,
      ...(options || {}),
    };
    const items = [];
    for (const item of parsed.items || []) {
      if (item.kind === "thinking" && !opts.includeThinking) continue;
      if (item.kind === "tool" && !opts.includeTools) continue;
      if (item.kind === "info" && !opts.includeInfo) continue;
      const clean = {
        kind: item.kind,
        ts: item.ts,
        text: item.text,
        name: item.name,
        input: item.input,
        output: item.output,
        isError: item.isError,
        side: item.side,
        label: item.label,
        run: item.run,
      };
      if (opts.includeRaw) clean.raw = item.raw;
      for (const key of Object.keys(clean)) {
        if (clean[key] === undefined) delete clean[key];
      }
      items.push(sanitizeValue(clean, opts, 0));
    }
    return {
      fablesVersion: 2,
      exportedAt: new Date().toISOString(),
      meta: plainMeta(parsed.meta, opts),
      items,
    };
  }

  return {
    detectFormat,
    inspectSensitive,
    itemSearchText,
    makeShareArchive,
    outputText,
    parseArchive,
    parseAider,
    parseClaude,
    parseCodex,
    parseCopilot,
    parseCursor,
    parseExtensionTask,
    parseGemini,
    parseGoose,
    parseKiro,
    parseMessagesArchive,
    parseOpenCode,
    parseSession,
    parseVSCode,
    redactText,
  };
});
